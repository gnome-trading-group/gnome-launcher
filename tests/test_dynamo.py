import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from launcher.dynamo import RULES_TABLE, REQUESTS_TABLE, DynamoClient
from launcher.rules.cs2 import CS2Rule
from launcher.sessions import build_session_config


@pytest.fixture
def dynamo(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        db = boto3.client("dynamodb")
        db.create_table(
            TableName=RULES_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "rule_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("rule_id", "rule_type", "status")],
            GlobalSecondaryIndexes=[{"IndexName": "rule_type-status-index", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "rule_type", "KeyType": "HASH"},
                                                   {"AttributeName": "status", "KeyType": "RANGE"}]}],
        )
        db.create_table(
            TableName=REQUESTS_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
        )
        yield DynamoClient()


PARAMS = {
    "strategy_id": 5, "strategy_type": "python", "mode": "paper",
    "strategy_class": "gnomepy_research.sessions.cs2_series.strategy:CS2Series",
    "simulation_config": {"simulation.profiles.kalshi.fee.taker.rate": 0.07,
                          "simulation.profiles.kalshi.network.latency.nanos": 50000000},
}


def test_a_rule_with_float_parameters_round_trips_as_plain_numbers(dynamo):
    created = dynamo.create_rule(name="CS2", rule_type="cs2", launch_path="auto", parameters=PARAMS,
                                 max_concurrent_sessions=30)

    [rule] = dynamo.get_active_rules("cs2")

    assert rule["parameters"] == PARAMS and rule["max_concurrent_sessions"] == 30
    assert type(rule["parameters"]["strategy_id"]) is int
    assert type(rule["parameters"]["simulation_config"]["simulation.profiles.kalshi.fee.taker.rate"]) is float
    assert dynamo.get_rule(created["rule_id"]) == rule
    updated = dynamo.update_rule(created["rule_id"], parameters={**PARAMS, "fee_rates": {"kalshi": 0.035}})
    assert updated["parameters"]["fee_rates"] == {"kalshi": 0.035}


def test_a_resolved_launch_from_a_stored_rule_serialises_for_the_registry(dynamo):
    dynamo.create_rule(name="CS2", rule_type="cs2", launch_path="auto", parameters=PARAMS)
    [rule] = dynamo.get_active_rules("cs2")
    ctx = MagicMock()
    ctx.registry.get_listing.return_value = [SimpleNamespace(listing_id=1)]
    data = {"match_id": 1, "venue": "kalshi", "market": "series", "listing_team_a": 11, "listing_team_b": 12}

    config = CS2Rule().evaluate(data, rule["parameters"], ctx).resolved_config
    request = dynamo.create_launch_request(rule_type="cs2", data=data, dedup_key="k", resolved_config=config.__dict__,
                                           matched_rule_id=rule["rule_id"], matched_rule_name="CS2",
                                           launch_path="auto", status="LAUNCHING")

    json.dumps(build_session_config(config))
    assert dynamo.get_request(request["request_id"])["resolved_config"]["strategy_args"]["fee_rate"] == 0.07
