import logging

from gnomepy.registry import RegistryClient

from launcher import lifecycle
from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.models import LaunchRequest
from launcher.rules.types import ResolvedStrategyConfig, RuleContext
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)


def handler(event, context):
    """Carries out an approval the Slack interaction already recorded (the request is APPROVED): schedules it if it
    has a time still to come, otherwise launches it."""
    request_id = event["request_id"]
    username = event.get("username", "unknown")
    # Approvals come from Slack or from the controller, which passes its operator as the actor.
    actor = event.get("actor") or f"slack:{username}"
    ctx = RuleContext(registry=RegistryClient(), dynamo=DynamoClient(), slack=SlackClient())

    request = ctx.dynamo.get_request(request_id)
    if not request:
        logger.error("Request not found: %s", request_id)
        return
    when = lifecycle.scheduled_time(request)
    if not lifecycle.is_due(when):
        _schedule(request, when, actor, username, ctx)
    else:
        _launch(request, actor, username, ctx)


def _schedule(request: LaunchRequest, when, actor: str, username: str, ctx: RuleContext):
    request_id = request["request_id"]
    try:
        name = lifecycle.create_schedule(request_id, when)
    except Exception as e:
        logger.exception("Failed to create schedule for request %s", request_id)
        lifecycle.fail(ctx, request, "APPROVED", request["matched_rule_name"], f"Couldn't create the schedule: {e}",
                       actor)
        ctx.slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Strategy launch approved by @{username} — scheduling failed",
            blocks=_build_failed_blocks(request, username, str(e)),
        )
        return
    if ctx.dynamo.transition(request_id, "APPROVED", "SCHEDULED", actor, schedule_name=name) is None:
        lifecycle.delete_schedule(name)
        return
    time_str = when.strftime("%Y-%m-%d %H:%M UTC")
    ctx.slack.update_message(
        message_ts=request["slack_message_ts"],
        text=f"Strategy launch approved by @{username} — scheduled for {time_str}",
        blocks=_build_scheduled_blocks(request, username, time_str),
    )
    ctx.slack.post_thread_reply(
        thread_ts=request["slack_message_ts"],
        text=f":calendar: Approved by @{username} — scheduled for *{time_str}*",
    )


def _launch(request: LaunchRequest, actor: str, username: str, ctx: RuleContext):
    request_id = request["request_id"]
    launching = ctx.dynamo.transition(request_id, "APPROVED", "LAUNCHING", actor)
    if launching is None:
        return
    blocker = lifecycle.blocking_session(ResolvedStrategyConfig(**request["resolved_config"]), ctx)
    if blocker:
        result = lifecycle.fail(ctx, launching, "LAUNCHING", request["matched_rule_name"],
                                f"Session {blocker} already holds these listings", actor)
    else:
        result = lifecycle.launch(ctx, launching, request["matched_rule_name"], f"approved by @{username}", actor)
    if result is None:
        return
    if result["status"] == "LAUNCHED":
        session_url = f"{config.CONTROLLER_BASE_URL}/sessions/{result['session_id']}"
        ctx.slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Strategy launch approved by @{username}",
            blocks=_build_launched_blocks(request, username),
        )
        ctx.slack.post_thread_reply(
            thread_ts=request["slack_message_ts"],
            text=f":rocket: Session launched: <{session_url}|View session>",
        )
    else:
        ctx.slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Strategy launch approved by @{username} — launch failed",
            blocks=_build_failed_blocks(request, username, result.get("launch_error") or ""),
        )
        ctx.slack.post_thread_reply(
            thread_ts=request["slack_message_ts"],
            text=f":x: Launch failed: {result.get('launch_error')}",
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
