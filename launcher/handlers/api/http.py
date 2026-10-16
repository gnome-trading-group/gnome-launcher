import json
from functools import wraps
from typing import Any

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
}


def ok(body: Any, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, default=str),
    }


def error(status: int, message: str, problems: list[str] | None = None) -> dict:
    body: dict = {"message": message}
    if problems:
        body["problems"] = problems
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps(body),
    }


def api_handler(fn):
    @wraps(fn)
    def wrapper(event, context):
        if event.get("httpMethod") == "OPTIONS":
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
        return fn(event, context)
    return wrapper


def actor(event: dict) -> str:
    """Who made an API call: the operator's email from their Cognito token."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    return f"ui:{claims.get('email') or claims.get('cognito:username') or 'unknown'}"


def body_of(event: dict) -> dict | None:
    """The request body as a JSON object, or None if it isn't one."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def query_int(event: dict, name: str, default: int, maximum: int) -> int | None:
    raw = (event.get("queryStringParameters") or {}).get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 < value <= maximum else None
