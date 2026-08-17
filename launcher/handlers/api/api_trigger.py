import json
import logging
import os
from datetime import datetime, timezone

import boto3

from launcher.handlers.api.http import api_handler, error, ok

logger = logging.getLogger(__name__)

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["LAUNCHER_QUEUE_URL"]

REQUIRED_FIELDS = {"rule_type", "data"}


@api_handler
def handler(event, context):
    body_raw = event.get("body") or ""
    try:
        body = json.loads(body_raw)
    except json.JSONDecodeError:
        return error(400, "Invalid JSON body")

    missing = REQUIRED_FIELDS - set(body)
    if missing:
        return error(400, f"Missing required fields: {', '.join(sorted(missing))}")

    if not isinstance(body["data"], dict):
        return error(400, "Field 'data' must be an object")

    message = {
        "rule_type": body["rule_type"],
        "timestamp": body.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "data": body["data"],
    }

    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(message),
    )

    return ok({"message": "Trigger accepted"}, status=202)
