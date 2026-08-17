import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["LAUNCHER_QUEUE_URL"]


def handler(event, context):
    for record in event["Records"]:
        body = json.loads(record["body"])

        if body.get("Type") == "Notification":
            payload = json.loads(body["Message"])
        else:
            payload = body

        if payload.get("type") != "new_events":
            logger.info("Ignoring non-new_events message: %s", payload.get("type"))
            return

        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({
                "rule_type": "classifier_event",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": {
                    "event_ids": payload["created_event_ids"],
                    "event_names": payload["created_event_names"],
                },
            }),
        )
