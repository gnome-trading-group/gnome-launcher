import hashlib
import json
import logging

from gnomepy.registry import RegistryClient

from launcher.config import config
from launcher.dynamo import DynamoClient
from launcher.rules.classifier_event import ClassifierEventRule  # noqa: F401 — registers rule type
from launcher.rules.direct_launch import DirectLaunchRule  # noqa: F401 — registers rule type
from launcher.rules.types import RULE_TYPE_REGISTRY, RuleContext
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)


def handler(event, context):
    ctx = RuleContext(
        registry=RegistryClient(),
        dynamo=DynamoClient(),
        slack=SlackClient(),
    )
    for record in event["Records"]:
        message = json.loads(record["body"])
        try:
            _process_shutdown_message(message, ctx)
        except Exception:
            logger.exception("Failed to process shutdown message: %s", record.get("messageId"))
            raise


def _process_shutdown_message(message: dict, ctx: RuleContext):
    rule_type = message["rule_type"]
    data = message["data"]

    rules = ctx.dynamo.get_active_rules(rule_type)
    if not rules:
        return

    dedup_key = _compute_dedup_key(rule_type, data)
    min_window = min((r.get("dedup_window_minutes", 60) for r in rules), default=60)
    if ctx.dynamo.find_by_dedup_key(dedup_key, dedup_window_minutes=min_window):
        logger.info("Duplicate shutdown trigger skipped: %s", dedup_key)
        return

    rule_type_cls = RULE_TYPE_REGISTRY.get(rule_type)
    if not rule_type_cls:
        logger.warning("Unknown rule type for shutdown: %s", rule_type)
        return

    for rule in rules:
        try:
            evaluation = rule_type_cls().evaluate_shutdown(data, rule["parameters"], ctx)
        except Exception:
            logger.exception("Shutdown evaluation failed for rule %s", rule.get("rule_id"))
            continue

        if not evaluation or not evaluation.should_shutdown:
            continue

        shutdown_path = rule.get("shutdown_path", "auto")
        if not config.SLACK_INTERACTION_ENABLED:
            shutdown_path = "auto"

        request = ctx.dynamo.create_launch_request(
            rule_type=rule_type,
            data=data,
            dedup_key=dedup_key,
            resolved_config={},
            matched_rule_id=rule["rule_id"],
            matched_rule_name=rule["name"],
            launch_path=shutdown_path,
            status="STOPPING" if shutdown_path == "auto" else "PENDING_SHUTDOWN_APPROVAL",
            action="shutdown",
            target_session_ids=evaluation.target_session_ids,
        )

        if shutdown_path == "auto":
            _auto_shutdown(request, rule, evaluation, ctx)
        else:
            _request_shutdown_approval(request, rule, evaluation, ctx)


def _auto_shutdown(request, rule, evaluation, ctx: RuleContext):
    errors = []
    for session_id in evaluation.target_session_ids:
        try:
            ctx.registry.stop_strategy_session(session_id)
        except Exception as e:
            logger.exception("Failed to stop session %s", session_id)
            errors.append({"session_id": session_id, "error": str(e)})

    if errors:
        ctx.dynamo.update_request(
            request["request_id"],
            status="STOP_FAILED",
            launch_error=json.dumps(errors),
        )
        ctx.slack.send_shutdown_failure_notification(
            rule_name=rule["name"],
            errors=errors,
        )
    else:
        ctx.dynamo.update_request(request["request_id"], status="STOPPED")
        msg_ts = ctx.slack.send_shutdown_notification(
            rule_name=rule["name"],
            reason=evaluation.reason,
            session_ids=evaluation.target_session_ids,
        )
        if msg_ts:
            ctx.dynamo.update_request(
                request["request_id"],
                slack_message_ts=msg_ts,
                slack_channel=ctx.slack.channel,
            )


def _request_shutdown_approval(request, rule, evaluation, ctx: RuleContext):
    message_ts = ctx.slack.send_shutdown_approval_message(
        request_id=request["request_id"],
        rule_name=rule["name"],
        reason=evaluation.reason,
        session_ids=evaluation.target_session_ids,
    )
    ctx.dynamo.update_request(
        request["request_id"],
        slack_message_ts=message_ts,
        slack_channel=ctx.slack.channel,
    )


def _compute_dedup_key(rule_type: str, data: dict) -> str:
    content = json.dumps({"rule_type": rule_type, "action": "shutdown", "data": data}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:24]
