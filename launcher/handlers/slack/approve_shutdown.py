import json
import logging

from gnomepy.registry import RegistryClient

from launcher.dynamo import DynamoClient
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
        logger.error("Shutdown request not found: %s", request_id)
        return

    target_session_ids = request.get("target_session_ids") or []
    errors = []
    for session_id in target_session_ids:
        try:
            registry.stop_strategy_session(session_id)
        except Exception as e:
            logger.exception("Failed to stop session %s", session_id)
            errors.append({"session_id": session_id, "error": str(e)})

    rule_name = request.get("matched_rule_name", "")
    if errors:
        dynamo.update_request(request_id, status="STOP_FAILED", launch_error=json.dumps(errors))
        slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Shutdown approved by @{username} — stop failed for {len(errors)} session(s)",
            blocks=_build_shutdown_result_blocks(request, username, errors=errors),
        )
    else:
        dynamo.update_request(request_id, status="STOPPED")
        slack.update_message(
            message_ts=request["slack_message_ts"],
            text=f"Shutdown approved by @{username} — {len(target_session_ids)} session(s) stopped",
            blocks=_build_shutdown_result_blocks(request, username),
        )
        slack.post_thread_reply(
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
