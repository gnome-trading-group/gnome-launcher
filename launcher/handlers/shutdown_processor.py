"""Shutdown triggers, one at a time per rule type. Every rule of the type is asked, not only active ones: disabling
a rule stops it launching, and must not leave the sessions it already launched running. A rule only ever stops
sessions it launched itself."""
import json
import logging
import uuid

from gnomepy.registry import RegistryClient

from launcher import alerts, lifecycle
from launcher.dynamo import DynamoClient
from launcher.links import request_links
from launcher.rules.catalog import RULE_TYPE_REGISTRY
from launcher.rules.types import NoMatch, RuleContext
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
    trigger = {**message, "action": "shutdown", "trigger_id": message.get("trigger_id") or str(uuid.uuid4())}
    rule_type, data = trigger["rule_type"], trigger["data"]
    problems = validate_data(rule_type, data)
    if problems:
        logger.error("Dropping invalid %s shutdown trigger: %s", rule_type, "; ".join(problems))
        ctx.dynamo.record_evaluation(trigger, "INVALID", "; ".join(problems))
        return
    rules = ctx.dynamo.get_rules_of_type(rule_type)
    if not rules:
        ctx.dynamo.record_evaluation(trigger, "NO_RULES", f"no {rule_type} rule")
        return
    rule_type_cls = RULE_TYPE_REGISTRY[rule_type]
    key = lifecycle.dedup_key(rule_type, "shutdown", data)
    for rule in rules:
        if lifecycle.is_repeat(ctx.dynamo, rule, key):
            ctx.dynamo.record_evaluation(trigger, "DUPLICATE", "already acted on this trigger inside the dedup window", rule)
            continue
        try:
            evaluation = rule_type_cls().evaluate_shutdown(data, rule["parameters"], ctx)
        except Exception as e:
            logger.exception("Shutdown evaluation failed for rule %s", rule["rule_id"])
            ctx.dynamo.record_evaluation(trigger, "ERROR", alerts.describe(e), rule)
            continue
        if isinstance(evaluation, NoMatch):
            ctx.dynamo.record_evaluation(trigger, "NO_MATCH", evaluation.reason, rule)
            continue
        targets = lifecycle.launched_by(ctx, rule["rule_id"], evaluation.target_session_ids)
        if not targets:
            ctx.dynamo.record_evaluation(trigger, "NO_MATCH", "none of the matching sessions were launched by this rule", rule)
            continue

        shutdown_path = rule.get("shutdown_path") or "auto"
        links = request_links(rule_type, data, rule["parameters"].get("strategy_id"), [], ctx.registry)
        request = ctx.dynamo.create_launch_request(
            rule_type=rule_type, data=data, dedup_key=key, resolved_config={},
            matched_rule_id=rule["rule_id"], matched_rule_name=rule["name"], launch_path=shutdown_path,
            status="STOPPING" if shutdown_path == "auto" else "PENDING_SHUTDOWN_APPROVAL",
            actor=lifecycle.AUTO_ACTOR, note=evaluation.reason, action="shutdown", target_session_ids=targets,
            trigger_id=trigger["trigger_id"], rule_revision=rule.get("revision"), links=links,
        )
        ctx.dynamo.record_evaluation(trigger, "REQUESTED", evaluation.reason, rule, request["request_id"])
        if shutdown_path == "auto":
            lifecycle.stop(ctx, request, rule["name"], evaluation.reason, f"launcher:{rule['name']}")
            continue
        try:
            ts = ctx.slack.send_shutdown_approval_message(
                request_id=request["request_id"], rule_name=rule["name"], rule_id=rule["rule_id"],
                reason=evaluation.reason, session_ids=targets, links=links)
        except Exception:
            logger.exception("Couldn't post the shutdown approval for %s to Slack", request["request_id"])
            continue
        if ts:
            ctx.dynamo.annotate(request["request_id"], slack_message_ts=ts, slack_channel=ctx.slack.channel)
