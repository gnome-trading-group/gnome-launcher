import json

import boto3
import pytest

from launcher.handlers.api import api_trigger, launch_rules

PROFILE = {f"simulation.profiles.kalshi.{k}": v for k, v in
           {"fee.model": "parametric", "fee.taker.rate": 0.07, "fee.maker.rate": 2.0}.items()}
CS2_RULE = {
    "name": "CS2 series (paper)",
    "rule_type": "cs2",
    "launch_path": "auto",
    "max_concurrent_sessions": 30,
    "parameters": {
        "strategy_id": 5, "strategy_type": "python", "mode": "paper",
        "strategy_class": "gnomepy_research.sessions.cs2_series.strategy:CS2Series",
        "venues": ["kalshi"], "simulation_config": PROFILE,
    },
}


def call(method, body=None, rule_id=None):
    response = launch_rules.handler({"httpMethod": method, "pathParameters": {"id": rule_id} if rule_id else None,
                                     "body": json.dumps(body) if body is not None else None}, None)
    return response["statusCode"], json.loads(response["body"])


def test_a_created_rule_reads_back_exactly(aws):
    status, created = call("POST", CS2_RULE)

    assert status == 201 and created["status"] == "active" and created["cooldown_minutes"] == 0
    _, fetched = call("GET", rule_id=created["rule_id"])
    assert fetched["parameters"] == CS2_RULE["parameters"]
    assert type(fetched["parameters"]["simulation_config"]["simulation.profiles.kalshi.fee.maker.rate"]) is float


@pytest.mark.parametrize("change, problem", [
    ({"parameters": {**CS2_RULE["parameters"], "strategy_id": None}}, "parameters.strategy_id"),
    ({"parameters": {k: v for k, v in CS2_RULE["parameters"].items() if k != "strategy_id"}}, "'strategy_id' is a required"),
    ({"cooldown_minutes": None}, "cooldown_minutes"),
    ({"max_concurrent_sessions": 0}, "max_concurrent_sessions"),
    ({"launch_path": "sometimes"}, "launch_path"),
    ({"rule_type": "nope"}, "unknown rule type"),
    ({"parameters": {**CS2_RULE["parameters"], "venues": ["kalshi", "polymarket"]}}, "doesn't define"),
    ({"parameters": {**CS2_RULE["parameters"], "simulation_profiles": {"kalshi": "kalshi_v2"}}}, "lowercase"),
])
def test_a_rule_that_would_fail_later_is_refused_now(aws, change, problem):
    status, body = call("POST", {**CS2_RULE, **change})

    assert status == 400 and any(problem in p for p in body["problems"]), body


def test_an_edit_is_validated_as_the_whole_rule(aws):
    _, created = call("POST", CS2_RULE)

    assert call("PATCH", {"cooldown_minutes": 15}, created["rule_id"])[1]["cooldown_minutes"] == 15
    assert call("PATCH", {"max_concurrent_sessions": -1}, created["rule_id"])[0] == 400
    assert call("PATCH", {"rule_type": "probe"}, created["rule_id"])[0] == 400
    assert call("GET", rule_id=created["rule_id"])[1]["cooldown_minutes"] == 15


def test_deleting_a_rule_archives_it(aws):
    _, created = call("POST", CS2_RULE)

    status, archived = call("DELETE", rule_id=created["rule_id"])

    assert status == 200 and archived["status"] == "archived"
    assert [r["status"] for r in call("GET")[1]] == ["archived"]


@pytest.fixture
def queues(aws, monkeypatch):
    sqs = boto3.client("sqs")
    urls = {name: sqs.create_queue(QueueName=f"{name}.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
            for name in ("launch", "shutdown")}
    monkeypatch.setenv("LAUNCHER_QUEUE_URL", urls["launch"])
    monkeypatch.setenv("SHUTDOWN_QUEUE_URL", urls["shutdown"])
    return sqs, urls


def post_trigger(body):
    response = api_trigger.handler({"body": json.dumps(body)}, None)
    return response["statusCode"], json.loads(response["body"])


def test_a_trigger_is_queued_in_its_rule_types_group(queues):
    sqs, urls = queues

    status, body = post_trigger({"rule_type": "probe", "action": "shutdown", "data": {"listings": [1]}})

    [message] = sqs.receive_message(QueueUrl=urls["shutdown"], AttributeNames=["MessageGroupId"])["Messages"]
    assert status == 202 and json.loads(message["Body"])["trigger_id"] == body["trigger_id"]
    assert message["Attributes"]["MessageGroupId"] == "probe"


def test_an_invalid_trigger_is_refused(queues):
    assert post_trigger({"rule_type": "probe", "data": {"listings": "1"}})[0] == 400
    assert post_trigger({"rule_type": "nope", "data": {}})[0] == 400
    assert post_trigger({"rule_type": "probe", "action": "explode", "data": {"listings": [1]}})[0] == 400
