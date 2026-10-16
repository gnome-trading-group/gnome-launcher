"""Launch and shutdown requests for the controller.

    GET  /launch-requests?rule_id=&rule_type=&status=&action=&before=&limit=   newest first; page with before=
    GET  /launch-requests/{id}                                                 the request and its trigger's log
    POST /launch-requests/{id}/approve|reject|cancel|retry

Approving here does what a Slack click does (the same conditional transition, then the same approve Lambda), so
the two can race safely: whichever lands second is told the request has moved on.
"""
import json
import logging
import os

import boto3

from launcher import lifecycle
from launcher.dynamo import DynamoClient
from launcher.handlers.api.http import actor, api_handler, error, ok, query_int
from launcher.models import LaunchRequest
from launcher.rules.catalog import RULE_TYPE_REGISTRY
from launcher.slack_client import SlackClient

logger = logging.getLogger(__name__)

MAX_PAGE = 200
APPROVE_LAUNCH_FUNCTION_NAME = os.environ.get("APPROVE_LAUNCH_FUNCTION_NAME", "")
APPROVE_SHUTDOWN_FUNCTION_NAME = os.environ.get("APPROVE_SHUTDOWN_FUNCTION_NAME", "")


@api_handler
def handler(event, context):
    dynamo = DynamoClient()
    params = event.get("pathParameters") or {}
    request_id, action = params.get("id"), params.get("action")

    if event.get("httpMethod") == "GET" and request_id:
        request = dynamo.get_request(request_id)
        if not request:
            return error(404, f"Request not found: {request_id}")
        evaluations = dynamo.evaluations_for_trigger(request["trigger_id"]) if request.get("trigger_id") else []
        return ok({**request, "evaluations": evaluations})

    if event.get("httpMethod") == "GET":
        query = event.get("queryStringParameters") or {}
        limit = query_int(event, "limit", 50, MAX_PAGE)
        if limit is None:
            return error(400, f"limit must be between 1 and {MAX_PAGE}")
        rule_types = [query["rule_type"]] if query.get("rule_type") else list(RULE_TYPE_REGISTRY)
        items = dynamo.list_requests(rule_id=query.get("rule_id"), rule_types=rule_types, status=query.get("status"),
                                     action=query.get("action"), before=query.get("before"), limit=limit)
        return ok({"items": items, "next_before": items[-1]["date_created"] if len(items) == limit else None})

    request = dynamo.get_request(request_id) if request_id else None
    if request is None:
        return error(404, f"Request not found: {request_id}")
    who = actor(event)
    if action == "approve":
        return _approve(request, who, dynamo)
    if action == "reject":
        return _reject(request, who, dynamo)
    if action == "cancel":
        return _cancel(request, who, dynamo)
    if action == "retry":
        return _retry(request, who, dynamo)
    return error(400, f"Unknown action: {action}")


def _moved_on(request: LaunchRequest, dynamo: DynamoClient):
    current = dynamo.get_request(request["request_id"]) or request
    return error(409, f"The request is {current['status']}; nothing was done")


def _invoke(function_name: str, request_id: str, who: str) -> None:
    boto3.client("lambda").invoke(FunctionName=function_name, InvocationType="Event", Payload=json.dumps({
        "request_id": request_id, "username": who.removeprefix("ui:"), "actor": who}).encode())


def _resolve_slack(request: LaunchRequest, text: str) -> None:
    # Replaces the message's buttons, so nobody clicks them in Slack after it was decided in the controller.
    SlackClient().update_message(request.get("slack_message_ts"), text,
                                 blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])


def _approve(request: LaunchRequest, who: str, dynamo: DynamoClient):
    if request["status"] == "PENDING_SHUTDOWN_APPROVAL":
        approved = dynamo.transition(request["request_id"], "PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_APPROVED", who,
                                     approved_by=who)
        function_name, text = APPROVE_SHUTDOWN_FUNCTION_NAME, f"Shutdown approved by {who} in the controller"
    else:
        approved = dynamo.transition(request["request_id"], "PENDING_APPROVAL", "APPROVED", who, approved_by=who)
        function_name, text = APPROVE_LAUNCH_FUNCTION_NAME, f"Launch approved by {who} in the controller"
    if approved is None:
        return _moved_on(request, dynamo)
    _resolve_slack(request, text)
    _invoke(function_name, request["request_id"], who)
    return ok(approved, 202)


def _reject(request: LaunchRequest, who: str, dynamo: DynamoClient):
    if request["status"] == "PENDING_SHUTDOWN_APPROVAL":
        rejected = dynamo.transition(request["request_id"], "PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_REJECTED", who,
                                     rejected_by=who)
    else:
        rejected = dynamo.transition(request["request_id"], "PENDING_APPROVAL", "REJECTED", who, rejected_by=who)
    if rejected is None:
        return _moved_on(request, dynamo)
    _resolve_slack(request, f"Rejected by {who} in the controller")
    return ok(rejected)


def _cancel(request: LaunchRequest, who: str, dynamo: DynamoClient):
    cancelled = dynamo.transition(request["request_id"], "SCHEDULED", "CANCELLED", who, rejected_by=who)
    if cancelled is None:
        return _moved_on(request, dynamo)
    lifecycle.delete_schedule(request.get("schedule_name"))
    _resolve_slack(request, f"Scheduled launch cancelled by {who} in the controller")
    return ok(cancelled)


def _retry(request: LaunchRequest, who: str, dynamo: DynamoClient):
    """A failed launch, tried again as a new request with the same resolved config. It starts APPROVED and goes
    through the approve Lambda, which re-checks the listings before launching."""
    if request.get("action", "launch") != "launch" or request["status"] != "FAILED":
        return error(409, f"Only a FAILED launch can be retried; this one is {request['status']}")
    rule = dynamo.get_rule(request["matched_rule_id"])
    if rule is None or rule["status"] != "active":
        return error(409, "The rule that made this request is no longer active")
    retry = dynamo.create_launch_request(
        rule_type=request["rule_type"], data=request["data"], dedup_key=request["dedup_key"],
        resolved_config=request["resolved_config"], matched_rule_id=rule["rule_id"], matched_rule_name=rule["name"],
        launch_path="approval", status="APPROVED", actor=who, note=f"retry of {request['request_id']}",
        trigger_id=request.get("trigger_id"), rule_revision=request.get("rule_revision"),
        retry_of=request["request_id"],
    )
    _invoke(APPROVE_LAUNCH_FUNCTION_NAME, retry["request_id"], who)
    return ok(retry, 202)
