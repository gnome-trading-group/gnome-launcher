from unittest.mock import MagicMock

import requests

from launcher import alerts
from launcher.rules.types import ResolvedStrategyConfig


def _config():
    return ResolvedStrategyConfig(
        strategy_id=7, strategy_type="python", strategy_class="Strat", mode="live", listings=[1, 2],
        research_commit=None, region=None, strategy_args={}, simulation_config={},
    )


def test_a_refused_launch_says_why():
    registry = MagicMock()
    response = requests.Response()
    response.status_code = 422
    response._content = b'{"message":"Missing MAX_POSITION for listing 2"}'
    error = requests.HTTPError("422 Client Error", response=response)

    alerts.launch_failed(registry, "cs2-prematch", "req-1", error, _config())

    args, kwargs = registry.send_alert.call_args
    assert args == ("LAUNCH_FAILED", "cs2-prematch couldn't launch")
    assert kwargs["key"] == "LAUNCH_FAILED:req-1"
    assert "Missing MAX_POSITION for listing 2" in kwargs["body"]
    assert kwargs["actor"] == "launcher:cs2-prematch"
    assert kwargs["mode"] == "live"


def test_each_session_that_couldnt_be_stopped_is_its_own_alert():
    registry = MagicMock()

    alerts.shutdown_failed(registry, "cs2-prematch", [{"session_id": "a", "error": "x"}, {"session_id": "b", "error": "y"}])

    assert [c.kwargs["session_id"] for c in registry.send_alert.call_args_list] == ["a", "b"]
    assert all(c.args[0] == "SHUTDOWN_FAILED" for c in registry.send_alert.call_args_list)


def test_alerting_failing_never_stops_the_launcher():
    registry = MagicMock()
    registry.send_alert.side_effect = RuntimeError("alerts API down")

    alerts.stopped(registry, "cs2-prematch", "match over", ["a"])
    alerts.launched(registry, "cs2-prematch", "pre-match window", _config(), "s1")
