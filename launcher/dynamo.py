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
TRIGGERS_TABLE = os.environ.get("LAUNCH_TRIGGERS_TABLE", "gnome-launch-triggers")
REVISIONS_TABLE = os.environ.get("LAUNCH_RULE_REVISIONS_TABLE", "gnome-launch-rule-revisions")

# Trigger evaluations that led nowhere are kept a month, long enough to answer "why didn't this launch?"; one that
# made a request is kept as long as the request.
SKIPPED_TRIGGER_DAYS = 30
TRIGGER_LEVEL = "-"
# Set on a launched request while its session is still going, so the sweeper finds just those through a sparse index.
TRACKING_OPEN = "open"

# Free-form JSON is stored as a string: DynamoDB takes no floats and returns every number as a Decimal, so a nested
# map would come back with 2.0 turned into 2 and every number in a type neither strategies nor the registry accept.
RULE_JSON_FIELDS = ("parameters",)
REQUEST_JSON_FIELDS = ("data", "resolved_config")
TRIGGER_JSON_FIELDS = ("data",)
REVISION_JSON_FIELDS = ("rule",)


class RevisionConflict(Exception):
    """The rule changed since the caller read it."""


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


def _query_all(table, **kwargs) -> list[dict]:
    items = []
    while True:
        response = table.query(**kwargs)
        items += response.get("Items", [])
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _query_newest(table, index: str, key, limit: int, keep=lambda item: True) -> list[dict]:
    """Up to `limit` items of an index, newest first, that pass `keep`; reads on until it has them."""
    items, kwargs = [], {"IndexName": index, "KeyConditionExpression": key, "ScanIndexForward": False}
    while len(items) < limit:
        response = table.query(**kwargs)
        items += [i for i in response.get("Items", []) if keep(i)]
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items[:limit]


class DynamoClient:
    def __init__(self):
        dynamodb = boto3.resource("dynamodb")
        self._requests = dynamodb.Table(REQUESTS_TABLE)
        self._rules = dynamodb.Table(RULES_TABLE)
        self._triggers = dynamodb.Table(TRIGGERS_TABLE)
        self._revisions = dynamodb.Table(REVISIONS_TABLE)

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
        trigger_id: str | None = None,
        rule_revision: int | None = None,
        retry_of: str | None = None,
    ) -> LaunchRequest:
        now = _now()
        item: LaunchRequest = {
            "request_id": str(uuid.uuid4()),
            "trigger_id": trigger_id,
            "rule_revision": rule_revision,
            "retry_of": retry_of,
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

    def list_requests(self, *, rule_id: str | None = None, rule_types: list[str] | None = None,
                      status: str | None = None, action: str | None = None, before: str | None = None,
                      limit: int = 50) -> list[LaunchRequest]:
        """Requests newest first, created before `before` (an ISO time; the next page starts at the last one's
        date_created). Without a rule or status, the newest across `rule_types` are merged."""
        def keep(item):
            return (action is None or item.get("action", "launch") == action) and \
                   (status is None or item["status"] == status)

        def created(key):
            return key & Key("date_created").lt(before) if before else key

        if rule_id:
            items = _query_newest(self._requests, "matched_rule_id-date_created-index",
                                  created(Key("matched_rule_id").eq(rule_id)), limit, keep)
        elif status:
            items = _query_newest(self._requests, "status-date_created-index",
                                  created(Key("status").eq(status)), limit, keep)
        else:
            items = [i for rule_type in rule_types or [] for i in _query_newest(
                self._requests, "rule_type-date_created-index", created(Key("rule_type").eq(rule_type)), limit, keep)]
            items = sorted(items, key=lambda i: i["date_created"], reverse=True)[:limit]
        return [_decode(i, REQUEST_JSON_FIELDS) for i in items]

    def requests_in_status(self, status: str) -> list[LaunchRequest]:
        return [_decode(i, REQUEST_JSON_FIELDS) for i in _query_all(
            self._requests, IndexName="status-date_created-index", KeyConditionExpression=Key("status").eq(status))]

    def tracked_requests(self) -> list[LaunchRequest]:
        """Launched requests whose session hasn't ended yet."""
        return [_decode(i, REQUEST_JSON_FIELDS) for i in _query_all(
            self._requests, IndexName="tracking-date_created-index", KeyConditionExpression=Key("tracking").eq(TRACKING_OPEN))]

    def record_session(self, request_id: str, ended: bool, **fields) -> None:
        """Copies the session's state onto its request; once it has ended the request leaves the tracked index."""
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":v{i}": value for i, value in enumerate(fields.values())}
        update = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        if ended:
            update += " REMOVE tracking"
        else:
            names["#tracking"], values[":open"] = "tracking", TRACKING_OPEN
            update += ", #tracking = :open"
        self._requests.update_item(Key={"request_id": request_id}, UpdateExpression=update,
                                   ConditionExpression="attribute_exists(request_id)",
                                   ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def requests_for_rule_since(self, rule_id: str, since: datetime) -> list[LaunchRequest]:
        """The rule's requests created since `since`, newest first."""
        return [_decode(i, REQUEST_JSON_FIELDS) for i in _query_all(
            self._requests, IndexName="matched_rule_id-date_created-index", ScanIndexForward=False,
            KeyConditionExpression=Key("matched_rule_id").eq(rule_id) & Key("date_created").gte(since.isoformat()))]

    def requests_with_dedup_key_since(self, dedup_key: str, minutes: int) -> list[LaunchRequest]:
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        return [_decode(i, REQUEST_JSON_FIELDS) for i in _query_all(
            self._requests, IndexName="dedup_key-date_created-index",
            KeyConditionExpression=Key("dedup_key").eq(dedup_key) & Key("date_created").gte(since))]

    # ── Trigger log ───────────────────────────────────────────────────────

    def record_evaluation(self, trigger: dict, outcome: str, reason: str | None = None, rule: dict | None = None,
                          request_id: str | None = None) -> None:
        """One trigger's outcome for one rule (or, with no rule, for the trigger as a whole)."""
        now = datetime.now(timezone.utc)
        item = {
            "trigger_id": trigger["trigger_id"],
            "rule_id": rule["rule_id"] if rule else TRIGGER_LEVEL,
            "rule_name": rule["name"] if rule else None,
            "rule_type": trigger["rule_type"],
            "action": trigger.get("action", "launch"),
            "data": trigger["data"],
            "received_at": trigger.get("timestamp") or now.isoformat(),
            "evaluated_at": now.isoformat(),
            "outcome": outcome,
            "reason": reason,
            "request_id": request_id,
        }
        if request_id is None:
            item["ttl"] = int((now + timedelta(days=SKIPPED_TRIGGER_DAYS)).timestamp())
        self._triggers.put_item(Item=_encode(item, TRIGGER_JSON_FIELDS))

    def evaluations_for_trigger(self, trigger_id: str) -> list[dict]:
        return [_decode(i, TRIGGER_JSON_FIELDS) for i in _query_all(
            self._triggers, KeyConditionExpression=Key("trigger_id").eq(trigger_id))]

    def evaluations(self, *, rule_id: str | None = None, rule_type: str | None = None, before: str | None = None,
                    limit: int = 50) -> list[dict]:
        """Newest first, for one rule or one rule type, received before `before`."""
        index, key = ("rule_id-received_at-index", Key("rule_id").eq(rule_id)) if rule_id else \
            ("rule_type-received_at-index", Key("rule_type").eq(rule_type))
        if before:
            key = key & Key("received_at").lt(before)
        return [_decode(i, TRIGGER_JSON_FIELDS) for i in _query_newest(self._triggers, index, key, limit)]

    def evaluations_since(self, rule_id: str, since: datetime) -> list[dict]:
        return [_decode(i, TRIGGER_JSON_FIELDS) for i in _query_all(
            self._triggers, IndexName="rule_id-received_at-index",
            KeyConditionExpression=Key("rule_id").eq(rule_id) & Key("received_at").gte(since.isoformat()))]

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

    def create_rule(self, rule: dict, actor: str) -> LaunchRule:
        now = _now()
        item: LaunchRule = {**rule, "rule_id": str(uuid.uuid4()), "revision": 1, "date_created": now,
                            "date_modified": now, "created_by": actor, "modified_by": actor}
        self._rules.put_item(Item=_encode(item, RULE_JSON_FIELDS))
        self._put_revision(item, actor, sorted(rule))
        return item

    def replace_rule(self, rule: LaunchRule, actor: str, changed: list[str]) -> LaunchRule:
        """Saves an edit as the next revision, refusing it if someone else saved one since `rule` was read."""
        item = {**rule, "revision": rule["revision"] + 1, "date_modified": _now(), "modified_by": actor}
        try:
            self._rules.put_item(Item=_encode(item, RULE_JSON_FIELDS),
                                 ConditionExpression="revision = :read",
                                 ExpressionAttributeValues={":read": rule["revision"]})
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise RevisionConflict(rule["rule_id"]) from e
            raise
        self._put_revision(item, actor, changed)
        return item

    def _put_revision(self, rule: LaunchRule, actor: str, changed: list[str]) -> None:
        self._revisions.put_item(Item=_encode({
            "rule_id": rule["rule_id"], "revision": rule["revision"], "rule": rule,
            "actor": actor, "at": rule["date_modified"], "changed": changed,
        }, REVISION_JSON_FIELDS))

    def list_revisions(self, rule_id: str) -> list[dict]:
        return [_decode(i, REVISION_JSON_FIELDS) for i in _query_all(
            self._revisions, KeyConditionExpression=Key("rule_id").eq(rule_id), ScanIndexForward=False)]
