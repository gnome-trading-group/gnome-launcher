"""Shutdown triggers, one at a time per rule type. Every rule of the type is asked, not only active ones: disabling
a rule stops it launching, and must not leave the sessions it already launched running. A rule only ever stops
sessions it launched itself."""
import json
import logging

from gnomepy.registry import RegistryClient

from launcher import lifecycle
from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.rules.catalog import RULE_TYPE_REGISTRY
from launcher.rules.types import RuleContext
from launcher.rules.validation import validate_data
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)


def handler(event, context):
    ctx = RuleContext(registry=RegistryClient(), dynamo=DynamoClient(), slack=SlackClient())
    for record in event["Records"]:
        message = json.loads(record["body"])
        try:
            process(message, ctx)
        except Exception:
            logger.exception("Failed to process shutdown message: %s", record.get("messageId"))
            raise


def process(message: dict, ctx: RuleContext) -> None:
    rule_type, data = message["rule_type"], message["data"]
    problems = validate_data(rule_type, data)
    if problems:
        logger.error("Dropping invalid %s shutdown trigger: %s", rule_type, "; ".join(problems))
        return
    rule_type_cls = RULE_TYPE_REGISTRY[rule_type]
    key = lifecycle.dedup_key(rule_type, "shutdown", data)
    for rule in ctx.dynamo.get_rules_of_type(rule_type):
        if lifecycle.is_repeat(ctx.dynamo, rule, key):
            continue
        try:
            evaluation = rule_type_cls().evaluate_shutdown(data, rule["parameters"], ctx)
        except Exception:
            logger.exception("Shutdown evaluation failed for rule %s", rule["rule_id"])
            continue
        if not evaluation or not evaluation.should_shutdown:
            continue
        targets = lifecycle.launched_by(ctx, rule["rule_id"], evaluation.target_session_ids)
        if not targets:
            continue

        shutdown_path = (rule.get("shutdown_path") or "auto") if config.SLACK_INTERACTION_ENABLED else "auto"
        request = ctx.dynamo.create_launch_request(
            rule_type=rule_type, data=data, dedup_key=key, resolved_config={},
            matched_rule_id=rule["rule_id"], matched_rule_name=rule["name"], launch_path=shutdown_path,
            status="STOPPING" if shutdown_path == "auto" else "PENDING_SHUTDOWN_APPROVAL",
            actor=lifecycle.AUTO_ACTOR, note=evaluation.reason, action="shutdown", target_session_ids=targets,
        )
        if shutdown_path == "auto":
            lifecycle.stop(ctx, request, rule["name"], evaluation.reason, f"launcher:{rule['name']}")
            continue
        try:
            ts = ctx.slack.send_shutdown_approval_message(
                request_id=request["request_id"], rule_name=rule["name"], reason=evaluation.reason, session_ids=targets)
        except Exception as e:
            logger.exception("Couldn't post the shutdown approval for %s", request["request_id"])
            ctx.dynamo.transition(request["request_id"], "PENDING_SHUTDOWN_APPROVAL", "STOP_FAILED", lifecycle.AUTO_ACTOR,
                                  note=f"Couldn't post the approval request: {e}", launch_error=str(e))
            continue
        if ts:
            ctx.dynamo.annotate(request["request_id"], slack_message_ts=ts, slack_channel=ctx.slack.channel)
