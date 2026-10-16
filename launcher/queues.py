"""Sending triggers to the launch and shutdown queues. They are FIFO queues grouped by rule type, so the processors
take one trigger of a type at a time: two triggers can't both pass a dedup or concurrency check the other was about
to fail. Lambda's account concurrency is too small here to serialise them by reserving concurrency instead."""
import json
import uuid
from datetime import datetime, timezone


def send_trigger(sqs, queue_url: str, rule_type: str, action: str, data: dict, timestamp: str | None = None) -> str:
    trigger_id = str(uuid.uuid4())
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "trigger_id": trigger_id,
            "rule_type": rule_type,
            "action": action,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "data": data,
        }),
        MessageGroupId=rule_type,
        # The launcher dedups triggers itself, per rule and window; SQS's own five-minute dedup is switched off.
        MessageDeduplicationId=trigger_id,
    )
    return trigger_id
