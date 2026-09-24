import dataclasses
import hashlib
import json
import logging
import os
import uuid
from typing import Any

import boto3
from gnomepy.registry import RegistryClient

from launcher.dynamo import DynamoClient
from launcher.models import LaunchRequest, LaunchRule
from launcher.rules.classifier_event import ClassifierEventRule  # noqa: F401 — registers rule type
from launcher.rules.direct_launch import DirectLaunchRule  # noqa: F401 — registers rule type
from launcher.rules.engine import evaluate_rules
from launcher.rules.types import RULE_TYPE_REGISTRY, ResolvedStrategyConfig, RuleContext, RuleMatch, ScheduleResult
from launcher.config import config
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)

SCHEDULED_LAUNCH_FUNCTION_ARN = os.environ.get("SCHEDULED_LAUNCH_FUNCTION_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")

_scheduler_client = boto3.client("scheduler")


def handler(event, context):
    ctx = RuleContext(
        registry=RegistryClient(),
        dynamo=DynamoClient(),
        slack=SlackClient(),
    )
    for record in event["Records"]:
        message = json.loads(record["body"])
        try:
            _process_message(message, ctx)
        except Exception:
            logger.exception("Failed to process message: %s", record.get("messageId"))
            raise


def _process_message(message: dict, ctx: RuleContext):
    rule_type = message["rule_type"]
    data = message["data"]

    dedup_key = _compute_dedup_key(rule_type, data)

    rules = ctx.dynamo.get_active_rules(rule_type)
    if not rules:
        return

    # Check dedup against the minimum window across all matched rules
    min_window = min((r.get("dedup_window_minutes", 60) for r in rules), default=60)
    existing = ctx.dynamo.find_by_dedup_key(dedup_key, dedup_window_minutes=min_window)
    if existing:
        logger.info("Duplicate trigger skipped: %s", dedup_key)
        return

    matches = evaluate_rules(rules, data, ctx)
    if not matches:
        return

    for match in matches:
        if _has_active_duplicate(match.evaluation.resolved_config, ctx):
            logger.info(
                "Active session already exists for rule %s, skipping",
                match.rule.get("name"),
            )
            continue

        launch_path = match.rule["launch_path"] if config.SLACK_INTERACTION_ENABLED else "auto"
        resolved_dict = dataclasses.asdict(match.evaluation.resolved_config)

        schedule_enabled = match.rule.get("schedule_enabled", False)
        schedule_result = None
        if schedule_enabled:
            rule_type_cls = RULE_TYPE_REGISTRY.get(match.rule["rule_type"])
            if rule_type_cls:
                schedule_result = rule_type_cls().compute_schedule_time(
                    data, match.rule["parameters"], ctx
                )

        if schedule_result and launch_path == "auto":
            _schedule_launch(rule_type, dedup_key, data, match, resolved_dict, schedule_result, ctx)
        else:
            scheduled_time_iso = schedule_result.scheduled_time.isoformat() if schedule_result else None
            request = ctx.dynamo.create_launch_request(
                rule_type=rule_type,
                data=data,
                dedup_key=dedup_key,
                resolved_config=resolved_dict,
                matched_rule_id=match.rule["rule_id"],
                matched_rule_name=match.rule["name"],
                launch_path=launch_path,
                status="LAUNCHING" if launch_path == "auto" else "PENDING_APPROVAL",
                scheduled_time=scheduled_time_iso,
            )
            if launch_path == "auto":
                _auto_launch(request, match, ctx)
            else:
                _request_approval(request, match, ctx, schedule_result)


def _schedule_launch(rule_type, dedup_key, data, match, resolved_dict, schedule_result: ScheduleResult, ctx: RuleContext):
    request = ctx.dynamo.create_launch_request(
        rule_type=rule_type,
        data=data,
        dedup_key=dedup_key,
        resolved_config=resolved_dict,
        matched_rule_id=match.rule["rule_id"],
        matched_rule_name=match.rule["name"],
        launch_path="auto",
        status="SCHEDULED",
        scheduled_time=schedule_result.scheduled_time.isoformat(),
    )

    schedule_name = f"launcher-{request['request_id']}"
    try:
        _scheduler_client.create_schedule(
            Name=schedule_name,
            ScheduleExpression=f"at({schedule_result.scheduled_time.strftime('%Y-%m-%dT%H:%M:%S')})",
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": SCHEDULED_LAUNCH_FUNCTION_ARN,
                "RoleArn": SCHEDULER_ROLE_ARN,
                "Input": json.dumps({"request_id": request["request_id"]}),
            },
            ActionAfterCompletion="DELETE",
        )
        ctx.dynamo.update_request(request["request_id"], schedule_name=schedule_name)
    except Exception:
        logger.exception("Failed to create EventBridge schedule for request %s", request["request_id"])
        ctx.dynamo.update_request(request["request_id"], status="FAILED", launch_error="Failed to create schedule")
        ctx.slack.send_failure_notification(rule_name=match.rule["name"], error="Failed to create EventBridge schedule")
        return

    msg_ts = ctx.slack.send_scheduled_launch_message(
        request_id=request["request_id"],
        rule_name=match.rule["name"],
        reason=match.evaluation.reason,
        config=match.evaluation.resolved_config,
        scheduled_time=schedule_result.scheduled_time,
        schedule_reason=schedule_result.reason,
        launch_path=launch_path,
    )
    ctx.dynamo.update_request(
        request["request_id"],
        slack_message_ts=msg_ts,
        slack_channel=ctx.slack.channel,
    )


def _auto_launch(request: LaunchRequest, match: RuleMatch, ctx: RuleContext):
    resolved_config = match.evaluation.resolved_config
    session_id = str(uuid.uuid4())
    try:
        ctx.registry.create_strategy_session(
            session_id=session_id,
            strategy_id=resolved_config.strategy_id,
            mode=resolved_config.mode,
            config=_build_session_config(resolved_config),
            research_commit=resolved_config.research_commit,
        )
        ctx.dynamo.update_request(request["request_id"], status="LAUNCHED", session_id=session_id)
        session_url = f"{config.CONTROLLER_BASE_URL}/sessions/{session_id}"
        msg_ts = ctx.slack.send_launch_notification(
            rule_name=match.rule["name"],
            reason=match.evaluation.reason,
            config=resolved_config,
            session_id=session_id,
            session_url=session_url,
        )
        ctx.dynamo.update_request(
            request["request_id"],
            slack_message_ts=msg_ts,
            slack_channel=ctx.slack.channel,
        )
    except Exception as e:
        logger.exception("Launch failed for request %s", request["request_id"])
        ctx.dynamo.update_request(
            request["request_id"], status="FAILED", launch_error=str(e),
        )
        ctx.slack.send_failure_notification(rule_name=match.rule["name"], error=str(e))


def _request_approval(request: LaunchRequest, match: RuleMatch, ctx: RuleContext, schedule_result: ScheduleResult | None = None):
    resolved_config = match.evaluation.resolved_config
    message_ts = ctx.slack.send_approval_message(
        request_id=request["request_id"],
        rule_name=match.rule["name"],
        reason=match.evaluation.reason,
        config=resolved_config,
        scheduled_time=schedule_result.scheduled_time if schedule_result else None,
        schedule_reason=schedule_result.reason if schedule_result else None,
    )
    ctx.dynamo.update_request(
        request["request_id"],
        slack_message_ts=message_ts,
        slack_channel=ctx.slack.channel,
    )


def _build_session_config(resolved_config: ResolvedStrategyConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "strategy.id": str(resolved_config.strategy_id),
        "mode": resolved_config.mode,
        "listings": resolved_config.listings,
        "strategy.type": resolved_config.strategy_type,
        "strategy.class": resolved_config.strategy_class,
    }
    for k, v in resolved_config.strategy_args.items():
        result[f"strategy.args.{k}"] = v
    for k, v in resolved_config.simulation_config.items():
        result[k] = v
    return result


def _compute_dedup_key(rule_type: str, data: dict) -> str:
    content = json.dumps({"rule_type": rule_type, "data": data}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:24]


def _has_active_duplicate(resolved_config: ResolvedStrategyConfig, ctx: RuleContext) -> bool:
    sessions = ctx.registry.get_strategy_sessions(strategy_id=resolved_config.strategy_id, status="RUNNING")
    target = set(resolved_config.listings)
    return any(set(s.config.get("listings", [])) == target for s in sessions)
