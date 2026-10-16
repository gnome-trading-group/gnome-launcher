import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import boto3
import pytest
import requests

from launcher import lifecycle
from launcher.handlers import scheduled_launch, shutdown_processor, trigger_processor
from launcher.handlers.slack import approve_launch, approve_shutdown, interaction
from tests.conftest import STRATEGY, in_minutes


def trigger(ctx, listings, **extra):
    trigger_processor.process({"rule_type": "probe", "data": {"listings": listings, **extra}}, ctx)


def requests_of(ctx, rule):
    return sorted(ctx.dynamo.requests_for_rule_since(rule["rule_id"], datetime(2000, 1, 1, tzinfo=timezone.utc)),
                  key=lambda r: r["date_created"])


# ---- auto launches -------------------------------------------------------------------------------------------------

def test_an_auto_launch_records_its_session_and_timeline(ctx, make_rule):
    rule = make_rule()

    trigger(ctx, [1, 2])

    [request] = requests_of(ctx, rule)
    session = ctx.registry.sessions[request["session_id"]]
    assert request["status"] == "LAUNCHED" and [e["status"] for e in request["events"]] == ["LAUNCHING", "LAUNCHED"]
    assert session.config["launcher.rule.id"] == rule["rule_id"] and session.config["listings"] == [1, 2]
    assert [a["type"] for a in ctx.registry.alerts] == ["LAUNCH_SUCCEEDED"]


def test_a_repeat_trigger_inside_the_window_does_nothing(ctx, make_rule):
    rule = make_rule()
    trigger(ctx, [1, 2])
    ctx.registry.sessions.clear()

    trigger(ctx, [1, 2])

    assert len(requests_of(ctx, rule)) == 1


def test_each_rule_dedups_in_its_own_window(ctx, make_rule):
    rule = make_rule(dedup_window_minutes=0)
    trigger(ctx, [1, 2])
    ctx.registry.sessions.clear()

    trigger(ctx, [1, 2])

    assert len(requests_of(ctx, rule)) == 2


def test_a_failed_launch_is_retried_by_the_next_trigger_under_one_alert(ctx, make_rule):
    rule = make_rule()

    def refuse(*_):
        raise requests.HTTPError("409 Client Error")
    ctx.registry.on_create = refuse
    trigger(ctx, [1, 2])
    trigger(ctx, [1, 2])

    assert [r["status"] for r in requests_of(ctx, rule)] == ["FAILED", "FAILED"]
    keys = {a["key"] for a in ctx.registry.alerts if a["type"] == "LAUNCH_FAILED"}
    assert len(keys) == 1


def test_a_launch_that_errors_after_the_registry_made_the_session_counts_as_launched(ctx, make_rule):
    rule = make_rule()

    def made_then_timed_out(session_id, strategy_id, mode, config):
        ctx.registry.add(session_id, strategy_id, mode, config)
        raise requests.Timeout("read timed out")
    ctx.registry.on_create = made_then_timed_out
    trigger(ctx, [1, 2])

    [request] = requests_of(ctx, rule)
    assert request["status"] == "LAUNCHED" and request["session_id"] in ctx.registry.sessions


def test_a_session_the_registry_failed_to_start_fails_the_request_with_its_reason(ctx, make_rule):
    rule = make_rule()

    def failed_row(session_id, strategy_id, mode, config):
        ctx.registry.add(session_id, strategy_id, mode, config, status="FAILED", failure_reason="Launch failed: no capacity")
        raise requests.HTTPError("500 Server Error")
    ctx.registry.on_create = failed_row
    trigger(ctx, [1, 2])

    [request] = requests_of(ctx, rule)
    assert request["status"] == "FAILED" and "no capacity" in request["launch_error"]
    assert request["session_id"], "the request still leads to the session the registry recorded"


def test_listings_held_by_an_active_session_block_the_launch(ctx, make_rule):
    rule = make_rule()
    ctx.registry.add("manual", STRATEGY["strategy_id"], "paper", {"listings": "2,3"}, status="RUNNING")

    trigger(ctx, [1, 2])

    assert requests_of(ctx, rule) == []


def test_the_concurrency_cap_counts_the_strategys_active_sessions(ctx, make_rule):
    rule = make_rule(max_concurrent_sessions=1)
    trigger(ctx, [1])

    trigger(ctx, [2])

    assert len(requests_of(ctx, rule)) == 1


def test_the_cooldown_sees_past_a_newer_shutdown_or_failure(ctx, make_rule):
    rule = make_rule(cooldown_minutes=60)
    trigger(ctx, [1])
    shutdown_processor.process({"rule_type": "probe", "data": {"listings": [1]}}, ctx)
    assert [r["action"] for r in requests_of(ctx, rule)] == ["launch", "shutdown"]

    trigger(ctx, [2])

    assert [r["action"] for r in requests_of(ctx, rule)] == ["launch", "shutdown"]


def test_an_invalid_trigger_is_dropped(ctx, make_rule):
    rule = make_rule()

    trigger_processor.process({"rule_type": "probe", "data": {"listings": "1,2"}}, ctx)

    assert requests_of(ctx, rule) == []


def test_disabled_rules_dont_launch(ctx, make_rule):
    rule = make_rule(status="disabled")

    trigger(ctx, [1])

    assert requests_of(ctx, rule) == []


# ---- approvals -----------------------------------------------------------------------------------------------------

SIGNING_SECRET = "secret"


class _Lambda:
    def __init__(self):
        self.invoked = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invoked.append((FunctionName, json.loads(Payload)))


@pytest.fixture
def slack_click(ctx, monkeypatch):
    lam = _Lambda()
    monkeypatch.setattr(interaction, "_get_signing_secret", lambda: SIGNING_SECRET)
    monkeypatch.setattr(interaction, "_lambda_client", lam)
    monkeypatch.setattr(interaction, "SlackClient", lambda: ctx.slack)

    def click(action_id, request_id, username="mason", action=None):
        body = urlencode({"payload": json.dumps({
            "actions": [action or {"action_id": action_id, "value": request_id}], "user": {"id": "U1", "username": username}})})
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(SIGNING_SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
        return interaction.handler({"headers": {"x-slack-request-timestamp": ts, "x-slack-signature": sig},
                                    "body": body}, None)
    click.lambda_client = lam
    return click


@pytest.fixture
def run_async(ctx, monkeypatch):
    for module in (approve_launch, approve_shutdown):
        monkeypatch.setattr(module, "RegistryClient", lambda: ctx.registry)
        monkeypatch.setattr(module, "SlackClient", lambda: ctx.slack)

    def run(click):
        for function, payload in click.lambda_client.invoked:
            (approve_launch if function == "approve-launch" else approve_shutdown).handler(payload, None)
        click.lambda_client.invoked.clear()
    return run


def test_an_approval_rule_waits_for_slack(ctx, make_rule):
    rule = make_rule(launch_path="approval")

    trigger(ctx, [1])

    [request] = requests_of(ctx, rule)
    assert request["status"] == "PENDING_APPROVAL" and request["slack_message_ts"] and not ctx.registry.sessions


def test_a_link_button_on_another_message_is_acknowledged_and_ignored(ctx, slack_click):
    # An alert's "Runbook" button: Slack reports the click with a url and no value.
    response = slack_click(None, None, action={"action_id": "WHk", "type": "button", "url": "https://example.com/runbook"})

    assert response["statusCode"] == 200
    assert not slack_click.lambda_client.invoked and not ctx.slack.sent


def test_a_double_click_approval_launches_once(ctx, make_rule, slack_click, run_async):
    make_rule(launch_path="approval")
    trigger(ctx, [1])
    request_id = ctx.dynamo.list_requests(status="PENDING_APPROVAL")[0]["request_id"]

    slack_click("launch_approve", request_id)
    slack_click("launch_approve", request_id, username="someone-else")
    run_async(slack_click)

    request = ctx.dynamo.get_request(request_id)
    assert request["status"] == "LAUNCHED" and len(ctx.registry.sessions) == 1
    assert [e["actor"] for e in request["events"]] == ["launcher", "slack:mason", "slack:mason", "slack:mason"]
    assert any("already APPROVED" in f.get("text", "") for kind, f in ctx.slack.sent if kind == "reply")


def test_a_reject_after_an_approval_changes_nothing(ctx, make_rule, slack_click, run_async):
    make_rule(launch_path="approval")
    trigger(ctx, [1])
    request_id = ctx.dynamo.list_requests(status="PENDING_APPROVAL")[0]["request_id"]

    slack_click("launch_approve", request_id)
    slack_click("launch_reject", request_id)
    run_async(slack_click)

    assert ctx.dynamo.get_request(request_id)["status"] == "LAUNCHED"


def test_an_approval_the_async_launcher_receives_twice_launches_once(ctx, make_rule, slack_click, run_async):
    make_rule(launch_path="approval")
    trigger(ctx, [1])
    request_id = ctx.dynamo.list_requests(status="PENDING_APPROVAL")[0]["request_id"]
    slack_click("launch_approve", request_id)
    slack_click.lambda_client.invoked *= 2

    run_async(slack_click)

    assert len(ctx.registry.sessions) == 1


def test_an_approval_slack_couldnt_post_still_waits_for_the_controller(ctx, make_rule):
    rule = make_rule(launch_path="approval")
    ctx.slack.fail = True

    trigger(ctx, [1])

    [request] = requests_of(ctx, rule)
    assert request["status"] == "PENDING_APPROVAL" and request["slack_message_ts"] is None



def test_approvals_hold_on_a_stage_with_no_slack_channel(ctx, make_rule):
    # Dev posts nothing to Slack; launches and shutdowns there wait for an approval from the controller all the same.
    rule = make_rule(launch_path="approval", shutdown_path="approval")
    ctx.slack.fail = True

    trigger(ctx, [1])
    [launch] = requests_of(ctx, rule)
    assert launch["status"] == "PENDING_APPROVAL" and launch["launch_path"] == "approval" and not ctx.registry.sessions

    ctx.dynamo.transition(launch["request_id"], "PENDING_APPROVAL", "APPROVED", actor="ui:someone")
    lifecycle.launch(ctx, ctx.dynamo.get_request(launch["request_id"]), rule["name"], "approved", "ui:someone")
    shutdown_processor.process({"rule_type": "probe", "data": {"listings": [1]}}, ctx)
    shutdown = requests_of(ctx, rule)[-1]
    assert shutdown["status"] == "PENDING_SHUTDOWN_APPROVAL" and not ctx.registry.stopped

# ---- schedules -----------------------------------------------------------------------------------------------------

def _schedules():
    return boto3.client("scheduler").list_schedules()["Schedules"]


def test_a_scheduled_launch_fires_once(ctx, make_rule):
    rule = make_rule(schedule_enabled=True)
    trigger(ctx, [1], launch_at=in_minutes(30))
    [request] = requests_of(ctx, rule)
    assert request["status"] == "SCHEDULED" and [s["Name"] for s in _schedules()] == [request["schedule_name"]]

    scheduled_launch.fire(request["request_id"], ctx)
    scheduled_launch.fire(request["request_id"], ctx)

    assert ctx.dynamo.get_request(request["request_id"])["status"] == "LAUNCHED" and len(ctx.registry.sessions) == 1


def test_a_schedule_for_a_rule_disabled_since_is_cancelled_when_it_fires(ctx, make_rule):
    rule = make_rule(schedule_enabled=True)
    trigger(ctx, [1], launch_at=in_minutes(30))
    ctx.dynamo.replace_rule({**ctx.dynamo.get_rule(rule["rule_id"]), "status": "disabled"}, "ui:tester", ["status"])
    [request] = requests_of(ctx, rule)

    scheduled_launch.fire(request["request_id"], ctx)

    assert ctx.dynamo.get_request(request["request_id"])["status"] == "CANCELLED" and not ctx.registry.sessions


def test_a_cancelled_schedule_never_launches(ctx, make_rule, slack_click):
    rule = make_rule(schedule_enabled=True)
    trigger(ctx, [1], launch_at=in_minutes(30))
    [request] = requests_of(ctx, rule)

    slack_click("schedule_cancel", request["request_id"])
    scheduled_launch.fire(request["request_id"], ctx)

    assert ctx.dynamo.get_request(request["request_id"])["status"] == "CANCELLED"
    assert not ctx.registry.sessions and _schedules() == []


def test_a_launch_time_already_past_launches_now(ctx, make_rule):
    rule = make_rule(schedule_enabled=True)

    trigger(ctx, [1], launch_at=in_minutes(-5))

    assert [r["status"] for r in requests_of(ctx, rule)] == ["LAUNCHED"]


def test_schedule_times_are_written_in_utc():
    when = datetime(2026, 10, 16, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert lifecycle.at_expression(when) == "at(2026-10-16T10:00:00)"


# ---- shutdowns -----------------------------------------------------------------------------------------------------

def test_a_shutdown_stops_only_the_sessions_its_rule_launched(ctx, make_rule):
    rule = make_rule()
    trigger(ctx, [1])
    launched = requests_of(ctx, rule)[0]["session_id"]
    ctx.registry.add("manual", STRATEGY["strategy_id"], "paper", {"listings": [1]}, status="RUNNING")

    shutdown_processor.process({"rule_type": "probe", "data": {"listings": [1]}}, ctx)

    assert ctx.registry.stopped == [(launched, "launcher:probe rule")]
    shutdown = requests_of(ctx, rule)[-1]
    assert shutdown["action"] == "shutdown" and shutdown["status"] == "STOPPED"
    assert shutdown["target_session_ids"] == [launched]


def test_a_disabled_rule_still_stops_its_sessions(ctx, make_rule):
    rule = make_rule()
    trigger(ctx, [1])
    ctx.dynamo.replace_rule({**ctx.dynamo.get_rule(rule["rule_id"]), "status": "disabled"}, "ui:tester", ["status"])

    shutdown_processor.process({"rule_type": "probe", "data": {"listings": [1]}}, ctx)

    assert len(ctx.registry.stopped) == 1


def test_a_double_click_shutdown_approval_stops_once(ctx, make_rule, slack_click, run_async):
    rule = make_rule(shutdown_path="approval")
    trigger(ctx, [1])
    shutdown_processor.process({"rule_type": "probe", "data": {"listings": [1]}}, ctx)
    shutdown = requests_of(ctx, rule)[-1]
    assert shutdown["status"] == "PENDING_SHUTDOWN_APPROVAL"

    slack_click("shutdown_approve", shutdown["request_id"])
    slack_click("shutdown_approve", shutdown["request_id"])
    slack_click("shutdown_reject", shutdown["request_id"])
    run_async(slack_click)

    assert ctx.dynamo.get_request(shutdown["request_id"])["status"] == "STOPPED" and len(ctx.registry.stopped) == 1


def test_a_direct_launch_trigger_carries_everything_a_deploy_does(ctx, make_rule):
    rule = make_rule(rule_type="direct_launch", parameters={})
    data = {**STRATEGY, "listings": [7, 8], "strategy_args": {"edge": 0.05, "dry": False, "legs": [1, 2]},
            "instance_type": "c7i.large", "latency_profile": "standard", "overrides": {"oms.max.orders": "5"}}

    trigger_processor.process({"rule_type": "direct_launch", "data": data}, ctx)

    [request] = requests_of(ctx, rule)
    session = ctx.registry.sessions[request["session_id"]]
    assert request["status"] == "LAUNCHED" and session.config["strategy.args.edge"] == 0.05
    assert session.config["strategy.args.legs"] == [1, 2] and session.config["overrides.oms.max.orders"] == "5"
    assert session.config["latency.profile"] == "standard"
