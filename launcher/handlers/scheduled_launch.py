"""An EventBridge schedule firing for a SCHEDULED request. Hours can pass between scheduling and firing, so the
rule and the listings are checked again: a rule disabled since then cancels the launch rather than running it."""
import logging

from gnomepy.registry import RegistryClient

from launcher import lifecycle
from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.rules.types import ResolvedStrategyConfig, RuleContext
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)
ACTOR = "schedule"


def handler(event, context):
    ctx = RuleContext(registry=RegistryClient(), dynamo=DynamoClient(), slack=SlackClient())
    fire(event["request_id"], ctx)


def fire(request_id: str, ctx: RuleContext) -> None:
    request = ctx.dynamo.transition(request_id, "SCHEDULED", "LAUNCHING", ACTOR)
    if request is None:
        logger.info("Request %s is no longer SCHEDULED; not launching", request_id)
        return
    rule_name = request["matched_rule_name"]
    rule = ctx.dynamo.get_rule(request["matched_rule_id"])
    blocker = lifecycle.blocking_session(ResolvedStrategyConfig(**request["resolved_config"]), ctx)
    if rule is None or rule["status"] != "active":
        result = ctx.dynamo.transition(request_id, "LAUNCHING", "CANCELLED", ACTOR,
                                       note=f"rule {rule_name} is no longer active")
    elif blocker:
        result = lifecycle.fail(ctx, request, "LAUNCHING", rule_name, f"Session {blocker} already holds these listings",
                                ACTOR)
    else:
        result = lifecycle.launch(ctx, request, rule_name, "scheduled launch", ACTOR)

    if result is None or not request.get("slack_message_ts"):
        return
    if result["status"] == "LAUNCHED":
        session_url = f"{config.CONTROLLER_BASE_URL}/sessions/{result['session_id']}"
        text = f":rocket: Scheduled launch executed: <{session_url}|View session>"
    elif result["status"] == "CANCELLED":
        text = f":no_entry_sign: Scheduled launch cancelled: rule {rule_name} is no longer active"
    else:
        text = f":x: Scheduled launch failed: {result.get('launch_error')}"
    ctx.slack.post_thread_reply(thread_ts=request["slack_message_ts"], text=text)
