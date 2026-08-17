import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qs

import boto3

from launcher.dynamo import DynamoClient
from launcher.models import LaunchRequest
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)

SLACK_SIGNING_SECRET_NAME = os.environ.get("SLACK_SIGNING_SECRET", "slack-signing-secret")
APPROVE_LAUNCH_FUNCTION_NAME = os.environ["APPROVE_LAUNCH_FUNCTION_NAME"]

_lambda_client = boto3.client("lambda")
_cached_signing_secret: str | None = None


def _get_signing_secret() -> str:
    global _cached_signing_secret
    if _cached_signing_secret:
        return _cached_signing_secret
    sm = boto3.client("secretsmanager")
    _cached_signing_secret = sm.get_secret_value(SecretId=SLACK_SIGNING_SECRET_NAME)["SecretString"]
    return _cached_signing_secret


def _verify_signature(body: str, timestamp: str, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    secret = _get_signing_secret()
    base = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    body = event.get("body") or ""

    if not _verify_signature(body, timestamp, signature):
        return {"statusCode": 401, "body": "Unauthorized"}

    parsed = parse_qs(body)
    payload = json.loads(parsed["payload"][0])

    action = payload["actions"][0]
    action_id = action["action_id"]
    request_id = action["value"]
    user_id = payload["user"]["id"]
    username = payload["user"].get("username") or payload["user"].get("name", "unknown")

    dynamo = DynamoClient()
    request = dynamo.get_request(request_id)
    if not request:
        return {"statusCode": 200, "body": ""}

    if action_id == "launch_reject":
        _handle_reject(request, user_id, username, dynamo)
    elif action_id == "launch_approve":
        _handle_approve(request, user_id, username, dynamo)

    return {"statusCode": 200, "body": ""}


def _handle_reject(request: LaunchRequest, user_id: str, username: str, dynamo: DynamoClient):
    slack = SlackClient()
    dynamo.update_request(request["request_id"], status="REJECTED", rejected_by=user_id)
    slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Strategy launch rejected by @{username}",
        blocks=_build_resolved_blocks(request, f"Rejected by @{username}", "rejected"),
    )


def _handle_approve(request: LaunchRequest, user_id: str, username: str, dynamo: DynamoClient):
    slack = SlackClient()
    dynamo.update_request(request["request_id"], status="APPROVED", approved_by=user_id)
    slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Strategy launch approved by @{username} — launching...",
        blocks=_build_resolved_blocks(request, f"Approved by @{username} — launching...", "approved"),
    )
    _lambda_client.invoke(
        FunctionName=APPROVE_LAUNCH_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({
            "request_id": request["request_id"],
            "username": username,
        }).encode(),
    )


def _build_resolved_blocks(request: dict, status_text: str, state: str) -> list[dict]:
    config = request.get("resolved_config", {})
    emoji = ":white_check_mark:" if state == "approved" else ":x:"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Strategy Launch Request"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{request.get('matched_rule_name', '')}"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n{config.get('strategy_class', '')} (ID {config.get('strategy_id', '')})"},
                {"type": "mrkdwn", "text": f"*Mode:*\n{config.get('mode', '')}"},
                {"type": "mrkdwn", "text": f"*Listings:*\n{config.get('listings', '')}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{emoji} {status_text}"}],
        },
    ]
