"""POST /triggers: a trigger sent by hand, launch or shutdown, checked against its rule type's data_schema before it
is queued like any other."""
import json
import logging
import os

import boto3

from launcher.handlers.api.http import api_handler, error, ok
from launcher.queues import send_trigger
from launcher.rules.validation import validate_data

logger = logging.getLogger(__name__)

QUEUES = {"launch": "LAUNCHER_QUEUE_URL", "shutdown": "SHUTDOWN_QUEUE_URL"}


@api_handler
def handler(event, context):
    try:
        body = json.loads(event.get("body") or "")
    except json.JSONDecodeError:
        return error(400, "Invalid JSON body")
    if not isinstance(body, dict) or "rule_type" not in body or "data" not in body:
        return error(400, "Missing required fields: rule_type, data")
    action = body.get("action", "launch")
    if action not in QUEUES:
        return error(400, f"action must be one of {', '.join(QUEUES)}")
    problems = validate_data(body["rule_type"], body["data"])
    if problems:
        return error(400, "Invalid trigger", problems)

    trigger_id = send_trigger(boto3.client("sqs"), os.environ[QUEUES[action]], body["rule_type"], action, body["data"],
                              body.get("timestamp"))
    return ok({"trigger_id": trigger_id}, status=202)
