import logging
import uuid

from gnomepy.registry import RegistryClient

from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.handlers.trigger_processor import _build_session_config
from launcher.rules.types import ResolvedStrategyConfig
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)


def handler(event, context):
    request_id = event["request_id"]

    dynamo = DynamoClient()
    registry = RegistryClient()
    slack = SlackClient()

    request = dynamo.get_request(request_id)
    if not request:
        logger.error("Scheduled request not found: %s", request_id)
        return

    if request.get("status") != "SCHEDULED":
        logger.info("Request %s is no longer SCHEDULED (status=%s), skipping", request_id, request.get("status"))
        return

    dynamo.update_request(request_id, status="LAUNCHING")
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
        if request.get("slack_message_ts"):
            slack.post_thread_reply(
                thread_ts=request["slack_message_ts"],
                text=f":rocket: Scheduled launch executed: <{session_url}|View session>",
            )
    except Exception as e:
        logger.exception("Scheduled launch failed for request %s", request_id)
        dynamo.update_request(request_id, status="FAILED", launch_error=str(e))
        if request.get("slack_message_ts"):
            slack.post_thread_reply(
                thread_ts=request["slack_message_ts"],
                text=f":x: Scheduled launch failed: {e}",
            )
