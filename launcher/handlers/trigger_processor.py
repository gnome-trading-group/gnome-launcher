import dataclasses
import hashlib
import json
import logging
import uuid

from gnomepy.registry import RegistryClient

from launcher.dynamo import DynamoClient
from launcher.models import LaunchRequest, LaunchRule
from launcher.rules.classifier_event import ClassifierEventRule  # noqa: F401 — registers rule type
from launcher.rules.direct_launch import DirectLaunchRule  # noqa: F401 — registers rule type
from launcher.rules.engine import evaluate_rules
from launcher.rules.types import ResolvedStrategyConfig, RuleContext, RuleMatch
from launcher.config import config
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

        # When Slack interaction is disabled, skip approval and auto-launch everything
        launch_path = match.rule["launch_path"] if config.SLACK_INTERACTION_ENABLED else "auto"

        resolved_dict = dataclasses.asdict(match.evaluation.resolved_config)
        request = ctx.dynamo.create_launch_request(
            rule_type=rule_type,
            data=data,
            dedup_key=dedup_key,
            resolved_config=resolved_dict,
            matched_rule_id=match.rule["rule_id"],
            matched_rule_name=match.rule["name"],
            launch_path=launch_path,
            status="LAUNCHING" if launch_path == "auto" else "PENDING_APPROVAL",
        )

        if launch_path == "auto":
            _auto_launch(request, match, ctx)
        else:
            _request_approval(request, match, ctx)


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


def _request_approval(request: LaunchRequest, match: RuleMatch, ctx: RuleContext):
    resolved_config = match.evaluation.resolved_config
    message_ts = ctx.slack.send_approval_message(
        request_id=request["request_id"],
        rule_name=match.rule["name"],
        reason=match.evaluation.reason,
        config=resolved_config,
    )
    ctx.dynamo.update_request(
        request["request_id"],
        slack_message_ts=message_ts,
        slack_channel=ctx.slack.channel,
    )


def _build_session_config(resolved_config: ResolvedStrategyConfig) -> dict[str, str]:
    result: dict[str, str] = {
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
    return any(s.config.get("listings") == resolved_config.listings for s in sessions)
