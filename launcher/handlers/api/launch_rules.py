"""Launch rules: create, read, change and archive. Every write is validated as a whole rule (an edit is merged
into the stored rule first), so a rule that would fail when its next trigger arrives is refused when saved.
Rules are archived rather than deleted: launch history keeps pointing at them, and the shutdown processor still
stops the sessions an archived rule launched."""
import json
import logging

from launcher.dynamo import DynamoClient
from launcher.handlers.api.http import api_handler, error, ok
from launcher.rules.validation import EDITABLE_FIELDS, RULE_DEFAULTS, validate_rule

logger = logging.getLogger(__name__)


@api_handler
def handler(event, context):
    dynamo = DynamoClient()
    method = event.get("httpMethod", "")
    rule_id = (event.get("pathParameters") or {}).get("id")

    if method == "GET" and rule_id:
        rule = dynamo.get_rule(rule_id)
        return ok(rule) if rule else error(404, f"Rule not found: {rule_id}")
    if method == "GET":
        return ok(dynamo.list_rules())

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return error(400, "The body must be a JSON object")

    if method == "POST":
        rule = {**RULE_DEFAULTS, **body}
        problems = validate_rule(rule)
        if problems:
            return error(400, "Invalid rule", problems)
        return ok(dynamo.create_rule(rule), 201)

    existing = dynamo.get_rule(rule_id) if rule_id else None
    if existing is None:
        return error(404, f"Rule not found: {rule_id}")

    if method == "PATCH":
        unknown = set(body) - set(EDITABLE_FIELDS)
        if unknown:
            return error(400, f"Not editable: {', '.join(sorted(unknown))}")
        rule = {**existing, **body}
        problems = validate_rule({k: v for k, v in rule.items() if k not in ("rule_id", "date_created", "date_modified")})
        if problems:
            return error(400, "Invalid rule", problems)
        return ok(dynamo.replace_rule(rule))

    if method == "DELETE":
        return ok(dynamo.replace_rule({**existing, "status": "archived"}))

    return error(400, f"Unsupported method: {method}")
