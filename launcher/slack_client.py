import json
import logging
import os

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
    ) -> str:
        blocks = _build_approval_blocks(request_id, rule_name, reason, config)
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
            f"*Mode:* {config.mode} | *Listings:* {config.listings}\n"
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


def _build_approval_blocks(
    request_id: str,
    rule_name: str,
    reason: str,
    config: ResolvedStrategyConfig,
) -> list[dict]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Strategy Launch Request"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{rule_name}"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n{config.strategy_class} (ID {config.strategy_id})"},
                {"type": "mrkdwn", "text": f"*Mode:*\n{config.mode}"},
                {"type": "mrkdwn", "text": f"*Listings:*\n{config.listings}"},
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
            ],
        },
    ]
