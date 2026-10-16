"""Every few minutes: brings launch requests up to date with what happened after the launcher acted.

- A launched request follows its session: the sweeper copies the session's status, start, end and failure reason
  from the registry until the session has ended, so a request says how its session went, not just that it began.
- A request stuck between steps (a Lambda that died after moving it to LAUNCHING, APPROVED or STOPPING) is
  resolved: a LAUNCHING one becomes LAUNCHED if the registry has its session, otherwise it fails.
- An approval nobody acted on expires, so a stale launch can't be approved hours after its moment has passed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from gnomepy.registry import RegistryClient

from launcher import lifecycle
from launcher.dynamo import TRACKING_OPEN, DynamoClient
from launcher.models import LaunchRequest
from launcher.rules.types import RuleContext
from launcher.sessions import find_session
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)

ACTOR = "sweeper"
ENDED_SESSION_STATUSES = ("STOPPED", "FAILED")
STUCK_AFTER = timedelta(minutes=15)
APPROVAL_EXPIRES_AFTER = timedelta(hours=24)


def handler(event, context):
    sweep(RuleContext(registry=RegistryClient(), dynamo=DynamoClient(), slack=SlackClient()))


def sweep(ctx: RuleContext, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    for request in ctx.dynamo.tracked_requests():
        _follow_session(ctx, request)
    for request in ctx.dynamo.requests_in_status("LAUNCHING"):
        if _idle(request, now, STUCK_AFTER):
            _resolve_launching(ctx, request)
    for request in ctx.dynamo.requests_in_status("APPROVED"):
        if _idle(request, now, STUCK_AFTER):
            lifecycle.fail(ctx, request, "APPROVED", request["matched_rule_name"],
                           "approved, but the launch was never carried out", ACTOR)
    for request in ctx.dynamo.requests_in_status("STOPPING"):
        if _idle(request, now, STUCK_AFTER):
            ctx.dynamo.transition(request["request_id"], "STOPPING", "STOP_FAILED", ACTOR,
                                  note="the stop was never carried out", launch_error="the stop was never carried out")
    for status, expired in (("PENDING_APPROVAL", "CANCELLED"), ("PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_REJECTED")):
        for request in ctx.dynamo.requests_in_status(status):
            if _idle(request, now, APPROVAL_EXPIRES_AFTER):
                if ctx.dynamo.transition(request["request_id"], status, expired, ACTOR,
                                         note=f"not approved within {APPROVAL_EXPIRES_AFTER}") is not None:
                    ctx.slack.update_message(request.get("slack_message_ts"),
                                             f"Expired: not approved within {APPROVAL_EXPIRES_AFTER}")


def _idle(request: LaunchRequest, now: datetime, after: timedelta) -> bool:
    return now - datetime.fromisoformat(request["date_modified"]) > after


def _follow_session(ctx: RuleContext, request: LaunchRequest) -> None:
    try:
        session = find_session(ctx.registry, request["session_id"])
    except Exception:
        logger.warning("Couldn't read session %s", request["session_id"], exc_info=True)
        return
    if session is None:
        ctx.dynamo.record_session(request["request_id"], ended=True, session_status="MISSING")
        return
    ctx.dynamo.record_session(
        request["request_id"], ended=session.status in ENDED_SESSION_STATUSES,
        session_status=session.status, session_started_at=session.started_at,
        session_stopped_at=session.stopped_at, session_failure_reason=session.failure_reason,
    )


def _resolve_launching(ctx: RuleContext, request: LaunchRequest) -> None:
    session = find_session(ctx.registry, request["session_id"]) if request.get("session_id") else None
    if session is not None and session.status != "FAILED":
        ctx.dynamo.transition(request["request_id"], "LAUNCHING", "LAUNCHED", ACTOR,
                              note=f"found session {session.session_id} after the launch was cut short",
                              tracking=TRACKING_OPEN, session_status=session.status)
        return
    reason = session.failure_reason if session is not None else None
    lifecycle.fail(ctx, request, "LAUNCHING", request["matched_rule_name"],
                   f"the launch was cut short{f': {reason}' if reason else ''}", ACTOR)
