import hashlib
import hmac
import json
import os
import time
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest

os.environ.setdefault("APPROVE_LAUNCH_FUNCTION_NAME", "test-approve-launch")
os.environ.setdefault("LAUNCH_REQUESTS_TABLE", "gnome-launch-requests")
os.environ.setdefault("LAUNCH_RULES_TABLE", "gnome-launch-rules")
os.environ.setdefault("SLACK_CHANNEL_ID", "C123")


SIGNING_SECRET = "test-signing-secret"


def _make_payload(action_id: str, request_id: str, user_id: str = "U123", username: str = "mason") -> str:
    payload = {
        "actions": [{"action_id": action_id, "value": request_id}],
        "user": {"id": user_id, "username": username},
    }
    return urlencode({"payload": json.dumps(payload)})


def _sign(body: str, timestamp: str) -> str:
    base = f"v0:{timestamp}:{body}"
    return "v0=" + hmac.new(SIGNING_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()


def _make_event(body: str) -> dict:
    ts = str(int(time.time()))
    sig = _sign(body, ts)
    return {
        "headers": {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        },
        "body": body,
    }


def _make_request(request_id: str = "req-1", status: str = "PENDING_APPROVAL") -> dict:
    return {
        "request_id": request_id,
        "status": status,
        "matched_rule_name": "Test Rule",
        "slack_message_ts": "ts-123",
        "slack_channel": "C123",
        "resolved_config": {
            "strategy_id": 1,
            "strategy_type": "python",
            "strategy_class": "TestStrat",
            "mode": "paper",
            "listings": "1,2",
            "research_commit": None,
            "region": None,
            "strategy_args": {},
            "simulation_config": {},
        },
    }


@pytest.fixture(autouse=True)
def mock_secrets(monkeypatch):
    monkeypatch.setattr(
        "launcher.handlers.slack.interaction._get_signing_secret",
        lambda: SIGNING_SECRET,
    )


def test_reject_updates_status_and_slack():
    body = _make_payload("launch_reject", "req-1")
    event = _make_event(body)

    mock_dynamo = MagicMock()
    mock_dynamo.get_request.return_value = _make_request()
    mock_slack = MagicMock()

    import launcher.handlers.slack.interaction as si
    with patch.object(si, "DynamoClient", return_value=mock_dynamo), \
         patch.object(si, "SlackClient", return_value=mock_slack):
        result = si.handler(event, None)

    assert result["statusCode"] == 200
    mock_dynamo.update_request.assert_called_once_with("req-1", status="REJECTED", rejected_by="U123")
    mock_slack.update_message.assert_called_once()
    update_kwargs = mock_slack.update_message.call_args.kwargs
    assert "rejected" in update_kwargs["text"].lower()
    assert "mason" in update_kwargs["text"]


def test_approve_updates_status_and_fires_async():
    body = _make_payload("launch_approve", "req-1")
    event = _make_event(body)

    mock_dynamo = MagicMock()
    mock_dynamo.get_request.return_value = _make_request()
    mock_slack = MagicMock()
    mock_lambda = MagicMock()

    import launcher.handlers.slack.interaction as si
    with patch.object(si, "DynamoClient", return_value=mock_dynamo), \
         patch.object(si, "SlackClient", return_value=mock_slack), \
         patch.object(si, "_lambda_client", mock_lambda):
        result = si.handler(event, None)

    assert result["statusCode"] == 200
    mock_dynamo.update_request.assert_called_once_with("req-1", status="APPROVED", approved_by="U123")
    mock_slack.update_message.assert_called_once()
    update_kwargs = mock_slack.update_message.call_args.kwargs
    assert "launching" in update_kwargs["text"]
    mock_lambda.invoke.assert_called_once()
    invoke_kwargs = mock_lambda.invoke.call_args.kwargs
    assert invoke_kwargs["InvocationType"] == "Event"


def test_invalid_signature_returns_401():
    body = _make_payload("launch_approve", "req-1")
    event = {
        "headers": {
            "x-slack-request-timestamp": str(int(time.time())),
            "x-slack-signature": "v0=badsignature",
        },
        "body": body,
    }
    from launcher.handlers.slack.interaction import handler
    result = handler(event, None)
    assert result["statusCode"] == 401


def test_unknown_request_id_returns_200():
    body = _make_payload("launch_approve", "nonexistent")
    event = _make_event(body)

    mock_dynamo = MagicMock()
    mock_dynamo.get_request.return_value = None

    import launcher.handlers.slack.interaction as si
    with patch.object(si, "DynamoClient", return_value=mock_dynamo):
        result = si.handler(event, None)

    assert result["statusCode"] == 200
