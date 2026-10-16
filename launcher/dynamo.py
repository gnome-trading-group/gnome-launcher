import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from launcher.models import LaunchRequest, LaunchRule

logger = logging.getLogger(__name__)

REQUESTS_TABLE = os.environ.get("LAUNCH_REQUESTS_TABLE", "gnome-launch-requests")
RULES_TABLE = os.environ.get("LAUNCH_RULES_TABLE", "gnome-launch-rules")

# Free-form JSON is stored as a string: DynamoDB takes no floats and returns every number as a Decimal, so a nested
# map would come back with 2.0 turned into 2 and every number in a type neither strategies nor the registry accept.
RULE_JSON_FIELDS = ("parameters",)
REQUEST_JSON_FIELDS = ("data", "resolved_config")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(item: dict, json_fields: tuple[str, ...]) -> dict:
    return {k: json.dumps(v) if k in json_fields else v for k, v in item.items()}


def _decode(item: dict | None, json_fields: tuple[str, ...]) -> dict | None:
    if item is None:
        return None
    # Outside the JSON fields every number is a whole count (limits, minutes), so it comes back as an int.
    return {
        k: json.loads(v) if k in json_fields and isinstance(v, str)
        else int(v) if isinstance(v, Decimal) else v
        for k, v in item.items()
    }


def _event(status: str | None, actor: str, note: str | None) -> dict:
    return {"at": _now(), "status": status, "actor": actor, "note": note}


class DynamoClient:
    def __init__(self):
        dynamodb = boto3.resource("dynamodb")
        self._requests = dynamodb.Table(REQUESTS_TABLE)
        self._rules = dynamodb.Table(RULES_TABLE)

    # ── Launch Requests ───────────────────────────────────────────────────

    def create_launch_request(
        self,
        rule_type: str,
        data: dict,
        dedup_key: str,
        resolved_config: dict,
        matched_rule_id: str,
        matched_rule_name: str,
        launch_path: str,
        status: str,
        actor: str,
        note: str | None = None,
        action: str = "launch",
        target_session_ids: list | None = None,
        scheduled_time: str | None = None,
    ) -> LaunchRequest:
        now = _now()
        item: LaunchRequest = {
            "request_id": str(uuid.uuid4()),
            "action": action,
            "status": status,
            "rule_type": rule_type,
            "data": data,
            "dedup_key": dedup_key,
            "resolved_config": resolved_config,
            "matched_rule_id": matched_rule_id,
            "matched_rule_name": matched_rule_name,
            "launch_path": launch_path,
            "slack_message_ts": None,
            "slack_channel": None,
            "approved_by": None,
            "rejected_by": None,
            "session_id": None,
            "target_session_ids": target_session_ids,
            "scheduled_time": scheduled_time,
            "schedule_name": None,
            "launch_error": None,
            "events": [_event(status, actor, note)],
            "date_created": now,
            "date_modified": now,
        }
        self._requests.put_item(Item=_encode(item, REQUEST_JSON_FIELDS))
        return item

    def transition(
        self,
        request_id: str,
        expected: str | tuple[str, ...],
        status: str,
        actor: str,
        note: str | None = None,
        **fields,
    ) -> LaunchRequest | None:
        """Moves a request to `status` only if it is still in `expected`, appending to its timeline; None if it had
        moved on, so two clicks, or an approval racing a cancel, can never both act."""
        expected = (expected,) if isinstance(expected, str) else expected
        names = {"#status": "status"}
        values = {":status": status, ":event": [_event(status, actor, note)], ":empty": [], ":now": _now()}
        sets = ["#status = :status", "events = list_append(if_not_exists(events, :empty), :event)",
                "date_modified = :now"]
        for i, (key, value) in enumerate(fields.items()):
            names[f"#f{i}"] = key
            values[f":v{i}"] = json.dumps(value) if key in REQUEST_JSON_FIELDS else value
            sets.append(f"#f{i} = :v{i}")
        for i, state in enumerate(expected):
            values[f":e{i}"] = state
        try:
            response = self._requests.update_item(
                Key={"request_id": request_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=f"#status IN ({', '.join(f':e{i}' for i in range(len(expected)))})",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info("Request %s is no longer %s; not moving it to %s", request_id, expected, status)
                return None
            raise
        return _decode(response["Attributes"], REQUEST_JSON_FIELDS)

    def annotate(self, request_id: str, **fields) -> LaunchRequest:
        """Sets fields that don't change the request's status (its Slack message, schedule name), on an existing request."""
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":v{i}": value for i, value in enumerate(fields.values())}
        values[":now"] = _now()
        response = self._requests.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET " + ", ".join([*(f"#f{i} = :v{i}" for i in range(len(fields))), "date_modified = :now"]),
            ConditionExpression="attribute_exists(request_id)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return _decode(response["Attributes"], REQUEST_JSON_FIELDS)

    def get_request(self, request_id: str) -> LaunchRequest | None:
        response = self._requests.get_item(Key={"request_id": request_id})
        return _decode(response.get("Item"), REQUEST_JSON_FIELDS)

    def list_requests(self, status: str | None = None, rule_type: str | None = None, limit: int = 50) -> list[LaunchRequest]:
        if status:
            index, key = "status-date_created-index", Key("status").eq(status)
        elif rule_type:
            index, key = "rule_type-date_created-index", Key("rule_type").eq(rule_type)
        else:
            response = self._requests.scan(Limit=limit)
            return [_decode(i, REQUEST_JSON_FIELDS) for i in response.get("Items", [])]
        response = self._requests.query(IndexName=index, KeyConditionExpression=key, ScanIndexForward=False, Limit=limit)
        return [_decode(i, REQUEST_JSON_FIELDS) for i in response.get("Items", [])]

    def requests_for_rule_since(self, rule_id: str, since: datetime) -> list[LaunchRequest]:
        """The rule's requests created since `since`, newest first."""
        items, kwargs = [], {
            "IndexName": "matched_rule_id-date_created-index",
            "KeyConditionExpression": Key("matched_rule_id").eq(rule_id) & Key("date_created").gte(since.isoformat()),
            "ScanIndexForward": False,
        }
        while True:
            response = self._requests.query(**kwargs)
            items += [_decode(i, REQUEST_JSON_FIELDS) for i in response.get("Items", [])]
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def requests_with_dedup_key_since(self, dedup_key: str, minutes: int) -> list[LaunchRequest]:
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        items, kwargs = [], {
            "IndexName": "dedup_key-date_created-index",
            "KeyConditionExpression": Key("dedup_key").eq(dedup_key) & Key("date_created").gte(since),
        }
        while True:
            response = self._requests.query(**kwargs)
            items += [_decode(i, REQUEST_JSON_FIELDS) for i in response.get("Items", [])]
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    # ── Launch Rules ──────────────────────────────────────────────────────

    def get_active_rules(self, rule_type: str) -> list[LaunchRule]:
        return self._rules_of_type(Key("rule_type").eq(rule_type) & Key("status").eq("active"))

    def get_rules_of_type(self, rule_type: str) -> list[LaunchRule]:
        """Every rule of the type, active or not: a disabled or archived rule's sessions still need shutting down."""
        return self._rules_of_type(Key("rule_type").eq(rule_type))

    def _rules_of_type(self, key) -> list[LaunchRule]:
        items, kwargs = [], {"IndexName": "rule_type-status-index", "KeyConditionExpression": key}
        while True:
            response = self._rules.query(**kwargs)
            items += [_decode(i, RULE_JSON_FIELDS) for i in response.get("Items", [])]
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def get_rule(self, rule_id: str) -> LaunchRule | None:
        response = self._rules.get_item(Key={"rule_id": rule_id})
        return _decode(response.get("Item"), RULE_JSON_FIELDS)

    def list_rules(self) -> list[LaunchRule]:
        items, kwargs = [], {}
        while True:
            response = self._rules.scan(**kwargs)
            items += [_decode(i, RULE_JSON_FIELDS) for i in response.get("Items", [])]
            if "LastEvaluatedKey" not in response:
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def create_rule(self, rule: dict) -> LaunchRule:
        now = _now()
        item: LaunchRule = {**rule, "rule_id": str(uuid.uuid4()), "date_created": now, "date_modified": now}
        self._rules.put_item(Item=_encode(item, RULE_JSON_FIELDS))
        return item

    def replace_rule(self, rule: LaunchRule) -> LaunchRule:
        item = {**rule, "date_modified": _now()}
        self._rules.put_item(Item=_encode(item, RULE_JSON_FIELDS), ConditionExpression="attribute_exists(rule_id)")
        return item
