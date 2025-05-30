#!/usr/bin/env python3
import json
import sys

from zenpy import Zenpy
import requests
from requests import Session
from requests.adapters import HTTPAdapter
import singer
from singer import metadata, metrics as singer_metrics
from tap_zendesk import metrics as zendesk_metrics
from tap_zendesk.discover import discover_streams
from tap_zendesk.streams import STREAMS
from tap_zendesk.sync import sync_stream

LOGGER = singer.get_logger()

REQUEST_TIMEOUT = 300

REQUIRED_CONFIG_KEYS = [
    "start_date",
    "subdomain",
]

# default authentication
OAUTH_CONFIG_KEYS = [
    "access_token",
]

# email + api_token authentication
API_TOKEN_CONFIG_KEYS = [
    "email",
    "api_token",
]

# patch Session.request to record HTTP request metrics
request = Session.request

def request_metrics_patch(self, method, url, **kwargs):
    with singer_metrics.http_request_timer(None):
        response = request(self, method, url, **kwargs)
        LOGGER.info("Request: %s, Response ETag: %s, Request Id: %s",
                    url,
                    response.headers.get('ETag', 'Not present'),
                    response.headers.get('X-Request-Id', 'Not present'))
        return response

Session.request = request_metrics_patch
# end patch

def do_discover(client, config):
    LOGGER.info("Starting discover")
    catalog = {"streams": discover_streams(client, config)}
    json.dump(catalog, sys.stdout, indent=2)
    LOGGER.info("Finished discover")

def stream_is_selected(mdata):
    return mdata.get((), {}).get('selected', False)

def get_selected_streams(catalog):
    selected_stream_names = []
    for stream in catalog.streams:
        mdata = metadata.to_map(stream.metadata)
        if stream_is_selected(mdata):
            selected_stream_names.append(stream.tap_stream_id)
    return selected_stream_names


SUB_STREAMS = {
    'tickets': ['ticket_audits', 'ticket_metrics', 'ticket_comments']
}

def get_sub_stream_names():
    sub_stream_names = []
    for parent_stream in SUB_STREAMS:
        sub_stream_names.extend(SUB_STREAMS[parent_stream])
    return sub_stream_names

class DependencyException(Exception):
    pass

def validate_dependencies(selected_stream_ids):
    errs = []
    msg_tmpl = ("Unable to extract {0} data. "
                "To receive {0} data, you also need to select {1}.")
    for parent_stream_name in SUB_STREAMS:
        sub_stream_names = SUB_STREAMS[parent_stream_name]
        for sub_stream_name in sub_stream_names:
            if sub_stream_name in selected_stream_ids and parent_stream_name not in selected_stream_ids:
                errs.append(msg_tmpl.format(sub_stream_name, parent_stream_name))

    if errs:
        raise DependencyException(" ".join(errs))

def populate_class_schemas(catalog, selected_stream_names):
    for stream in catalog.streams:
        if stream.tap_stream_id in selected_stream_names:
            STREAMS[stream.tap_stream_id].stream = stream

def do_sync(client, catalog, state, config):

    selected_stream_names = get_selected_streams(catalog)
    validate_dependencies(selected_stream_names)
    populate_class_schemas(catalog, selected_stream_names)
    all_sub_stream_names = get_sub_stream_names()

    for stream in catalog.streams:
        stream_name = stream.tap_stream_id
        mdata = metadata.to_map(stream.metadata)
        if stream_name not in selected_stream_names:
            LOGGER.info("%s: Skipping - not selected", stream_name)
            continue

        # if starting_stream:
        #     if starting_stream == stream_name:
        #         LOGGER.info("%s: Resuming", stream_name)
        #         starting_stream = None
        #     else:
        #         LOGGER.info("%s: Skipping - already synced", stream_name)
        #         continue
        # else:
        #     LOGGER.info("%s: Starting", stream_name)


        key_properties = metadata.get(mdata, (), 'table-key-properties')
        singer.write_schema(stream_name, stream.schema.to_dict(), key_properties)

        sub_stream_names = SUB_STREAMS.get(stream_name)
        if sub_stream_names:
            for sub_stream_name in sub_stream_names:
                if sub_stream_name not in selected_stream_names:
                    continue
                sub_stream = STREAMS[sub_stream_name].stream
                sub_mdata = metadata.to_map(sub_stream.metadata)
                sub_key_properties = metadata.get(sub_mdata, (), 'table-key-properties')
                singer.write_schema(sub_stream.tap_stream_id, sub_stream.schema.to_dict(), sub_key_properties)

        # parent stream will sync sub stream
        if stream_name in all_sub_stream_names:
            continue

        LOGGER.info("%s: Starting sync", stream_name)
        instance = STREAMS[stream_name](client, config)
        counter_value = sync_stream(state, config.get('start_date'), instance)
        # singer.write_state(state)
        LOGGER.info("%s: Completed sync (%s rows)", stream_name, counter_value)
        zendesk_metrics.log_aggregate_rates()

    singer.write_state(state)
    LOGGER.info("Finished sync")
    zendesk_metrics.log_aggregate_rates()

def oauth_auth(args):
    if not set(OAUTH_CONFIG_KEYS).issubset(args.config.keys()):
        LOGGER.debug("OAuth authentication unavailable.")
        return None

    LOGGER.info("Using OAuth authentication.")
    return {
        "subdomain": args.config['subdomain'],
        "oauth_token": args.config['access_token'],
    }

def api_token_auth(args):
    if not set(API_TOKEN_CONFIG_KEYS).issubset(args.config.keys()):
        LOGGER.debug("API Token authentication unavailable.")
        return None

    LOGGER.info("Using API Token authentication.")
    return {
        "subdomain": args.config['subdomain'],
        "email": args.config['email'],
        "token": args.config['api_token']
    }

def get_session(config):
    """ Add partner information to requests Session object if specified in the config. """
    if not all(k in config for k in ["marketplace_name",
                                     "marketplace_organization_id",
                                     "marketplace_app_id"]):
        return None
    session = requests.Session()
    # Using Zenpy's default adapter args, following the method outlined here:
    # https://github.com/facetoe/zenpy/blob/master/docs/zenpy.rst#usage
    session.mount("https://", HTTPAdapter(**Zenpy.http_adapter_kwargs()))
    session.headers["X-Zendesk-Marketplace-Name"] = config.get("marketplace_name", "")
    session.headers["X-Zendesk-Marketplace-Organization-Id"] = str(config.get("marketplace_organization_id", ""))
    session.headers["X-Zendesk-Marketplace-App-Id"] = str(config.get("marketplace_app_id", ""))
    return session

@singer.utils.handle_top_exception(LOGGER)
def main():
    parsed_args = singer.utils.parse_args(REQUIRED_CONFIG_KEYS)

    # Set request timeout to config param `request_timeout` value.
    config_request_timeout = parsed_args.config.get('request_timeout')
    if config_request_timeout and float(config_request_timeout):
        request_timeout = float(config_request_timeout)
    else:
        request_timeout = REQUEST_TIMEOUT # If value is 0, "0", "" or not passed then it sets default to 300 seconds.
    # OAuth has precedence
    creds = oauth_auth(parsed_args) or api_token_auth(parsed_args)
    session = get_session(parsed_args.config)
    client = Zenpy(session=session, timeout=request_timeout, **creds) # Pass request timeout

    if not client:
        LOGGER.error("""No suitable authentication keys provided.""")

    if parsed_args.discover:
        # passing the config to check the authentication in the do_discover method
        do_discover(client, parsed_args.config)
    elif parsed_args.catalog:
        state = parsed_args.state
        do_sync(client, parsed_args.catalog, state, parsed_args.config)

if __name__=="__main__":
    main()