import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

sqs = boto3.client("sqs")
LAUNCHER_QUEUE_URL = os.environ["LAUNCHER_QUEUE_URL"]
SHUTDOWN_QUEUE_URL = os.environ["SHUTDOWN_QUEUE_URL"]


def handler(event, context):
    for record in event["Records"]:
        body = json.loads(record["body"])

        if body.get("Type") == "Notification":
            payload = json.loads(body["Message"])
        else:
            payload = body

        msg_type = payload.get("type")
        timestamp = datetime.now(timezone.utc).isoformat()

        if msg_type == "new_events":
            sqs.send_message(
                QueueUrl=LAUNCHER_QUEUE_URL,
                MessageBody=json.dumps({
                    "rule_type": "classifier_event",
                    "action": "launch",
                    "timestamp": timestamp,
                    "data": {
                        "event_ids": payload["created_event_ids"],
                        "event_names": payload["created_event_names"],
                    },
                }),
            )
        elif msg_type == "resolved":
            sqs.send_message(
                QueueUrl=SHUTDOWN_QUEUE_URL,
                MessageBody=json.dumps({
                    "rule_type": "classifier_event",
                    "action": "shutdown",
                    "timestamp": timestamp,
                    "data": {
                        "event_ids": payload["resolved_event_ids"],
                        "event_names": payload["resolved_event_names"],
                    },
                }),
            )
        else:
            logger.info("Ignoring classifier message type: %s", msg_type)
