import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from gnomepy.registry import RegistryClient

from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.handlers.trigger_processor import _build_session_config
from launcher.models import LaunchRequest
from launcher.rules.types import ResolvedStrategyConfig
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)

SCHEDULED_LAUNCH_FUNCTION_ARN = os.environ.get("SCHEDULED_LAUNCH_FUNCTION_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")

_scheduler_client = boto3.client("scheduler")


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

    scheduled_time_str = request.get("scheduled_time")
    if scheduled_time_str:
        _create_schedule(request_id, scheduled_time_str, dynamo, slack, request, username)
    else:
        _launch_session(request_id, request, registry, dynamo, slack, username)


def _create_schedule(request_id: str, scheduled_time_str: str, dynamo: DynamoClient, slack: SlackClient, request: LaunchRequest, username: str):
    scheduled_time = datetime.fromisoformat(scheduled_time_str)
    if scheduled_time.tzinfo is None:
        scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)

    schedule_name = f"launcher-{request_id}"
    try:
        _scheduler_client.create_schedule(
            Name=schedule_name,
            ScheduleExpression=f"at({scheduled_time.strftime('%Y-%m-%dT%H:%M:%S')})",
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": SCHEDULED_LAUNCH_FUNCTION_ARN,
                "RoleArn": SCHEDULER_ROLE_ARN,
                "Input": json.dumps({"request_id": request_id}),
            },
            ActionAfterCompletion="DELETE",
        )
        dynamo.update_request(request_id, status="SCHEDULED", schedule_name=schedule_name)
        time_str = scheduled_time.strftime("%Y-%m-%d %H:%M UTC")
        slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Strategy launch approved by @{username} — scheduled for {time_str}",
            blocks=_build_scheduled_blocks(request, username, time_str),
        )
        slack.post_thread_reply(
            thread_ts=request["slack_message_ts"],
            text=f":calendar: Approved by @{username} — scheduled for *{time_str}*",
        )
    except Exception as e:
        logger.exception("Failed to create schedule for request %s", request_id)
        dynamo.update_request(request_id, status="FAILED", launch_error=str(e))
        slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Strategy launch approved by @{username} — scheduling failed",
            blocks=_build_failed_blocks(request, username, str(e)),
        )


def _launch_session(request_id: str, request: LaunchRequest, registry: RegistryClient, dynamo: DynamoClient, slack: SlackClient, username: str):
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
    cfg = request.get("resolved_config", {})
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Strategy Launch Request"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{request.get('matched_rule_name', '')}"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n{cfg.get('strategy_class', '')} (ID {cfg.get('strategy_id', '')})"},
                {"type": "mrkdwn", "text": f"*Mode:*\n{cfg.get('mode', '')}"},
                {"type": "mrkdwn", "text": f"*Listings:*\n{', '.join(str(x) for x in cfg.get('listings', []))}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":white_check_mark: Approved by @{username}"}],
        },
    ]


def _build_scheduled_blocks(request: LaunchRequest, username: str, time_str: str) -> list[dict]:
    cfg = request.get("resolved_config", {})
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Strategy Launch Request"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{request.get('matched_rule_name', '')}"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n{cfg.get('strategy_class', '')} (ID {cfg.get('strategy_id', '')})"},
                {"type": "mrkdwn", "text": f"*Mode:*\n{cfg.get('mode', '')}"},
                {"type": "mrkdwn", "text": f"*Listings:*\n{', '.join(str(x) for x in cfg.get('listings', []))}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":calendar: Approved by @{username} — scheduled for {time_str}"}],
        },
    ]


def _build_failed_blocks(request: LaunchRequest, username: str, error: str) -> list[dict]:
    cfg = request.get("resolved_config", {})
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Strategy Launch Request"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{request.get('matched_rule_name', '')}"},
                {"type": "mrkdwn", "text": f"*Strategy:*\n{cfg.get('strategy_class', '')} (ID {cfg.get('strategy_id', '')})"},
                {"type": "mrkdwn", "text": f"*Mode:*\n{cfg.get('mode', '')}"},
                {"type": "mrkdwn", "text": f"*Listings:*\n{', '.join(str(x) for x in cfg.get('listings', []))}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":x: Approved by @{username} — failed: {error}"}],
        },
    ]
