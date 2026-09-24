import json
import logging
import os
from datetime import datetime

import boto3
import requests

from launcher.rules.types import ResolvedStrategyConfig

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN_SECRET = os.environ.get("SLACK_BOT_TOKEN_SECRET", "slack-bot-token")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")

_cached_token: str | None = None


def _get_token() -> str:
    global _cached_token
    if _cached_token:
        return _cached_token
    sm = boto3.client("secretsmanager")
    response = sm.get_secret_value(SecretId=SLACK_BOT_TOKEN_SECRET)
    _cached_token = response["SecretString"]
    return _cached_token


def _post(method: str, payload: dict) -> dict:
    token = _get_token()
    response = requests.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error ({method}): {data.get('error')}")
    return data


class SlackClient:
    def __init__(self):
        self.channel = SLACK_CHANNEL_ID

    def send_approval_message(
        self,
        request_id: str,
        rule_name: str,
        reason: str,
        config: ResolvedStrategyConfig,
        scheduled_time: datetime | None = None,
        schedule_reason: str | None = None,
    ) -> str:
        blocks = _build_approval_blocks(request_id, rule_name, reason, config, scheduled_time, schedule_reason)
        result = _post("chat.postMessage", {
            "channel": self.channel,
            "text": f"Strategy launch approval needed: {rule_name}",
            "blocks": blocks,
        })
        return result["ts"]

    def send_launch_notification(
        self,
        rule_name: str,
        reason: str,
        config: ResolvedStrategyConfig,
        session_id: str,
        session_url: str,
    ) -> str:
        text = (
            f":rocket: *Auto-launched:* {rule_name}\n"
            f"*Strategy:* {config.strategy_class} (ID {config.strategy_id})\n"
            f"*Mode:* {config.mode} | *Listings:* {', '.join(str(x) for x in config.listings)}\n"
            f"*Reason:* {reason}\n"
            f"<{session_url}|View session>"
        )
        result = _post("chat.postMessage", {
            "channel": self.channel,
            "text": text,
        })
        return result["ts"]

    def send_failure_notification(self, rule_name: str, error: str):
        try:
            _post("chat.postMessage", {
                "channel": self.channel,
                "text": f":x: *Launch failed:* {rule_name}\nError: {error}",
            })
        except Exception:
            logger.warning("Failed to send Slack failure notification", exc_info=True)

    def post_thread_reply(self, thread_ts: str, text: str):
        try:
            _post("chat.postMessage", {
                "channel": self.channel,
                "thread_ts": thread_ts,
                "text": text,
            })
        except Exception:
            logger.warning("Failed to post Slack thread reply", exc_info=True)

    def update_message(self, message_ts: str, text: str, blocks: list | None = None):
        payload: dict = {"channel": self.channel, "ts": message_ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        try:
            _post("chat.update", payload)
        except Exception:
            logger.warning("Failed to update Slack message", exc_info=True)

    def send_shutdown_approval_message(
        self,
        request_id: str,
        rule_name: str,
        reason: str,
        session_ids: list[str],
    ) -> str:
        blocks = _build_shutdown_approval_blocks(request_id, rule_name, reason, session_ids)
        result = _post("chat.postMessage", {
            "channel": self.channel,
            "text": f"Strategy shutdown approval needed: {rule_name}",
            "blocks": blocks,
        })
        return result["ts"]

    def send_shutdown_notification(
        self,
        rule_name: str,
        reason: str,
        session_ids: list[str],
    ) -> str | None:
        text = (
            f":octagonal_sign: *Auto-stopped:* {rule_name}\n"
            f"*Sessions:* {', '.join(session_ids)}\n"
            f"*Reason:* {reason}"
        )
        try:
            result = _post("chat.postMessage", {"channel": self.channel, "text": text})
            return result["ts"]
        except Exception:
            logger.warning("Failed to send shutdown notification", exc_info=True)
            return None

    def send_shutdown_failure_notification(self, rule_name: str, errors: list[dict]):
        try:
            error_text = "\n".join(f"• {e['session_id']}: {e['error']}" for e in errors)
            _post("chat.postMessage", {
                "channel": self.channel,
                "text": f":x: *Shutdown failed:* {rule_name}\n{error_text}",
            })
        except Exception:
            logger.warning("Failed to send shutdown failure notification", exc_info=True)

    def send_scheduled_launch_message(
        self,
        request_id: str,
        rule_name: str,
        reason: str,
        config: ResolvedStrategyConfig,
        scheduled_time: datetime,
        schedule_reason: str,
        launch_path: str,
    ) -> str:
        blocks = _build_scheduled_launch_blocks(
            request_id, rule_name, reason, config, scheduled_time, schedule_reason, launch_path
        )
        result = _post("chat.postMessage", {
            "channel": self.channel,
            "text": f"Strategy launch scheduled: {rule_name}",
            "blocks": blocks,
        })
        return result["ts"]

    def open_schedule_modify_modal(self, trigger_id: str, request_id: str, current_time: datetime):
        _post("views.open", {
            "trigger_id": trigger_id,
            "view": {
                "type": "modal",
                "callback_id": "schedule_modify",
                "private_metadata": json.dumps({"request_id": request_id}),
                "title": {"type": "plain_text", "text": "Modify Launch Time"},
                "submit": {"type": "plain_text", "text": "Update"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"Current scheduled time: *{current_time.strftime('%Y-%m-%d %H:%M UTC')}*",
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "new_time_block",
                        "label": {"type": "plain_text", "text": "New Launch Time (UTC)"},
                        "element": {
                            "type": "datetimepicker",
                            "action_id": "new_time_input",
                            "initial_date_time": int(current_time.timestamp()),
                        },
                    },
                ],
            },
        })


def _build_approval_blocks(
    request_id: str,
    rule_name: str,
    reason: str,
    config: ResolvedStrategyConfig,
    scheduled_time: datetime | None = None,
    schedule_reason: str | None = None,
) -> list[dict]:
    fields = [
        {"type": "mrkdwn", "text": f"*Rule:*\n{rule_name}"},
        {"type": "mrkdwn", "text": f"*Strategy:*\n{config.strategy_class} (ID {config.strategy_id})"},
        {"type": "mrkdwn", "text": f"*Mode:*\n{config.mode}"},
        {"type": "mrkdwn", "text": f"*Listings:*\n{', '.join(str(x) for x in config.listings)}"},
    ]
    if scheduled_time:
        time_str = scheduled_time.strftime("%Y-%m-%d %H:%M UTC")
        fields.append({"type": "mrkdwn", "text": f"*Scheduled Time:*\n{time_str}"})
        if schedule_reason:
            fields.append({"type": "mrkdwn", "text": f"*Why:*\n{schedule_reason}"})

    action_buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve"},
            "style": "primary",
            "action_id": "launch_approve",
            "value": request_id,
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reject"},
            "style": "danger",
            "action_id": "launch_reject",
            "value": request_id,
        },
    ]
    if scheduled_time:
        action_buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Modify Time"},
            "action_id": "schedule_modify",
            "value": request_id,
        })

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Strategy Launch Request"},
        },
        {
            "type": "section",
            "fields": fields,
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason:*\n{reason}"},
        },
        {
            "type": "actions",
            "elements": action_buttons,
        },
    ]


def _build_shutdown_approval_blocks(
    request_id: str,
    rule_name: str,
    reason: str,
    session_ids: list[str],
) -> list[dict]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Strategy Shutdown Request"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{rule_name}"},
                {"type": "mrkdwn", "text": f"*Sessions:*\n{', '.join(session_ids)}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason:*\n{reason}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Stop Sessions"},
                    "style": "danger",
                    "action_id": "shutdown_approve",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Keep Running"},
                    "action_id": "shutdown_reject",
                    "value": request_id,
                },
            ],
        },
    ]


def _build_scheduled_launch_blocks(
    request_id: str,
    rule_name: str,
    reason: str,
    config: ResolvedStrategyConfig,
    scheduled_time: datetime,
    schedule_reason: str,
    launch_path: str,
) -> list[dict]:
    time_str = scheduled_time.strftime("%Y-%m-%d %H:%M UTC")
    approval_note = " (approval required at launch)" if launch_path == "approval" else ""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Strategy Launch Scheduled"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{rule_name}"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n{config.strategy_class} (ID {config.strategy_id})"},
                {"type": "mrkdwn", "text": f"*Mode:*\n{config.mode}"},
                {"type": "mrkdwn", "text": f"*Listings:*\n{', '.join(str(x) for x in config.listings)}"},
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Scheduled Time:*\n{time_str}{approval_note}"},
                {"type": "mrkdwn", "text": f"*Why:*\n{schedule_reason}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Trigger Reason:*\n{reason}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Modify Time"},
                    "action_id": "schedule_modify",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "style": "danger",
                    "action_id": "schedule_cancel",
                    "value": request_id,
                },
            ],
        },
    ]
