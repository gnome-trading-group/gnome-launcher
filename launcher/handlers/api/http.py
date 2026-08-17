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


def error(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": CORS_HEADERS,
        "body": json.dumps({"message": message}),
    }


def api_handler(fn):
    @wraps(fn)
    def wrapper(event, context):
        if event.get("httpMethod") == "OPTIONS":
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
        return fn(event, context)
    return wrapper
