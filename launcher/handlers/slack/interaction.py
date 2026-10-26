import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs

import boto3

from launcher import lifecycle
from launcher.dynamo import DynamoClient
from launcher.models import LaunchRequest
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)

SLACK_SIGNING_SECRET_NAME = os.environ.get("SLACK_SIGNING_SECRET", "slack-signing-secret")
APPROVE_LAUNCH_FUNCTION_NAME = os.environ["APPROVE_LAUNCH_FUNCTION_NAME"]
APPROVE_SHUTDOWN_FUNCTION_NAME = os.environ["APPROVE_SHUTDOWN_FUNCTION_NAME"]
# gnome-bot posts its buttons through the same Slack app, so their clicks arrive here; its function has a fixed name.
BOT_INTERACT_FUNCTION_NAME = os.environ.get("BOT_INTERACT_FUNCTION_NAME", "gnome-bot-interact")
BOT_ACTION_PREFIX = "bot_"

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


ACTIONS = {"launch_approve", "launch_reject", "shutdown_approve", "shutdown_reject", "schedule_modify", "schedule_cancel"}


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    body = event.get("body") or ""

    if not _verify_signature(body, timestamp, signature):
        return {"statusCode": 401, "body": "Unauthorized"}

    parsed = parse_qs(body)
    payload = json.loads(parsed["payload"][0])

    dynamo = DynamoClient()
    slack = SlackClient()

    payload_type = payload.get("type")
    if payload_type == "view_submission":
        return _handle_view_submission(payload, dynamo, slack)

    # Slack sends this app every click on any of its messages, including link buttons on the alerts it posts, which
    # carry no request; those are acknowledged and left alone.
    action = payload["actions"][0]
    action_id = action.get("action_id")
    if (action_id or "").startswith(BOT_ACTION_PREFIX):
        _forward_to_bot(payload)
        return {"statusCode": 200, "body": ""}
    if action_id not in ACTIONS or "value" not in action:
        return {"statusCode": 200, "body": ""}
    request_id = action["value"]
    user_id = payload["user"]["id"]
    username = payload["user"].get("username") or payload["user"].get("name", "unknown")

    request = dynamo.get_request(request_id)
    if not request:
        return {"statusCode": 200, "body": ""}

    if action_id == "launch_reject":
        _handle_launch_reject(request, user_id, username, dynamo, slack)
    elif action_id == "launch_approve":
        _handle_launch_approve(request, user_id, username, dynamo, slack)
    elif action_id == "shutdown_approve":
        _handle_shutdown_approve(request, user_id, username, dynamo, slack)
    elif action_id == "shutdown_reject":
        _handle_shutdown_reject(request, user_id, username, dynamo, slack)
    elif action_id == "schedule_modify":
        _handle_schedule_modify(request, payload.get("trigger_id", ""), slack)
    elif action_id == "schedule_cancel":
        _handle_schedule_cancel(request, user_id, username, dynamo, slack)

    return {"statusCode": 200, "body": ""}


def _forward_to_bot(payload: dict) -> None:
    """Hands a gnome-bot button click to gnome-bot, already verified, without waiting: Slack wants its 200 in 3s."""
    try:
        _lambda_client.invoke(FunctionName=BOT_INTERACT_FUNCTION_NAME, InvocationType="Event",
                              Payload=json.dumps(payload).encode())
    except Exception:
        logger.warning("Couldn't forward %s to gnome-bot", payload["actions"][0].get("action_id"), exc_info=True)


def _handle_view_submission(payload: dict, dynamo: DynamoClient, slack: SlackClient) -> dict:
    view = payload.get("view", {})
    if view.get("callback_id") != "schedule_modify":
        return {"statusCode": 200, "body": ""}

    metadata = json.loads(view.get("private_metadata", "{}"))
    request_id = metadata.get("request_id")
    if not request_id:
        return {"statusCode": 200, "body": ""}

    state = view.get("state", {}).get("values", {})
    new_timestamp = state.get("new_time_block", {}).get("new_time_input", {}).get("selected_date_time")
    if not new_timestamp:
        return {"statusCode": 200, "body": ""}

    new_time = datetime.fromtimestamp(new_timestamp, tz=timezone.utc)
    request = dynamo.get_request(request_id)
    if not request or request.get("status") != "SCHEDULED":
        return {"statusCode": 200, "body": ""}

    # Both schedules are named after the request, so the old one has to go before the new one can exist.
    lifecycle.delete_schedule(request.get("schedule_name"))
    try:
        new_schedule_name = lifecycle.create_schedule(request_id, new_time)
    except Exception:
        logger.exception("Failed to create updated schedule for request %s; restoring the old time", request_id)
        lifecycle.create_schedule(request_id, lifecycle.scheduled_time(request))
        return {"statusCode": 200, "body": ""}

    username = payload.get("user", {}).get("username") or payload.get("user", {}).get("name", "unknown")
    if dynamo.transition(request_id, "SCHEDULED", "SCHEDULED", f"slack:{username}",
                         note=f"rescheduled to {new_time.isoformat()}",
                         scheduled_time=new_time.isoformat(), schedule_name=new_schedule_name) is None:
        # It fired or was cancelled while the modal was open.
        lifecycle.delete_schedule(new_schedule_name)
        return {"statusCode": 200, "body": ""}

    time_str = new_time.strftime("%Y-%m-%d %H:%M UTC")
    slack.post_thread_reply(
        thread_ts=request["slack_message_ts"],
        text=f":pencil: Schedule updated to *{time_str}*",
    )

    return {"statusCode": 200, "body": ""}


def _already_handled(request: LaunchRequest, username: str, slack: SlackClient):
    current = DynamoClient().get_request(request["request_id"]) or request
    slack.post_thread_reply(
        thread_ts=request["slack_message_ts"],
        text=f"@{username}: nothing done, this request is already {current['status']}",
    )


def _handle_launch_reject(request: LaunchRequest, user_id: str, username: str, dynamo: DynamoClient, slack: SlackClient):
    if dynamo.transition(request["request_id"], "PENDING_APPROVAL", "REJECTED", f"slack:{username}",
                         rejected_by=user_id) is None:
        return _already_handled(request, username, slack)
    slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Strategy launch rejected by @{username}",
        blocks=_build_launch_resolved_blocks(request, f"Rejected by @{username}", "rejected"),
    )


def _handle_launch_approve(request: LaunchRequest, user_id: str, username: str, dynamo: DynamoClient, slack: SlackClient):
    if lifecycle.is_expired(request.get("expires_at")):
        if lifecycle.expire(dynamo, slack, request, "PENDING_APPROVAL", f"slack:{username}") is None:
            return _already_handled(request, username, slack)
        slack.post_thread_reply(thread_ts=request["slack_message_ts"],
                                text=f"@{username}: too late to launch, {lifecycle.expiry_note(request)}")
        return
    if dynamo.transition(request["request_id"], "PENDING_APPROVAL", "APPROVED", f"slack:{username}",
                         approved_by=user_id) is None:
        return _already_handled(request, username, slack)
    slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Strategy launch approved by @{username} — launching...",
        blocks=_build_launch_resolved_blocks(request, f"Approved by @{username} — launching...", "approved"),
    )
    _lambda_client.invoke(
        FunctionName=APPROVE_LAUNCH_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({
            "request_id": request["request_id"],
            "username": username,
        }).encode(),
    )


def _handle_shutdown_approve(request: LaunchRequest, user_id: str, username: str, dynamo: DynamoClient, slack: SlackClient):
    if dynamo.transition(request["request_id"], "PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_APPROVED", f"slack:{username}",
                         approved_by=user_id) is None:
        return _already_handled(request, username, slack)
    slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Shutdown approved by @{username} — stopping...",
        blocks=_build_shutdown_resolved_blocks(request, f"Approved by @{username} — stopping...", "approved"),
    )
    _lambda_client.invoke(
        FunctionName=APPROVE_SHUTDOWN_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({
            "request_id": request["request_id"],
            "username": username,
        }).encode(),
    )


def _handle_shutdown_reject(request: LaunchRequest, user_id: str, username: str, dynamo: DynamoClient, slack: SlackClient):
    if dynamo.transition(request["request_id"], "PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_REJECTED", f"slack:{username}",
                         rejected_by=user_id) is None:
        return _already_handled(request, username, slack)
    slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Shutdown rejected by @{username} — keeping sessions running",
        blocks=_build_shutdown_resolved_blocks(request, f"Rejected by @{username} — keeping running", "rejected"),
    )


def _handle_schedule_modify(request: LaunchRequest, trigger_id: str, slack: SlackClient):
    if request.get("status") != "SCHEDULED":
        return
    current_time_str = request.get("scheduled_time", "")
    current_time = datetime.fromisoformat(current_time_str) if current_time_str else datetime.now(timezone.utc)
    slack.open_schedule_modify_modal(
        trigger_id=trigger_id,
        request_id=request["request_id"],
        current_time=current_time,
    )


def _handle_schedule_cancel(request: LaunchRequest, user_id: str, username: str, dynamo: DynamoClient, slack: SlackClient):
    if dynamo.transition(request["request_id"], "SCHEDULED", "CANCELLED", f"slack:{username}",
                         rejected_by=user_id) is None:
        return _already_handled(request, username, slack)
    lifecycle.delete_schedule(request.get("schedule_name"))
    slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Scheduled launch cancelled by @{username}",
        blocks=_build_schedule_cancelled_blocks(request, username),
    )


def _build_launch_resolved_blocks(request: dict, status_text: str, state: str) -> list[dict]:
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
                {"type": "mrkdwn", "text": f"*Listings:*\n{', '.join(str(x) for x in config.get('listings', []))}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{emoji} {status_text}"}],
        },
    ]


def _build_shutdown_resolved_blocks(request: dict, status_text: str, state: str) -> list[dict]:
    session_ids = request.get("target_session_ids") or []
    emoji = ":octagonal_sign:" if state == "approved" else ":white_check_mark:"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Strategy Shutdown Request"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{request.get('matched_rule_name', '')}"},
                {"type": "mrkdwn", "text": f"*Sessions:*\n{', '.join(session_ids)}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{emoji} {status_text}"}],
        },
    ]


def _build_schedule_cancelled_blocks(request: dict, username: str) -> list[dict]:
    config = request.get("resolved_config", {})
    scheduled_time = request.get("scheduled_time", "unknown")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Strategy Launch Scheduled"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{request.get('matched_rule_name', '')}"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n{config.get('strategy_class', '')} (ID {config.get('strategy_id', '')})"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":x: Cancelled by @{username} (was scheduled for {scheduled_time})"}],
        },
    ]
