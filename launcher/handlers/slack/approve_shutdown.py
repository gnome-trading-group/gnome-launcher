import json
import logging

from gnomepy.registry import RegistryClient

from launcher import lifecycle
from launcher.dynamo import DynamoClient
from launcher.rules.types import RuleContext
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)


def handler(event, context):
    """Carries out a shutdown approval the Slack interaction already recorded (the request is SHUTDOWN_APPROVED)."""
    request_id = event["request_id"]
    username = event.get("username", "unknown")
    # Approvals come from Slack or from the controller, which passes its operator as the actor.
    actor = event.get("actor") or f"slack:{username}"
    ctx = RuleContext(registry=RegistryClient(), dynamo=DynamoClient(), slack=SlackClient())

    request = ctx.dynamo.transition(request_id, "SHUTDOWN_APPROVED", "STOPPING", actor)
    if request is None:
        logger.info("Shutdown request %s is no longer approved and waiting; nothing to do", request_id)
        return
    result = lifecycle.stop(ctx, request, request["matched_rule_name"], f"approved by @{username}", actor)
    target_session_ids = request.get("target_session_ids") or []
    if result is not None and result["status"] == "STOP_FAILED":
        errors = json.loads(result["launch_error"])
        ctx.slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Shutdown approved by @{username} — stop failed for {len(errors)} session(s)",
            blocks=_build_shutdown_result_blocks(request, username, errors=errors),
        )
    else:
        ctx.slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Shutdown approved by @{username} — {len(target_session_ids)} session(s) stopped",
            blocks=_build_shutdown_result_blocks(request, username),
        )
        ctx.slack.post_thread_reply(
            thread_ts=request["slack_message_ts"],
            text=f":white_check_mark: {len(target_session_ids)} session(s) stopped by @{username}",
        )


def _build_shutdown_result_blocks(request: dict, username: str, errors: list | None = None) -> list[dict]:
    session_ids = request.get("target_session_ids") or []
    rule_name = request.get("matched_rule_name", "")
    if errors:
        status_text = f":x: Approved by @{username} — stop failed for {len(errors)} session(s)"
    else:
        status_text = f":white_check_mark: Approved by @{username} — {len(session_ids)} session(s) stopped"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Strategy Shutdown Request"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Rule:*\n{rule_name}"},
                {"type": "mrkdwn", "text": f"*Sessions:*\n{', '.join(session_ids)}"},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": status_text}],
        },
    ]
