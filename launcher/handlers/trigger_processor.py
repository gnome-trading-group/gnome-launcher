"""Launch triggers, one at a time per rule type (the queue is FIFO, grouped by type): each active rule of the type
decides on its own whether the trigger is a repeat, whether its limits allow a launch, and whether it matches."""
import dataclasses
import json
import logging

from gnomepy.registry import RegistryClient

from launcher import lifecycle
from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.models import LaunchRule
from launcher.rules.catalog import RULE_TYPE_REGISTRY
from launcher.rules.engine import evaluate_rule
from launcher.rules.types import RuleContext, RuleEvaluation
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
            logger.exception("Failed to process message: %s", record.get("messageId"))
            raise


def process(message: dict, ctx: RuleContext) -> None:
    rule_type, data = message["rule_type"], message["data"]
    problems = validate_data(rule_type, data)
    if problems:
        # Retrying can't fix a malformed trigger, so it is logged and dropped rather than sent to the DLQ.
        logger.error("Dropping invalid %s trigger: %s", rule_type, "; ".join(problems))
        return
    key = lifecycle.dedup_key(rule_type, "launch", data)
    for rule in ctx.dynamo.get_active_rules(rule_type):
        if lifecycle.is_repeat(ctx.dynamo, rule, key):
            logger.info("Rule %s already acted on this trigger", rule["name"])
            continue
        evaluation = evaluate_rule(rule, data, ctx)
        if evaluation is None:
            continue
        blocker = lifecycle.blocking_session(evaluation.resolved_config, ctx)
        if blocker:
            logger.info("Rule %s: session %s already holds these listings", rule["name"], blocker)
            continue
        _start(rule, evaluation, data, key, ctx)


def _start(rule: LaunchRule, evaluation: RuleEvaluation, data: dict, key: str, ctx: RuleContext) -> None:
    launch_path = rule["launch_path"] if config.SLACK_INTERACTION_ENABLED else "auto"
    schedule = None
    if rule.get("schedule_enabled"):
        schedule = RULE_TYPE_REGISTRY[rule["rule_type"]]().compute_schedule_time(data, rule["parameters"], ctx)
    if schedule and lifecycle.is_due(schedule.scheduled_time):
        schedule = None

    if launch_path == "approval":
        status = "PENDING_APPROVAL"
    else:
        status = "SCHEDULED" if schedule else "LAUNCHING"
    request = ctx.dynamo.create_launch_request(
        rule_type=rule["rule_type"], data=data, dedup_key=key,
        resolved_config=dataclasses.asdict(evaluation.resolved_config),
        matched_rule_id=rule["rule_id"], matched_rule_name=rule["name"], launch_path=launch_path,
        status=status, actor=lifecycle.AUTO_ACTOR, note=evaluation.reason,
        scheduled_time=schedule.scheduled_time.isoformat() if schedule else None,
    )

    if status == "LAUNCHING":
        lifecycle.launch(ctx, request, rule["name"], evaluation.reason, lifecycle.AUTO_ACTOR)
    elif status == "SCHEDULED":
        _schedule(request, rule, evaluation, schedule, ctx)
    else:
        _request_approval(request, rule, evaluation, schedule, ctx)


def _schedule(request, rule: LaunchRule, evaluation: RuleEvaluation, schedule, ctx: RuleContext) -> None:
    try:
        name = lifecycle.create_schedule(request["request_id"], schedule.scheduled_time)
    except Exception as e:
        logger.exception("Failed to create the schedule for request %s", request["request_id"])
        lifecycle.fail(ctx, request, "SCHEDULED", rule["name"], f"Couldn't create the schedule: {e}", lifecycle.AUTO_ACTOR)
        return
    ctx.dynamo.annotate(request["request_id"], schedule_name=name)
    ts = ctx.slack.send_scheduled_launch_message(
        request_id=request["request_id"], rule_name=rule["name"], reason=evaluation.reason,
        config=evaluation.resolved_config, scheduled_time=schedule.scheduled_time,
        schedule_reason=schedule.reason, launch_path="auto",
    )
    if ts:
        ctx.dynamo.annotate(request["request_id"], slack_message_ts=ts, slack_channel=ctx.slack.channel)


def _request_approval(request, rule: LaunchRule, evaluation: RuleEvaluation, schedule, ctx: RuleContext) -> None:
    try:
        ts = ctx.slack.send_approval_message(
            request_id=request["request_id"], rule_name=rule["name"], reason=evaluation.reason,
            config=evaluation.resolved_config,
            scheduled_time=schedule.scheduled_time if schedule else None,
            schedule_reason=schedule.reason if schedule else None,
        )
    except Exception as e:
        # Nobody would ever see it to approve it; failing it lets the next identical trigger ask again.
        logger.exception("Couldn't post the approval request for %s", request["request_id"])
        lifecycle.fail(ctx, request, "PENDING_APPROVAL", rule["name"], f"Couldn't post the approval request: {e}",
                       lifecycle.AUTO_ACTOR)
        return
    if ts:
        ctx.dynamo.annotate(request["request_id"], slack_message_ts=ts, slack_channel=ctx.slack.channel)
