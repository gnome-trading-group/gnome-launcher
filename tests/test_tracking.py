import json
from datetime import datetime, timedelta, timezone

import pytest

from launcher.handlers import shutdown_processor, sweeper, trigger_processor
from launcher.handlers.api import api_trigger, launch_requests, launch_rules
from launcher.handlers.slack import approve_launch
from tests.conftest import STRATEGY, in_minutes


def trigger(ctx, listings, trigger_id=None, **extra):
    message = {"rule_type": "probe", "data": {"listings": listings, **extra}, "timestamp": datetime.now(timezone.utc).isoformat()}
    if trigger_id:
        message["trigger_id"] = trigger_id
    trigger_processor.process(message, ctx)


def log(ctx, trigger_id):
    return {e["rule_id"]: e for e in ctx.dynamo.evaluations_for_trigger(trigger_id)}


def api(handler, method, path_params=None, body=None, query=None, resource=""):
    event = {"httpMethod": method, "pathParameters": path_params, "queryStringParameters": query, "resource": resource,
             "body": json.dumps(body) if body is not None else None,
             "requestContext": {"authorizer": {"claims": {"email": "mason@example.com"}}}}
    response = handler(event, None)
    return response["statusCode"], json.loads(response["body"])


# ---- the trigger log -----------------------------------------------------------------------------------------------

def test_every_rule_records_why_it_did_or_didnt_launch(ctx, make_rule):
    launches = make_rule(name="launches")
    capped = make_rule(name="capped", max_concurrent_sessions=1)
    ctx.registry.add("other", STRATEGY["strategy_id"], "paper", {"listings": [9]}, status="RUNNING")

    trigger(ctx, [1], trigger_id="t1")

    entries = log(ctx, "t1")
    request = ctx.dynamo.get_request(entries[launches["rule_id"]]["request_id"])
    assert entries[launches["rule_id"]]["outcome"] == "REQUESTED" and request["trigger_id"] == "t1"
    assert request["rule_revision"] == 1 and "ttl" not in entries[launches["rule_id"]]
    assert entries[capped["rule_id"]]["outcome"] == "RATE_LIMITED" and "cap of 1" in entries[capped["rule_id"]]["reason"]
    assert entries[capped["rule_id"]]["ttl"] > datetime.now(timezone.utc).timestamp()


def test_repeats_blocks_and_bad_triggers_are_logged(ctx, make_rule):
    rule = make_rule()
    trigger(ctx, [1], trigger_id="first")
    trigger(ctx, [1], trigger_id="again")
    ctx.registry.add("manual", STRATEGY["strategy_id"], "paper", {"listings": [2]}, status="RUNNING")
    trigger(ctx, [2], trigger_id="held")
    trigger_processor.process({"rule_type": "probe", "trigger_id": "bad", "data": {"listings": "2"}}, ctx)

    assert log(ctx, "again")[rule["rule_id"]]["outcome"] == "DUPLICATE"
    assert log(ctx, "held")[rule["rule_id"]]["outcome"] == "BLOCKED"
    assert log(ctx, "bad")["-"]["outcome"] == "INVALID"


def test_a_trigger_with_no_rule_to_ask_is_logged(ctx):
    trigger(ctx, [1], trigger_id="lonely")

    assert log(ctx, "lonely")["-"]["outcome"] == "NO_RULES"


def test_shutdowns_are_logged_too(ctx, make_rule):
    rule = make_rule()
    shutdown_processor.process({"rule_type": "probe", "trigger_id": "s1", "data": {"listings": [1]}}, ctx)

    assert log(ctx, "s1")[rule["rule_id"]]["outcome"] == "NO_MATCH"


# ---- the sweeper ---------------------------------------------------------------------------------------------------

def test_a_launched_request_follows_its_session_until_it_ends(ctx, make_rule):
    rule = make_rule()
    trigger(ctx, [1], trigger_id="t")
    request = ctx.dynamo.get_request(log(ctx, "t")[rule["rule_id"]]["request_id"])
    session = ctx.registry.sessions[request["session_id"]]

    session.status, session.started_at, session.stopped_at = "RUNNING", "2026-10-16T00:00:00Z", None
    sweeper.sweep(ctx)
    assert ctx.dynamo.get_request(request["request_id"])["session_status"] == "RUNNING"

    session.status, session.stopped_at = "STOPPED", "2026-10-16T08:30:00Z"
    session.stopped_by, session.stop_reason = "strategy", "match went live"
    sweeper.sweep(ctx)
    ended = ctx.dynamo.get_request(request["request_id"])
    assert ended["session_status"] == "STOPPED" and ended["session_stopped_at"] == "2026-10-16T08:30:00Z"
    assert ended["session_stopped_by"] == "strategy" and ended["session_stop_reason"] == "match went live"
    assert "tracking" not in ended and ctx.dynamo.tracked_requests() == []


@pytest.fixture
def stuck(ctx, make_rule):
    """A request left LAUNCHING by a Lambda that died after recording the session id it was about to create."""
    rule = make_rule()
    request = ctx.dynamo.create_launch_request(
        rule_type="probe", data={"listings": [1]}, dedup_key="k", resolved_config={**STRATEGY, "listings": [1]},
        matched_rule_id=rule["rule_id"], matched_rule_name=rule["name"], launch_path="auto", status="LAUNCHING",
        actor="launcher")
    ctx.dynamo.annotate(request["request_id"], session_id="s-1")
    return request


def test_a_stuck_launch_whose_session_exists_becomes_launched(ctx, stuck):
    ctx.registry.add("s-1", STRATEGY["strategy_id"], "paper", {"listings": [1]}, status="RUNNING")

    sweeper.sweep(ctx, now=datetime.now(timezone.utc) + timedelta(minutes=20))

    request = ctx.dynamo.get_request(stuck["request_id"])
    assert request["status"] == "LAUNCHED" and request["tracking"] == "open"


def test_a_stuck_launch_without_a_session_fails(ctx, stuck):
    sweeper.sweep(ctx, now=datetime.now(timezone.utc) + timedelta(minutes=20))

    assert ctx.dynamo.get_request(stuck["request_id"])["status"] == "FAILED"


def test_a_launch_is_not_stuck_while_it_is_still_fresh(ctx, stuck):
    sweeper.sweep(ctx)

    assert ctx.dynamo.get_request(stuck["request_id"])["status"] == "LAUNCHING"


def test_an_approval_nobody_acted_on_expires(ctx, make_rule):
    make_rule(launch_path="approval")
    trigger(ctx, [1])
    [pending] = ctx.dynamo.requests_in_status("PENDING_APPROVAL")

    sweeper.sweep(ctx, now=datetime.now(timezone.utc) + timedelta(hours=25))

    assert ctx.dynamo.get_request(pending["request_id"])["status"] == "CANCELLED"


# ---- requests API --------------------------------------------------------------------------------------------------

@pytest.fixture
def invoked(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(launch_requests, "_invoke", lambda function, request_id, who: calls.append((request_id, who)))
    monkeypatch.setattr(launch_requests, "SlackClient", lambda: ctx.slack)
    monkeypatch.setattr(approve_launch, "RegistryClient", lambda: ctx.registry)
    monkeypatch.setattr(approve_launch, "SlackClient", lambda: ctx.slack)

    def run():
        for request_id, who in calls:
            approve_launch.handler({"request_id": request_id, "username": who, "actor": who}, None)
        calls.clear()
    run.calls = calls
    return run


def test_approving_in_the_controller_launches_once_even_if_slack_also_clicks(ctx, make_rule, invoked):
    make_rule(launch_path="approval")
    trigger(ctx, [1])
    [pending] = ctx.dynamo.requests_in_status("PENDING_APPROVAL")

    status, body = api(launch_requests.handler, "POST", {"id": pending["request_id"], "action": "approve"})
    again, _ = api(launch_requests.handler, "POST", {"id": pending["request_id"], "action": "approve"})
    invoked()

    request = ctx.dynamo.get_request(pending["request_id"])
    assert (status, again) == (202, 409) and request["status"] == "LAUNCHED"
    assert request["approved_by"] == "ui:mason@example.com" and len(ctx.registry.sessions) == 1


def test_rejecting_and_cancelling_in_the_controller(ctx, make_rule):
    make_rule(launch_path="approval")
    trigger(ctx, [1])
    [pending] = ctx.dynamo.requests_in_status("PENDING_APPROVAL")

    assert api(launch_requests.handler, "POST", {"id": pending["request_id"], "action": "reject"})[1]["status"] == "REJECTED"
    assert api(launch_requests.handler, "POST", {"id": pending["request_id"], "action": "cancel"})[0] == 409


def test_a_failed_launch_retried_from_the_controller(ctx, make_rule, invoked):
    rule = make_rule()

    def refuse(*_):
        raise RuntimeError("no capacity")
    ctx.registry.on_create = refuse
    trigger(ctx, [1], trigger_id="t")
    failed_id = log(ctx, "t")[rule["rule_id"]]["request_id"]
    ctx.registry.on_create = None

    status, retry = api(launch_requests.handler, "POST", {"id": failed_id, "action": "retry"})
    invoked()

    assert status == 202 and retry["retry_of"] == failed_id
    assert ctx.dynamo.get_request(retry["request_id"])["status"] == "LAUNCHED"
    assert api(launch_requests.handler, "POST", {"id": retry["request_id"], "action": "retry"})[0] == 409


def test_history_filters_and_pages(ctx, make_rule):
    rule = make_rule(dedup_window_minutes=0)
    for listing in range(5):
        trigger(ctx, [listing])

    _, first = api(launch_requests.handler, "GET", query={"rule_id": rule["rule_id"], "limit": "3"})
    _, second = api(launch_requests.handler, "GET", query={"rule_id": rule["rule_id"], "limit": "3",
                                                            "before": first["next_before"]})
    _, everything = api(launch_requests.handler, "GET", query={"limit": "50"})

    assert len(first["items"]) == 3 and len(second["items"]) == 2 and second["next_before"] is None
    assert [r["request_id"] for r in first["items"] + second["items"]] == [r["request_id"] for r in everything["items"]]
    _, one = api(launch_requests.handler, "GET", {"id": first["items"][0]["request_id"]})
    assert one["evaluations"][0]["outcome"] == "REQUESTED"


# ---- rules API -----------------------------------------------------------------------------------------------------

PROBE_RULE = {"name": "probe rule", "rule_type": "probe", "launch_path": "auto", "parameters": dict(STRATEGY)}


@pytest.fixture
def rules_api(ctx, monkeypatch):
    monkeypatch.setattr(launch_rules, "RegistryClient", lambda: ctx.registry)
    monkeypatch.setattr(launch_rules, "SlackClient", lambda: ctx.slack)
    return lambda method, rule_id=None, view=None, **kw: api(
        launch_rules.handler, method, {"id": rule_id, "view": view} if rule_id else None, **kw)


def test_every_edit_is_a_revision_with_its_author(rules_api):
    _, rule = rules_api("POST", body=PROBE_RULE)

    _, edited = rules_api("PATCH", rule["rule_id"], body={"cooldown_minutes": 5, "revision": 1})
    stale, _ = rules_api("PATCH", rule["rule_id"], body={"cooldown_minutes": 9, "revision": 1})

    _, revisions = rules_api("GET", rule["rule_id"], "revisions")
    assert edited["revision"] == 2 and edited["modified_by"] == "ui:mason@example.com" and stale == 409
    assert [r["revision"] for r in revisions] == [2, 1] and revisions[0]["changed"] == ["cooldown_minutes"]
    assert revisions[0]["rule"]["cooldown_minutes"] == 5 and revisions[1]["actor"] == "ui:mason@example.com"


def test_stats_and_activity_show_what_the_rule_did(ctx, rules_api):
    _, rule = rules_api("POST", body={**PROBE_RULE, "max_concurrent_sessions": 1})
    rule = ctx.dynamo.get_rule(rule["rule_id"])
    trigger(ctx, [1])
    trigger(ctx, [2])

    _, stats = rules_api("GET", rule["rule_id"], "stats")
    _, activity = rules_api("GET", rule["rule_id"], "activity")

    assert stats["launches"] == {"LAUNCHED": 1} and stats["outcomes"] == {"REQUESTED": 1, "RATE_LIMITED": 1}
    assert [s["status"] for s in stats["running_sessions"]] == ["SUBMITTED"] and stats["last_launch_at"]
    assert [e["outcome"] for e in activity["items"]] == ["RATE_LIMITED", "REQUESTED"]


def test_a_dry_run_shows_what_would_launch_without_launching(ctx, rules_api):
    _, rule = rules_api("POST", body=PROBE_RULE)

    _, result = rules_api("POST", rule["rule_id"], "dry-run", body={"data": {"listings": [1, 2]}})

    assert result["outcome"] == "REQUESTED" and result["session_config"]["launcher.rule.id"] == rule["rule_id"]
    assert result["blocked_by"] is None and not ctx.registry.sessions
    assert ctx.dynamo.list_requests(rule_id=rule["rule_id"]) == []


def test_validate_checks_a_rule_without_saving_it(rules_api):
    status, body = api(launch_rules.handler, "POST", resource="/launch-rules/validate",
                       body={"rule": {**PROBE_RULE, "cooldown_minutes": -1}})

    assert status == 200 and any("cooldown_minutes" in p for p in body["problems"])
    assert rules_api("GET")[1] == []


def test_the_global_trigger_log(ctx, make_rule):
    make_rule()
    trigger(ctx, [1], trigger_id="a")
    trigger(ctx, [2], trigger_id="b")

    status, body = api(api_trigger.handler, "GET", query={"rule_type": "probe"})

    assert status == 200 and [e["trigger_id"] for e in body["items"]] == ["b", "a"]
