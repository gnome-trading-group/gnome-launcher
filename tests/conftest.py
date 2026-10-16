"""Launcher flows run against moto's DynamoDB, SQS and EventBridge Scheduler. The registry and Slack are the two
services outside AWS, so they are in-memory stand-ins that record what they were asked to do."""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "APPROVE_LAUNCH_FUNCTION_NAME": "approve-launch",
    "APPROVE_SHUTDOWN_FUNCTION_NAME": "approve-shutdown",
    "SCHEDULED_LAUNCH_FUNCTION_ARN": "arn:aws:lambda:us-east-1:123456789012:function:scheduled-launch",
    "SCHEDULER_ROLE_ARN": "arn:aws:iam::123456789012:role/scheduler",
})

from launcher.dynamo import REQUESTS_TABLE, REVISIONS_TABLE, RULES_TABLE, TRIGGERS_TABLE, DynamoClient  # noqa: E402
from launcher.rules.types import (  # noqa: E402
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    NoMatch,
    ResolvedStrategyConfig,
    RuleContext,
    RuleEvaluation,
    RuleType,
    ScheduleResult,
    ShutdownEvaluation,
    register_rule_type,
)
from launcher.sessions import listing_ids  # noqa: E402


@register_rule_type
class ProbeRule(RuleType):
    """A rule type for flow tests: launches on any trigger naming its listings, at `launch_at` if given."""

    type = "probe"
    display_name = "Probe"
    data_schema = {
        "type": "object",
        "properties": {"listings": {"type": "array", "items": {"type": "integer"}}, "launch_at": {"type": "string"}},
        "required": ["listings"],
    }
    parameter_schema = {"type": "object", "properties": STRATEGY_PARAMS_SCHEMA, "required": STRATEGY_REQUIRED_PARAMS}

    def evaluate(self, data, params, ctx):
        return RuleEvaluation(True, ResolvedStrategyConfig.from_params(params, listings=data["listings"]),
                              f"probe {data['listings']}")

    def evaluate_shutdown(self, data, params, ctx):
        running = ctx.registry.get_strategy_sessions(strategy_id=params["strategy_id"], status="SUBMITTED,STARTING,RUNNING")
        targets = [s.session_id for s in running if listing_ids(s.config["listings"]) & set(data["listings"])]
        return ShutdownEvaluation(True, targets, "probe shutdown") if targets else NoMatch("nothing to stop")

    def compute_schedule_time(self, data, params, ctx):
        if "launch_at" not in data:
            return None
        return ScheduleResult(datetime.fromisoformat(data["launch_at"]), "probe schedule")


class FakeRegistry:
    def __init__(self):
        self.sessions: dict[str, SimpleNamespace] = {}
        self.alerts: list[dict] = []
        self.stopped: list[tuple[str, str]] = []
        self.on_create = None

    def create_strategy_session(self, session_id, strategy_id, mode, config, **kwargs):
        if self.on_create:
            return self.on_create(session_id, strategy_id, mode, config)
        self.add(session_id, strategy_id, mode, config)
        return {}

    def add(self, session_id, strategy_id, mode, config, status="SUBMITTED", failure_reason=None):
        self.sessions[session_id] = SimpleNamespace(session_id=session_id, strategy_id=strategy_id, mode=mode,
                                                    config=config, status=status, failure_reason=failure_reason,
                                                    started_at=None, stopped_at=None)

    def get_strategy_sessions(self, session_id=None, strategy_id=None, status=None):
        wanted = set(status.split(",")) if status else None
        return [s for s in self.sessions.values()
                if (session_id is None or s.session_id == session_id)
                and (strategy_id is None or s.strategy_id == strategy_id)
                and (wanted is None or s.status in wanted)]

    def stop_strategy_session_as_service(self, session_id, actor):
        self.stopped.append((session_id, actor))
        self.sessions[session_id].status = "STOPPING"

    def get_listing(self, listing_id=None, **_):
        return [SimpleNamespace(listing_id=listing_id)]

    def send_alert(self, alert_type, title, **fields):
        self.alerts.append({"type": alert_type, "title": title, **fields})


class FakeSlack:
    channel = "C1"

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.fail = False

    def _send(self, kind, **fields):
        if self.fail:
            raise RuntimeError("Slack is down")
        self.sent.append((kind, fields))
        return f"ts-{len(self.sent)}"

    def send_approval_message(self, **fields):
        return self._send("approval", **fields)

    def send_shutdown_approval_message(self, **fields):
        return self._send("shutdown_approval", **fields)

    def send_scheduled_launch_message(self, **fields):
        return self._send("scheduled", **fields)

    def post_thread_reply(self, thread_ts, text):
        self.sent.append(("reply", {"thread_ts": thread_ts, "text": text}))

    def update_message(self, message_ts, text, blocks=None):
        self.sent.append(("update", {"message_ts": message_ts, "text": text}))


def _gsi(name, hash_key, range_key):
    return {"IndexName": name, "Projection": {"ProjectionType": "ALL"},
            "KeySchema": [{"AttributeName": hash_key, "KeyType": "HASH"}, {"AttributeName": range_key, "KeyType": "RANGE"}]}


@pytest.fixture
def aws():
    with mock_aws():
        db = boto3.client("dynamodb")
        db.create_table(
            TableName=RULES_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "rule_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("rule_id", "rule_type", "status")],
            GlobalSecondaryIndexes=[_gsi("rule_type-status-index", "rule_type", "status")],
        )
        db.create_table(
            TableName=REQUESTS_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in
                                  ("request_id", "dedup_key", "status", "rule_type", "matched_rule_id", "tracking",
                                   "date_created")],
            GlobalSecondaryIndexes=[_gsi(f"{k}-date_created-index", k, "date_created")
                                    for k in ("dedup_key", "status", "rule_type", "matched_rule_id", "tracking")],
        )
        db.create_table(
            TableName=TRIGGERS_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "trigger_id", "KeyType": "HASH"}, {"AttributeName": "rule_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in
                                  ("trigger_id", "rule_id", "rule_type", "received_at")],
            GlobalSecondaryIndexes=[_gsi("rule_id-received_at-index", "rule_id", "received_at"),
                                    _gsi("rule_type-received_at-index", "rule_type", "received_at")],
        )
        db.create_table(
            TableName=REVISIONS_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "rule_id", "KeyType": "HASH"}, {"AttributeName": "revision", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "rule_id", "AttributeType": "S"},
                                  {"AttributeName": "revision", "AttributeType": "N"}],
        )
        yield


@pytest.fixture
def ctx(aws):
    return RuleContext(registry=FakeRegistry(), dynamo=DynamoClient(), slack=FakeSlack())


STRATEGY = {"strategy_id": 7, "strategy_type": "python", "strategy_class": "probe.Strategy", "mode": "paper"}


@pytest.fixture
def make_rule(ctx):
    def _make(**overrides):
        rule = {"name": "probe rule", "description": None, "rule_type": "probe", "status": "active",
                "launch_path": "auto", "shutdown_path": "auto", "schedule_enabled": False,
                "max_concurrent_sessions": None, "cooldown_minutes": 0, "dedup_window_minutes": 60,
                "parameters": dict(STRATEGY)}
        return ctx.dynamo.create_rule({**rule, **overrides}, "ui:tester@example.com")
    return _make


def in_minutes(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
