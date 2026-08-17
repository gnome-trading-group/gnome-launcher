import os
import uuid
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

from launcher.models import LaunchRequest, LaunchRule

logger = logging.getLogger(__name__)

REQUESTS_TABLE = os.environ.get("LAUNCH_REQUESTS_TABLE", "gnome-launch-requests")
RULES_TABLE = os.environ.get("LAUNCH_RULES_TABLE", "gnome-launch-rules")

_TTL_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl() -> int:
    return int(datetime.now(timezone.utc).timestamp()) + _TTL_DAYS * 86400


class DynamoClient:
    def __init__(self):
        dynamodb = boto3.resource("dynamodb")
        self._requests = dynamodb.Table(REQUESTS_TABLE)
        self._rules = dynamodb.Table(RULES_TABLE)

    # ── Launch Requests ───────────────────────────────────────────────────

    def find_by_dedup_key(self, dedup_key: str, dedup_window_minutes: int | None = None) -> LaunchRequest | None:
        response = self._requests.query(
            IndexName="dedup_key-date_created-index",
            KeyConditionExpression=Key("dedup_key").eq(dedup_key),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        if not items:
            return None

        item = items[0]
        if dedup_window_minutes is None:
            return item

        created = datetime.fromisoformat(item["date_created"])
        age_minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60
        if age_minutes <= dedup_window_minutes:
            return item
        return None

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
    ) -> LaunchRequest:
        now = _now()
        item: LaunchRequest = {
            "request_id": str(uuid.uuid4()),
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
            "launch_error": None,
            "date_created": now,
            "date_modified": now,
            "ttl": _ttl(),
        }
        self._requests.put_item(Item=item)
        return item

    def update_request(self, request_id: str, **fields) -> LaunchRequest:
        fields["date_modified"] = _now()
        update_expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i, _ in enumerate(fields))
        attr_names = {f"#f{i}": k for i, k in enumerate(fields)}
        attr_values = {f":v{i}": v for i, v in enumerate(fields.values())}
        response = self._requests.update_item(
            Key={"request_id": request_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
            ReturnValues="ALL_NEW",
        )
        return response["Attributes"]

    def get_request(self, request_id: str) -> LaunchRequest | None:
        response = self._requests.get_item(Key={"request_id": request_id})
        return response.get("Item")

    def list_requests(self, status: str | None = None, rule_type: str | None = None, limit: int = 50) -> list[LaunchRequest]:
        if status:
            response = self._requests.query(
                IndexName="status-date_created-index",
                KeyConditionExpression=Key("status").eq(status),
                ScanIndexForward=False,
                Limit=limit,
            )
            return response.get("Items", [])

        if rule_type:
            response = self._requests.query(
                IndexName="rule_type-date_created-index",
                KeyConditionExpression=Key("rule_type").eq(rule_type),
                ScanIndexForward=False,
                Limit=limit,
            )
            return response.get("Items", [])

        response = self._requests.scan(Limit=limit)
        return response.get("Items", [])

    def get_latest_launch_for_rule(self, rule_id: str) -> LaunchRequest | None:
        response = self._requests.query(
            IndexName="matched_rule_id-date_created-index",
            KeyConditionExpression=Key("matched_rule_id").eq(rule_id),
            FilterExpression="#s IN (:launched, :approved)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":launched": "LAUNCHED", ":approved": "APPROVED"},
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return items[0] if items else None

    # ── Launch Rules ──────────────────────────────────────────────────────

    def get_active_rules(self, rule_type: str) -> list[LaunchRule]:
        response = self._rules.query(
            IndexName="rule_type-status-index",
            KeyConditionExpression=Key("rule_type").eq(rule_type) & Key("status").eq("active"),
        )
        return response.get("Items", [])

    def get_rule(self, rule_id: str) -> LaunchRule | None:
        response = self._rules.get_item(Key={"rule_id": rule_id})
        return response.get("Item")

    def list_rules(self) -> list[LaunchRule]:
        response = self._rules.scan()
        return response.get("Items", [])

    def create_rule(
        self,
        name: str,
        rule_type: str,
        launch_path: str,
        parameters: dict,
        description: str | None = None,
        max_concurrent_sessions: int | None = None,
        cooldown_minutes: int = 0,
        dedup_window_minutes: int = 60,
    ) -> LaunchRule:
        now = _now()
        item: LaunchRule = {
            "rule_id": str(uuid.uuid4()),
            "name": name,
            "description": description,
            "rule_type": rule_type,
            "status": "active",
            "launch_path": launch_path,
            "max_concurrent_sessions": max_concurrent_sessions,
            "cooldown_minutes": cooldown_minutes,
            "dedup_window_minutes": dedup_window_minutes,
            "parameters": parameters,
            "date_created": now,
            "date_modified": now,
        }
        self._rules.put_item(Item=item)
        return item

    def update_rule(self, rule_id: str, **fields) -> LaunchRule:
        fields["date_modified"] = _now()
        update_expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i, _ in enumerate(fields))
        attr_names = {f"#f{i}": k for i, k in enumerate(fields)}
        attr_values = {f":v{i}": v for i, v in enumerate(fields.values())}
        response = self._rules.update_item(
            Key={"rule_id": rule_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
            ReturnValues="ALL_NEW",
        )
        return response["Attributes"]

    def delete_rule(self, rule_id: str):
        self._rules.delete_item(Key={"rule_id": rule_id})
