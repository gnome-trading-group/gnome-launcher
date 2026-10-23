import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest

from launcher import lifecycle
from launcher.dynamo import TRIGGER_LEVEL
from launcher.handlers import scheduled_launch, sweeper, trigger_processor
from launcher.handlers.api import api_trigger, launch_requests
from launcher.queues import send_trigger
from tests.conftest import in_minutes
from tests.test_launch_flow import requests_of
from tests.test_tracking import api, log


def trigger(ctx, listings, deadline=None, trigger_id=None, **extra):
    message = {"rule_type": "probe", "data": {"listings": listings, **extra}, "deadline": deadline}
    if trigger_id:
        message["trigger_id"] = trigger_id
    trigger_processor.process(message, ctx)


def pending(ctx, rule, deadline):
    trigger(ctx, [1], deadline=deadline)
    [request] = requests_of(ctx, rule)
    return request


def overdue(ctx, request):
    """The request as it is once its deadline has passed."""
    return ctx.dynamo.annotate(request["request_id"], expires_at=in_minutes(-1))


def sent(ctx, kind):
    return [fields for k, fields in ctx.slack.sent if k == kind]


# ---- triggers ------------------------------------------------------------------------------------------------------

def test_a_request_expires_at_its_triggers_deadline_and_says_so_in_slack(ctx, make_rule):
    rule = make_rule(launch_path="approval")
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)

    request = pending(ctx, rule, deadline.isoformat())

    assert lifecycle.deadline(request["expires_at"]) == deadline
    [approval] = sent(ctx, "approval")
    assert approval["expires_at"] == deadline and approval["deadline_label"] == "deadline"


def test_a_trigger_after_its_deadline_makes_no_request(ctx, make_rule):
    rule = make_rule(launch_path="approval")

    trigger(ctx, [1], deadline=in_minutes(-1), trigger_id="late")

    assert requests_of(ctx, rule) == [] and log(ctx, "late")[rule["rule_id"]]["outcome"] == "EXPIRED"


def test_a_schedule_that_would_launch_after_the_deadline_makes_no_request(ctx, make_rule):
    rule = make_rule(schedule_enabled=True)

    trigger(ctx, [1], deadline=in_minutes(30), trigger_id="t", launch_at=in_minutes(60))

    assert requests_of(ctx, rule) == [] and log(ctx, "t")[rule["rule_id"]]["outcome"] == "EXPIRED"


@pytest.mark.parametrize("deadline", ["tomorrow", "2026-10-18T15:00:00"])
def test_a_deadline_that_isnt_an_iso_time_with_a_timezone_is_invalid(ctx, make_rule, deadline):
    rule = make_rule()

    trigger(ctx, [1], deadline=deadline, trigger_id="bad")

    assert requests_of(ctx, rule) == [] and log(ctx, "bad")[TRIGGER_LEVEL]["outcome"] == "INVALID"


def test_a_trigger_sent_by_hand_is_checked_for_its_deadline():
    status, _ = api(api_trigger.handler, "POST", body={"rule_type": "probe", "data": {"listings": [1]}, "deadline": "soon"})
    assert status == 400


def test_a_repeat_trigger_with_a_new_deadline_moves_the_open_requests_expiry(ctx, make_rule):
    rule = make_rule(launch_path="approval")
    request = pending(ctx, rule, in_minutes(60))
    later = in_minutes(90)

    trigger(ctx, [1], deadline=later, trigger_id="moved")

    [moved] = requests_of(ctx, rule)
    assert moved["request_id"] == request["request_id"] and moved["expires_at"] == later
    assert "moved its expiry" in log(ctx, "moved")[rule["rule_id"]]["reason"]
    assert [r["expires_at"] for r in sent(ctx, "refresh")] == [later]


def test_a_repeat_trigger_with_the_same_deadline_leaves_the_request_alone(ctx, make_rule):
    rule = make_rule(launch_path="approval")
    deadline = in_minutes(60)
    pending(ctx, rule, deadline)

    trigger(ctx, [1], deadline=deadline)

    assert len(requests_of(ctx, rule)[0]["events"]) == 1 and sent(ctx, "refresh") == []


def test_a_deadline_moved_earlier_past_now_expires_the_request_at_the_next_sweep(ctx, make_rule):
    rule = make_rule(launch_path="approval")
    request = pending(ctx, rule, in_minutes(60))

    trigger(ctx, [1], deadline=in_minutes(-1))
    sweeper.sweep(ctx)

    assert ctx.dynamo.get_request(request["request_id"])["status"] == "EXPIRED"


def test_an_expired_request_doesnt_block_a_new_trigger_for_a_later_deadline(ctx, make_rule):
    rule = make_rule(launch_path="approval")
    overdue(ctx, pending(ctx, rule, in_minutes(60)))
    sweeper.sweep(ctx)

    trigger(ctx, [1], deadline=in_minutes(120))

    assert [r["status"] for r in requests_of(ctx, rule)] == ["EXPIRED", "PENDING_APPROVAL"]


# ---- approvals -----------------------------------------------------------------------------------------------------

def test_approving_in_slack_after_the_deadline_is_refused(ctx, make_rule, slack_click):
    rule = make_rule(launch_path="approval")
    request = overdue(ctx, pending(ctx, rule, in_minutes(60)))

    slack_click("launch_approve", request["request_id"])

    assert ctx.dynamo.get_request(request["request_id"])["status"] == "EXPIRED"
    assert not slack_click.lambda_client.invoked and not ctx.registry.sessions
    assert sent(ctx, "update")[-1]["text"].startswith("Expired: deadline was")
    assert "too late" in sent(ctx, "reply")[-1]["text"]


def test_approving_in_the_controller_after_the_deadline_is_refused(ctx, make_rule, invoked):
    rule = make_rule(launch_path="approval")
    request = overdue(ctx, pending(ctx, rule, in_minutes(60)))

    status, body = api(launch_requests.handler, "POST", {"id": request["request_id"], "action": "approve"})

    assert status == 409 and "expired" in body["message"]
    assert ctx.dynamo.get_request(request["request_id"])["status"] == "EXPIRED" and not invoked.calls


def test_an_approval_made_just_before_the_deadline_doesnt_launch_after_it(ctx, make_rule, slack_click, run_async):
    rule = make_rule(launch_path="approval")
    request = pending(ctx, rule, in_minutes(60))
    slack_click("launch_approve", request["request_id"])
    overdue(ctx, request)

    run_async(slack_click)

    assert ctx.dynamo.get_request(request["request_id"])["status"] == "EXPIRED" and not ctx.registry.sessions


def test_a_failed_launch_cant_be_retried_after_the_deadline(ctx, make_rule, invoked):
    rule = make_rule()

    def refuse(*_):
        raise RuntimeError("no capacity")
    ctx.registry.on_create = refuse
    trigger(ctx, [1], deadline=in_minutes(60))
    failed = overdue(ctx, requests_of(ctx, rule)[0])

    status, body = api(launch_requests.handler, "POST", {"id": failed["request_id"], "action": "retry"})

    assert status == 409 and "Too late" in body["message"] and not invoked.calls


# ---- schedules and the sweeper -------------------------------------------------------------------------------------

def test_a_scheduled_launch_firing_after_the_deadline_doesnt_launch(ctx, make_rule):
    rule = make_rule(schedule_enabled=True)
    trigger(ctx, [1], deadline=in_minutes(60), launch_at=in_minutes(30))
    request = overdue(ctx, requests_of(ctx, rule)[0])

    scheduled_launch.fire(request["request_id"], ctx)

    assert ctx.dynamo.get_request(request["request_id"])["status"] == "EXPIRED" and not ctx.registry.sessions


def test_the_sweeper_expires_overdue_requests_and_retires_their_slack_buttons(ctx, make_rule):
    approval = make_rule(launch_path="approval")
    scheduled = make_rule(name="scheduled", schedule_enabled=True)
    trigger(ctx, [1], deadline=in_minutes(60), launch_at=in_minutes(30))
    [waiting] = requests_of(ctx, approval)
    [timed] = requests_of(ctx, scheduled)
    assert timed["status"] == "SCHEDULED"

    sweeper.sweep(ctx, now=datetime.now(timezone.utc) + timedelta(minutes=61))

    assert [ctx.dynamo.get_request(r["request_id"])["status"] for r in (waiting, timed)] == ["EXPIRED", "EXPIRED"]
    assert boto3.client("scheduler").list_schedules()["Schedules"] == []
    assert {u["message_ts"] for u in sent(ctx, "update") if u["text"].startswith("Expired")} == {
        waiting["slack_message_ts"], timed["slack_message_ts"]}


def test_a_request_with_no_deadline_never_expires(ctx, make_rule):
    rule = make_rule(launch_path="approval")
    request = pending(ctx, rule, None)

    sweeper.sweep(ctx, now=datetime.now(timezone.utc) + timedelta(hours=2))

    assert request["expires_at"] is None
    assert ctx.dynamo.get_request(request["request_id"])["status"] == "PENDING_APPROVAL"


def test_a_cs2_requests_expiry_is_told_as_its_entry_cutoff():
    request = {"rule_type": "cs2", "expires_at": "2026-10-18T17:00:00+02:00"}
    assert lifecycle.expiry_note(request) == "entry cutoff was 2026-10-18 15:00 UTC"


def test_trigger_messages_carry_the_deadline_beside_the_data(aws):
    sqs = boto3.client("sqs")
    url = sqs.create_queue(QueueName="t.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    send_trigger(sqs, url, "cs2", "launch", {"match_id": 1}, deadline="2026-10-18T15:00:00+00:00")

    body = json.loads(sqs.receive_message(QueueUrl=url)["Messages"][0]["Body"])
    assert body["data"] == {"match_id": 1} and body["deadline"] == "2026-10-18T15:00:00+00:00"
