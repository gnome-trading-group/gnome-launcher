"""Triggers.

    POST /triggers                                   one sent by hand, launch or shutdown, checked against its rule
                                                     type's data_schema before it is queued like any other; an
                                                     optional deadline (ISO time) expires its launch
    GET  /triggers?rule_type=&before=&limit=         the trigger log: every rule's outcome for each trigger, newest
                                                     first, across every rule type unless one is named
"""
import json
import logging
import os

import boto3

from launcher.dynamo import DynamoClient
from launcher.handlers.api.http import api_handler, error, ok, query_int
from launcher.queues import send_trigger
from launcher.rules.catalog import RULE_TYPE_REGISTRY
from launcher.rules.validation import validate_data, validate_deadline

logger = logging.getLogger(__name__)

QUEUES = {"launch": "LAUNCHER_QUEUE_URL", "shutdown": "SHUTDOWN_QUEUE_URL"}
MAX_PAGE = 200


@api_handler
def handler(event, context):
    if event.get("httpMethod") == "GET":
        return _log(event)
    try:
        body = json.loads(event.get("body") or "")
    except json.JSONDecodeError:
        return error(400, "Invalid JSON body")
    if not isinstance(body, dict) or "rule_type" not in body or "data" not in body:
        return error(400, "Missing required fields: rule_type, data")
    action = body.get("action", "launch")
    if action not in QUEUES:
        return error(400, f"action must be one of {', '.join(QUEUES)}")
    problems = validate_data(body["rule_type"], body["data"]) + validate_deadline(body.get("deadline"))
    if problems:
        return error(400, "Invalid trigger", problems)

    trigger_id = send_trigger(boto3.client("sqs"), os.environ[QUEUES[action]], body["rule_type"], action, body["data"],
                              body.get("timestamp"), body.get("deadline"))
    return ok({"trigger_id": trigger_id}, status=202)


def _log(event):
    limit = query_int(event, "limit", 50, MAX_PAGE)
    if limit is None:
        return error(400, f"limit must be between 1 and {MAX_PAGE}")
    query = event.get("queryStringParameters") or {}
    dynamo = DynamoClient()
    rule_types = [query["rule_type"]] if query.get("rule_type") else list(RULE_TYPE_REGISTRY)
    items = sorted((e for t in rule_types for e in dynamo.evaluations(rule_type=t, before=query.get("before"), limit=limit)),
                   key=lambda e: e["received_at"], reverse=True)[:limit]
    return ok({"items": items, "next_before": items[-1]["received_at"] if len(items) == limit else None})
