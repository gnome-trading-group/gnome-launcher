"""Launch rules for the controller.

    GET    /launch-rules                         every rule, archived ones included
    POST   /launch-rules                         create
    POST   /launch-rules/validate                {rule} -> {problems}, without saving
    GET    /launch-rules/{id}
    PATCH  /launch-rules/{id}                    edit; pass the `revision` you read to refuse a lost update
    DELETE /launch-rules/{id}                    archive
    GET    /launch-rules/{id}/revisions          every saved version, newest first
    GET    /launch-rules/{id}/activity           its trigger log, newest first; page with before=
    GET    /launch-rules/{id}/stats?days=        launches, outcomes and the sessions it has running
    POST   /launch-rules/{id}/dry-run            {data, action?} -> what it would do now, without doing it

Every write is validated as a whole rule (an edit is merged into the stored rule first), so a rule that would fail
when its next trigger arrives is refused when saved. Rules are archived rather than deleted: launch history keeps
pointing at them, and the shutdown processor still stops the sessions an archived rule launched.
"""
import dataclasses
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from gnomepy.registry import RegistryClient

from launcher import lifecycle
from launcher.dynamo import DynamoClient, RevisionConflict
from launcher.handlers.api.http import actor, api_handler, body_of, error, ok, query_int
from launcher.rules.catalog import RULE_TYPE_REGISTRY
from launcher.rules.engine import rate_limit
from launcher.rules.types import NoMatch, RuleContext
from launcher.rules.validation import EDITABLE_FIELDS, RULE_DEFAULTS, validate_data, validate_rule
from launcher.sessions import ACTIVE_SESSION_STATUSES, RULE_ID_KEY, build_session_config
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)

STORED_FIELDS = ("rule_id", "revision", "date_created", "date_modified", "created_by", "modified_by")
MAX_PAGE = 200
MAX_STATS_DAYS = 90


@api_handler
def handler(event, context):
    dynamo = DynamoClient()
    method = event.get("httpMethod", "")
    params = event.get("pathParameters") or {}
    rule_id, view = params.get("id"), params.get("view")
    path = event.get("resource", "")

    if method == "POST" and path.endswith("/validate"):
        body = body_of(event)
        if body is None or not isinstance(body.get("rule"), dict):
            return error(400, "Expected {rule: {...}}")
        return ok({"problems": validate_rule({**RULE_DEFAULTS, **body["rule"]})})

    if method == "GET" and not rule_id:
        return ok(dynamo.list_rules())

    if method == "POST" and not rule_id:
        body = body_of(event)
        if body is None:
            return error(400, "The body must be a JSON object")
        rule = {**RULE_DEFAULTS, **body}
        problems = validate_rule(rule)
        if problems:
            return error(400, "Invalid rule", problems)
        return ok(dynamo.create_rule(rule, actor(event)), 201)

    rule = dynamo.get_rule(rule_id)
    if rule is None:
        return error(404, f"Rule not found: {rule_id}")

    if method == "GET" and view is None:
        return ok(rule)
    if method == "GET" and view == "revisions":
        return ok(dynamo.list_revisions(rule_id))
    if method == "GET" and view == "activity":
        limit = query_int(event, "limit", 50, MAX_PAGE)
        if limit is None:
            return error(400, f"limit must be between 1 and {MAX_PAGE}")
        before = (event.get("queryStringParameters") or {}).get("before")
        items = dynamo.evaluations(rule_id=rule_id, before=before, limit=limit)
        return ok({"items": items, "next_before": items[-1]["received_at"] if len(items) == limit else None})
    if method == "GET" and view == "stats":
        days = query_int(event, "days", 7, MAX_STATS_DAYS)
        if days is None:
            return error(400, f"days must be between 1 and {MAX_STATS_DAYS}")
        return ok(_stats(rule, days, dynamo))
    if method == "POST" and view == "dry-run":
        return _dry_run(rule, body_of(event), dynamo)
    if method == "PATCH" and view is None:
        return _edit(rule, body_of(event), actor(event), dynamo)
    if method == "DELETE" and view is None:
        return _save(rule, {"status": "archived"}, actor(event), dynamo)
    return error(400, f"Unsupported: {method} {path}")


def _edit(rule: dict, body: dict | None, who: str, dynamo: DynamoClient):
    if body is None:
        return error(400, "The body must be a JSON object")
    read_revision = body.pop("revision", rule["revision"])
    if read_revision != rule["revision"]:
        return error(409, f"The rule has changed since revision {read_revision} (it is at {rule['revision']}); reload it")
    unknown = set(body) - set(EDITABLE_FIELDS)
    if unknown:
        return error(400, f"Not editable: {', '.join(sorted(unknown))}")
    return _save(rule, body, who, dynamo)


def _save(rule: dict, changes: dict, who: str, dynamo: DynamoClient):
    updated = {**rule, **changes}
    problems = validate_rule({k: v for k, v in updated.items() if k not in STORED_FIELDS})
    if problems:
        return error(400, "Invalid rule", problems)
    changed = sorted(k for k in changes if rule.get(k) != changes[k])
    if not changed:
        return ok(rule)
    try:
        return ok(dynamo.replace_rule(updated, who, changed))
    except RevisionConflict:
        return error(409, "The rule was changed by someone else at the same time; reload it")


def _stats(rule: dict, days: int, dynamo: DynamoClient) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    requests = dynamo.requests_for_rule_since(rule["rule_id"], since)
    launches = [r for r in requests if r.get("action", "launch") == "launch"]
    sessions = []
    strategy_id = rule["parameters"].get("strategy_id")
    if strategy_id is not None:
        running = RegistryClient().get_strategy_sessions(strategy_id=strategy_id, status=f"{ACTIVE_SESSION_STATUSES},STOPPING")
        sessions = [{"session_id": s.session_id, "status": s.status, "mode": s.mode, "started_at": s.started_at}
                    for s in running if s.config.get(RULE_ID_KEY) == rule["rule_id"]]
    return {
        "days": days,
        "launches": dict(Counter(r["status"] for r in launches)),
        "shutdowns": dict(Counter(r["status"] for r in requests if r.get("action") == "shutdown")),
        "outcomes": dict(Counter(e["outcome"] for e in dynamo.evaluations_since(rule["rule_id"], since))),
        "last_launch_at": launches[0]["date_created"] if launches else None,
        "running_sessions": sessions,
    }


def _dry_run(rule: dict, body: dict | None, dynamo: DynamoClient):
    """What the rule would do with this trigger right now. Nothing is written and nothing is launched."""
    if body is None or not isinstance(body.get("data"), dict):
        return error(400, "Expected {data: {...}, action?: launch|shutdown}")
    data, action = body["data"], body.get("action", "launch")
    problems = validate_data(rule["rule_type"], data)
    if problems:
        return ok({"outcome": "INVALID", "reason": "; ".join(problems)})
    ctx = RuleContext(registry=RegistryClient(), dynamo=dynamo, slack=SlackClient())
    rule_type = RULE_TYPE_REGISTRY[rule["rule_type"]]()

    if action == "shutdown":
        evaluation = rule_type.evaluate_shutdown(data, rule["parameters"], ctx)
        if isinstance(evaluation, NoMatch):
            return ok({"outcome": "NO_MATCH", "reason": evaluation.reason})
        targets = lifecycle.launched_by(ctx, rule["rule_id"], evaluation.target_session_ids)
        return ok({"outcome": "REQUESTED" if targets else "NO_MATCH", "reason": evaluation.reason,
                   "target_session_ids": targets})

    key = lifecycle.dedup_key(rule["rule_type"], "launch", data)
    notes = {"duplicate": lifecycle.is_repeat(dynamo, rule, key), "rate_limit": rate_limit(rule, ctx)}
    evaluation = rule_type.evaluate(data, rule["parameters"], ctx)
    if isinstance(evaluation, NoMatch):
        return ok({"outcome": "NO_MATCH", "reason": evaluation.reason, **notes})
    config = evaluation.resolved_config
    return ok({
        "outcome": "REQUESTED", "reason": evaluation.reason, **notes,
        "blocked_by": lifecycle.blocking_session(config, ctx),
        "resolved_config": dataclasses.asdict(config),
        "session_config": build_session_config(config, rule["rule_id"]),
    })
