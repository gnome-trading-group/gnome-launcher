import logging
import uuid

from gnomepy.registry import RegistryClient

from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.handlers.trigger_processor import _build_session_config
from launcher.models import LaunchRequest
from launcher.rules.types import ResolvedStrategyConfig
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)


def handler(event, context):
    request_id = event["request_id"]
    username = event.get("username", "unknown")

    dynamo = DynamoClient()
    registry = RegistryClient()
    slack = SlackClient()

    request = dynamo.get_request(request_id)
    if not request:
        logger.error("Request not found: %s", request_id)
        return

    resolved_config = ResolvedStrategyConfig(**request["resolved_config"])
    session_id = str(uuid.uuid4())

    try:
        registry.create_strategy_session(
            session_id=session_id,
            strategy_id=resolved_config.strategy_id,
            mode=resolved_config.mode,
            config=_build_session_config(resolved_config),
            research_commit=resolved_config.research_commit,
        )
        dynamo.update_request(request_id, status="LAUNCHED", session_id=session_id)

        session_url = f"{config.CONTROLLER_BASE_URL}/sessions/{session_id}"
        slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Strategy launch approved by @{username}",
            blocks=_build_launched_blocks(request, username),
        )
        slack.post_thread_reply(
            thread_ts=request["slack_message_ts"],
            text=f":rocket: Session launched: <{session_url}|View session>",
        )
    except Exception as e:
        logger.exception("Approve-launch failed for request %s", request_id)
        dynamo.update_request(request_id, status="FAILED", launch_error=str(e))
        slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Strategy launch approved by @{username} — launch failed",
            blocks=_build_failed_blocks(request, username, str(e)),
        )
        slack.post_thread_reply(
            thread_ts=request["slack_message_ts"],
            text=f":x: Launch failed: {e}",
        )


def _build_launched_blocks(request: LaunchRequest, username: str) -> list[dict]:
    config = request.get("resolved_config", {})
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
            "elements": [{"type": "mrkdwn", "text": f":white_check_mark: Approved by @{username}"}],
        },
    ]


def _build_failed_blocks(request: LaunchRequest, username: str, error: str) -> list[dict]:
    config = request.get("resolved_config", {})
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
            "elements": [{"type": "mrkdwn", "text": f":x: Approved by @{username} — launch failed: {error}"}],
        },
    ]
