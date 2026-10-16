"""The classifier's SNS notifications, turned into classifier_event triggers: new events to launch, resolved ones to
shut down."""
import json
import logging
import os

import boto3

from launcher.queues import send_trigger

logger = logging.getLogger(__name__)

RULE_TYPE = "classifier_event"

sqs = boto3.client("sqs")
LAUNCHER_QUEUE_URL = os.environ["LAUNCHER_QUEUE_URL"]
SHUTDOWN_QUEUE_URL = os.environ["SHUTDOWN_QUEUE_URL"]


def handler(event, context):
    for record in event["Records"]:
        body = json.loads(record["body"])
        payload = json.loads(body["Message"]) if body.get("Type") == "Notification" else body

        msg_type = payload.get("type")
        if msg_type == "new_events":
            send_trigger(sqs, LAUNCHER_QUEUE_URL, RULE_TYPE, "launch", {
                "event_ids": payload["created_event_ids"],
                "event_names": payload["created_event_names"],
            })
        elif msg_type == "resolved":
            send_trigger(sqs, SHUTDOWN_QUEUE_URL, RULE_TYPE, "shutdown", {
                "event_ids": payload["resolved_event_ids"],
                "event_names": payload["resolved_event_names"],
            })
        else:
            logger.info("Ignoring classifier message type: %s", msg_type)
