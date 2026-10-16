"""
Everything that moves a launch or shutdown request forward, in one place, whichever path got it there: the
trigger processor's auto launches, a Slack (or UI) approval, an EventBridge schedule firing.

Every status change goes through DynamoClient.transition, which only applies it if the request is still where
the caller saw it, so a double-clicked approval, or an approval racing a cancel, acts once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3

from launcher import alerts
from launcher.dynamo import TRACKING_OPEN, DynamoClient
from launcher.models import DEAD_END_STATUSES, LaunchRequest, LaunchRule
from launcher.rules.types import ResolvedStrategyConfig, RuleContext
from launcher.sessions import ACTIVE_SESSION_STATUSES, RULE_ID_KEY, create_session, find_session, listing_ids

logger = logging.getLogger(__name__)

AUTO_ACTOR = "launcher"
SCHEDULED_LAUNCH_FUNCTION_ARN = os.environ.get("SCHEDULED_LAUNCH_FUNCTION_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
DEFAULT_DEDUP_WINDOW_MINUTES = 60


def dedup_key(rule_type: str, action: str, data: dict) -> str:
    content = json.dumps({"rule_type": rule_type, "action": action, "data": data}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:24]


def is_repeat(dynamo: DynamoClient, rule: LaunchRule, key: str) -> bool:
    """Whether this rule already acted on this trigger inside its own dedup window. A request that went nowhere
    (failed, rejected, cancelled) doesn't count, so the next identical trigger retries it."""
    window = rule.get("dedup_window_minutes")
    window = DEFAULT_DEDUP_WINDOW_MINUTES if window is None else window
    if window == 0:
        return False
    return any(r["matched_rule_id"] == rule["rule_id"] and r["status"] not in DEAD_END_STATUSES
               for r in dynamo.requests_with_dedup_key_since(key, window))


# The registry refuses a session whose listings overlap another session of the same strategy and mode that still
# holds them (a stopping session keeps its listings until its process is gone), so it is checked before launching.
def blocking_session(config: ResolvedStrategyConfig, ctx: RuleContext) -> str | None:
    sessions = ctx.registry.get_strategy_sessions(
        strategy_id=config.strategy_id, status=f"{ACTIVE_SESSION_STATUSES},STOPPING")
    target = listing_ids(config.listings)
    for s in sessions:
        if getattr(s, "mode", config.mode) == config.mode and listing_ids(s.config.get("listings", [])) & target:
            return s.session_id
    return None


def launched_by(ctx: RuleContext, rule_id: str, session_ids: list[str]) -> list[str]:
    """The sessions among these that this rule launched."""
    out = []
    for session_id in session_ids:
        session = find_session(ctx.registry, session_id)
        if session is not None and session.config.get(RULE_ID_KEY) == rule_id:
            out.append(session_id)
    return out


def launch(ctx: RuleContext, request: LaunchRequest, rule_name: str, reason: str, actor: str) -> LaunchRequest | None:
    """Creates the session for a request already LAUNCHING. The session id is recorded before the registry is
    called, so a launch that times out after the registry made the session still leads to it."""
    request_id = request["request_id"]
    config = ResolvedStrategyConfig(**request["resolved_config"])
    session_id = str(uuid.uuid4())
    ctx.dynamo.annotate(request_id, session_id=session_id)
    try:
        create_session(ctx.registry, session_id, config, request["matched_rule_id"])
    except Exception as e:
        session = _lookup(ctx, session_id)
        if session is None or session.status == "FAILED":
            error = alerts.describe(e)
            if session is not None and session.failure_reason:
                error = f"{error} ({session.failure_reason})"
            logger.exception("Launch failed for request %s", request_id)
            failed = ctx.dynamo.transition(request_id, "LAUNCHING", "FAILED", actor, note=error, launch_error=error)
            alerts.launch_failed(ctx.registry, rule_name, _failure_key(request), error, config)
            return failed
        logger.warning("Launching %s raised, but the registry has its session %s (%s); treating it as launched",
                       request_id, session_id, session.status, exc_info=True)
    launched = ctx.dynamo.transition(request_id, "LAUNCHING", "LAUNCHED", actor, note=f"session {session_id}",
                                     tracking=TRACKING_OPEN, session_status="SUBMITTED")
    alerts.launched(ctx.registry, rule_name, reason, config, session_id)
    return launched


def fail(ctx: RuleContext, request: LaunchRequest, expected: str, rule_name: str, error: str, actor: str) -> LaunchRequest | None:
    failed = ctx.dynamo.transition(request["request_id"], expected, "FAILED", actor, note=error, launch_error=error)
    if failed is not None:
        config = ResolvedStrategyConfig(**request["resolved_config"]) if request.get("resolved_config") else None
        alerts.launch_failed(ctx.registry, rule_name, _failure_key(request), error, config)
    return failed


def stop(ctx: RuleContext, request: LaunchRequest, rule_name: str, reason: str, actor: str) -> LaunchRequest | None:
    """Stops the sessions of a request already STOPPING. `reason` is recorded on each session as why it stopped."""
    errors = []
    for session_id in request.get("target_session_ids") or []:
        try:
            ctx.registry.stop_strategy_session_as_service(session_id, actor=actor, reason=reason)
        except Exception as e:
            logger.exception("Failed to stop session %s", session_id)
            errors.append({"session_id": session_id, "error": alerts.describe(e)})
    if errors:
        alerts.shutdown_failed(ctx.registry, rule_name, errors)
        return ctx.dynamo.transition(request["request_id"], "STOPPING", "STOP_FAILED", actor,
                                     note=f"{len(errors)} session(s) didn't stop", launch_error=json.dumps(errors))
    alerts.stopped(ctx.registry, rule_name, reason, request.get("target_session_ids") or [])
    return ctx.dynamo.transition(request["request_id"], "STOPPING", "STOPPED", actor, note=reason)


def at_expression(when: datetime) -> str:
    # The schedule's timezone is UTC, so the time must be written in UTC whatever offset it arrived with.
    return f"at({when.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')})"


def create_schedule(request_id: str, when: datetime) -> str:
    name = f"launcher-{request_id}"
    boto3.client("scheduler").create_schedule(
        Name=name,
        ScheduleExpression=at_expression(when),
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": SCHEDULED_LAUNCH_FUNCTION_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"request_id": request_id}),
        },
        ActionAfterCompletion="DELETE",
    )
    return name


def delete_schedule(name: str | None) -> None:
    if not name:
        return
    try:
        boto3.client("scheduler").delete_schedule(Name=name)
    except Exception:
        logger.warning("Failed to delete schedule %s", name, exc_info=True)


def scheduled_time(request: LaunchRequest) -> datetime | None:
    if not request.get("scheduled_time"):
        return None
    when = datetime.fromisoformat(request["scheduled_time"])
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def is_due(when: datetime | None) -> bool:
    return when is None or when <= datetime.now(timezone.utc)


def _lookup(ctx: RuleContext, session_id: str):
    try:
        return find_session(ctx.registry, session_id)
    except Exception:
        logger.warning("Couldn't look up session %s after a failed launch", session_id, exc_info=True)
        return None


def _failure_key(request: LaunchRequest) -> str:
    # The same rule failing on the same trigger is one alert that fires again, not a new one per retry.
    return f"LAUNCH_FAILED:{request['matched_rule_id']}:{request['dedup_key']}"
