import json
import logging
import os
from datetime import datetime, timezone

import boto3
import requests

from launcher.links import listing_url, request_url, rule_url, session_url, strategy_url
from launcher.models import LaunchRequest
from launcher.rules.types import ResolvedStrategyConfig

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN_SECRET = os.environ.get("SLACK_BOT_TOKEN_SECRET", "slack-bot-token")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")
# Slack refuses a field over 2000 characters or a text over 3000, so a request with many listings or events shows the
# first few and points at the controller, which shows everything.
MAX_TITLES = 3
MAX_LISTINGS = 10
MAX_PAGES = 10
MAX_LABEL = 80

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
    """Posts to the launcher's channel. With no channel configured (dev) every call is a no-op and the send_*
    methods return None, so a stage without Slack runs the same code."""

    def __init__(self):
        self.channel = SLACK_CHANNEL_ID

    def send_approval_message(
        self,
        request_id: str,
        rule_name: str,
        rule_id: str,
        reason: str,
        config: ResolvedStrategyConfig,
        links: dict,
        scheduled_time: datetime | None = None,
        schedule_reason: str | None = None,
        expires_at: datetime | None = None,
        deadline_label: str = "deadline",
    ) -> str | None:
        if not self.channel:
            return None
        blocks = _build_approval_blocks(request_id, rule_name, rule_id, reason, config, links, scheduled_time,
                                        schedule_reason, expires_at, deadline_label)
        result = _post("chat.postMessage", {
            "channel": self.channel,
            "text": f"Strategy launch approval needed: {rule_name}",
            "blocks": blocks,
        })
        return result["ts"]

    def refresh_approval_message(self, request: LaunchRequest, deadline_label: str) -> None:
        """Rebuilds a pending request's approval message from the request as stored, after its deadline moved."""
        scheduled = request.get("scheduled_time")
        expires_at = request.get("expires_at")
        blocks = _build_approval_blocks(
            request["request_id"], request["matched_rule_name"], request["matched_rule_id"], request_reason(request),
            ResolvedStrategyConfig(**request["resolved_config"]), request.get("links") or {},
            datetime.fromisoformat(scheduled) if scheduled else None, None,
            datetime.fromisoformat(expires_at) if expires_at else None, deadline_label)
        self.update_message(request.get("slack_message_ts"),
                            f"Strategy launch approval needed: {request['matched_rule_name']}", blocks=blocks)

    def post_thread_reply(self, thread_ts: str | None, text: str):
        if not self.channel or not thread_ts:
            return
        try:
            _post("chat.postMessage", {
                "channel": self.channel,
                "thread_ts": thread_ts,
                "text": text,
            })
        except Exception:
            logger.warning("Failed to post Slack thread reply", exc_info=True)

    def update_message(self, message_ts: str | None, text: str, blocks: list | None = None):
        if not self.channel or not message_ts:
            return
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
        rule_id: str,
        reason: str,
        session_ids: list[str],
        links: dict,
    ) -> str | None:
        if not self.channel:
            return None
        blocks = _build_shutdown_approval_blocks(request_id, rule_name, rule_id, reason, session_ids, links)
        result = _post("chat.postMessage", {
            "channel": self.channel,
            "text": f"Strategy shutdown approval needed: {rule_name}",
            "blocks": blocks,
        })
        return result["ts"]

    def send_scheduled_launch_message(
        self,
        request_id: str,
        rule_name: str,
        rule_id: str,
        reason: str,
        config: ResolvedStrategyConfig,
        links: dict,
        scheduled_time: datetime,
        schedule_reason: str,
        launch_path: str,
    ) -> str | None:
        if not self.channel:
            return None
        blocks = _build_scheduled_launch_blocks(
            request_id, rule_name, rule_id, reason, config, links, scheduled_time, schedule_reason, launch_path
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


def _button(text: str, action_id: str, request_id: str, style: str | None = None) -> dict:
    button = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id, "value": request_id}
    if style:
        button["style"] = style
    return button


def _build_approval_blocks(
    request_id: str,
    rule_name: str,
    rule_id: str,
    reason: str,
    config: ResolvedStrategyConfig,
    links: dict,
    scheduled_time: datetime | None = None,
    schedule_reason: str | None = None,
    expires_at: datetime | None = None,
    deadline_label: str = "deadline",
) -> list[dict]:
    fields = _launch_fields(rule_name, rule_id, config, links)
    if scheduled_time:
        fields.append(_field("Launches at", format_time(scheduled_time)))
        if schedule_reason:
            fields.append(_field("Why then", schedule_reason))
    if expires_at:
        fields.append(_field("Expires", f"at {deadline_label}, {format_time(expires_at)}"))
    buttons = [_button("Approve", "launch_approve", request_id, "primary"),
               _button("Reject", "launch_reject", request_id, "danger")]
    if scheduled_time:
        buttons.append(_button("Modify time", "schedule_modify", request_id))
    return _message(":rocket: Launch approval needed", reason, fields, links, request_id, buttons)


def _build_shutdown_approval_blocks(
    request_id: str,
    rule_name: str,
    rule_id: str,
    reason: str,
    session_ids: list[str],
    links: dict,
) -> list[dict]:
    fields = [_rule_field(rule_name, rule_id)]
    if links.get("strategy"):
        fields.append(_field("Strategy", _anchor(links["strategy"]["label"], links["strategy"]["url"])))
    fields.append(_field("Sessions", "\n".join(_capped([_anchor(sid, session_url(sid)) for sid in session_ids],
                                                         MAX_LISTINGS))))
    buttons = [_button("Stop sessions", "shutdown_approve", request_id, "danger"),
               _button("Keep running", "shutdown_reject", request_id)]
    return _message(":octagonal_sign: Shutdown approval needed", reason, fields, links, request_id, buttons)


def _build_scheduled_launch_blocks(
    request_id: str,
    rule_name: str,
    rule_id: str,
    reason: str,
    config: ResolvedStrategyConfig,
    links: dict,
    scheduled_time: datetime,
    schedule_reason: str,
    launch_path: str,
) -> list[dict]:
    approval_note = " (approval required at launch)" if launch_path == "approval" else ""
    fields = _launch_fields(rule_name, rule_id, config, links)
    fields.append(_field("Launches at", format_time(scheduled_time) + approval_note))
    fields.append(_field("Why then", schedule_reason))
    buttons = [_button("Modify time", "schedule_modify", request_id),
               _button("Cancel", "schedule_cancel", request_id, "danger")]
    return _message(":calendar: Launch scheduled", reason, fields, links, request_id, buttons)


def _message(title: str, reason: str, fields: list[dict], links: dict, request_id: str, buttons: list[dict]) -> list[dict]:
    """Title, the trigger's reason as the headline (with what it trades), the request's facts, its pages on one line,
    then the buttons."""
    titles = [_escape(_clip(title)) for title in links.get("titles") or []]
    headline = "\n".join([f"*{_escape(reason)}*", *_capped(titles, MAX_TITLES)])
    return [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": headline}},
        {"type": "section", "fields": fields},
        _links_context(links, request_id),
        {"type": "divider"},
        {"type": "actions", "elements": buttons},
    ]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LABEL else text[:MAX_LABEL - 1].rstrip() + "…"


def _anchor(label: str, url: str) -> str:
    return f"<{url}|{_escape(_clip(label))}>"


def _capped(lines: list[str], limit: int) -> list[str]:
    return lines if len(lines) <= limit else lines[:limit] + [f"_+{len(lines) - limit} more in the controller_"]


def _field(name: str, value: str) -> dict:
    return {"type": "mrkdwn", "text": f"*{name}*\n{value}"}


def format_time(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def request_reason(request: LaunchRequest) -> str:
    """Why the request was made: the note its first event was recorded with."""
    events = request.get("events") or [{}]
    return events[0].get("note") or request["matched_rule_name"]


def _rule_field(rule_name: str, rule_id: str) -> dict:
    return _field("Rule", _anchor(rule_name, rule_url(rule_id)))


def _launch_fields(rule_name: str, rule_id: str, config: ResolvedStrategyConfig, links: dict) -> list[dict]:
    strategy = links.get("strategy") or {"label": f"Strategy {config.strategy_id}", "url": strategy_url(config.strategy_id)}
    # A request made before links were stored still links its listings, by id.
    listings = links.get("listings") or [{"label": str(lid), "url": listing_url(lid)} for lid in config.listings]
    return [
        _rule_field(rule_name, rule_id),
        _field("Strategy", _anchor(strategy["label"], strategy["url"])),
        _field("Mode", config.mode),
        _field("Listings", "\n".join(_capped([_anchor(item["label"], item["url"]) for item in listings], MAX_LISTINGS))),
    ]


def _links_context(links: dict, request_id: str) -> dict:
    pages = _capped([_anchor(page["label"], page["url"]) for page in links.get("pages") or []], MAX_PAGES)
    pages.append(_anchor("Open in controller", request_url(request_id)))
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(pages)}]}
