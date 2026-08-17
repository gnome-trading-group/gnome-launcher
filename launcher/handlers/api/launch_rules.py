import json
import logging

from launcher.dynamo import DynamoClient
from launcher.handlers.api.http import api_handler, error, ok

logger = logging.getLogger(__name__)


@api_handler
def handler(event, context):
    dynamo = DynamoClient()
    method = event.get("httpMethod", "")
    path_params = event.get("pathParameters") or {}
    rule_id = path_params.get("id")

    if method == "GET" and rule_id:
        item = dynamo.get_rule(rule_id)
        if not item:
            return error(404, f"Rule not found: {rule_id}")
        return ok(item)

    if method == "GET":
        return ok(dynamo.list_rules())

    if method == "POST":
        return _handle_create(event, dynamo)

    if method == "PATCH" and rule_id:
        return _handle_update(event, dynamo, rule_id)

    if method == "DELETE" and rule_id:
        existing = dynamo.get_rule(rule_id)
        if not existing:
            return error(404, f"Rule not found: {rule_id}")
        dynamo.delete_rule(rule_id)
        return ok({"deleted": rule_id})

    return error(400, f"Unsupported method: {method}")


def _handle_create(event, dynamo: DynamoClient) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error(400, "Invalid JSON body")

    required = {"name", "rule_type", "launch_path", "parameters"}
    missing = required - set(body)
    if missing:
        return error(400, f"Missing required fields: {', '.join(sorted(missing))}")

    item = dynamo.create_rule(
        name=body["name"],
        rule_type=body["rule_type"],
        launch_path=body["launch_path"],
        parameters=body["parameters"],
        description=body.get("description"),
        max_concurrent_sessions=body.get("max_concurrent_sessions"),
        cooldown_minutes=body.get("cooldown_minutes", 0),
        dedup_window_minutes=body.get("dedup_window_minutes", 60),
    )
    return ok(item, 201)


def _handle_update(event, dynamo: DynamoClient, rule_id: str) -> dict:
    existing = dynamo.get_rule(rule_id)
    if not existing:
        return error(404, f"Rule not found: {rule_id}")

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error(400, "Invalid JSON body")

    allowed = {"name", "description", "status", "launch_path", "parameters",
               "max_concurrent_sessions", "cooldown_minutes", "dedup_window_minutes"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return error(400, "No updatable fields provided")

    updated = dynamo.update_rule(rule_id, **updates)
    return ok(updated)
