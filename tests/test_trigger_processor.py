from types import SimpleNamespace
from unittest.mock import MagicMock

from launcher.rules.types import RuleContext, RuleEvaluation, ResolvedStrategyConfig, RuleType, RULE_TYPE_REGISTRY


def _make_resolved_config(**kwargs):
    defaults = dict(
        strategy_id=1, strategy_type="python", strategy_class="Strat",
        mode="paper", listings="1,2", research_commit=None, region=None,
        strategy_args={}, simulation_config={},
    )
    return ResolvedStrategyConfig(**{**defaults, **kwargs})


def _make_ctx(rules=None, existing_request=None, active_sessions=None):
    ctx = MagicMock()
    ctx.dynamo.get_active_rules.return_value = rules or []
    ctx.dynamo.find_by_dedup_key.return_value = existing_request
    ctx.dynamo.create_launch_request.return_value = {"request_id": "req-1"}
    ctx.dynamo.update_request.return_value = {}
    ctx.registry.get_strategy_sessions.return_value = active_sessions or []
    ctx.registry.create_strategy_session.return_value = None
    ctx.slack.send_launch_notification.return_value = "ts-123"
    ctx.slack.send_approval_message.return_value = "ts-456"
    ctx.slack.channel = "C123"
    return ctx


def _make_rule(rule_id="rule-1", launch_path="auto", rule_type="test_proc_match"):
    return {
        "rule_id": rule_id,
        "name": "Test Rule",
        "rule_type": rule_type,
        "launch_path": launch_path,
        "parameters": {},
        "status": "active",
        "dedup_window_minutes": 60,
        "cooldown_minutes": 0,
    }


class ProcMatchRule(RuleType):
    type = "test_proc_match"
    display_name = "Proc Match"
    parameter_schema = {}
    data_schema = {}

    def evaluate(self, data, params, ctx):
        return RuleEvaluation(
            should_launch=True,
            resolved_config=_make_resolved_config(),
            reason="test match",
        )


def setup_function():
    RULE_TYPE_REGISTRY["test_proc_match"] = ProcMatchRule


def _process(message: dict, ctx: RuleContext):
    from launcher.handlers.trigger_processor import _process_message
    _process_message(message, ctx)


def test_drops_message_when_no_active_rules():
    ctx = _make_ctx(rules=[])
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    ctx.dynamo.create_launch_request.assert_not_called()


def test_drops_message_on_dedup_hit():
    ctx = _make_ctx(
        rules=[_make_rule()],
        existing_request={"request_id": "old-req"},
    )
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    ctx.dynamo.create_launch_request.assert_not_called()


def test_auto_launch_creates_request_and_launches():
    ctx = _make_ctx(rules=[_make_rule(launch_path="auto")])
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    ctx.dynamo.create_launch_request.assert_called_once()
    ctx.registry.create_strategy_session.assert_called_once()
    ctx.slack.send_launch_notification.assert_called_once()


def test_approval_path_sends_slack_message():
    ctx = _make_ctx(rules=[_make_rule(launch_path="approval")])
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    ctx.dynamo.create_launch_request.assert_called_once()
    ctx.registry.create_strategy_session.assert_not_called()
    ctx.slack.send_approval_message.assert_called_once()


def test_skips_when_active_session_has_same_listings():
    ctx = _make_ctx(
        rules=[_make_rule(launch_path="auto")],
        active_sessions=[SimpleNamespace(config={"listings": "1,2"})],
    )
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    ctx.dynamo.create_launch_request.assert_not_called()


def test_launches_when_active_session_has_different_listings():
    ctx = _make_ctx(
        rules=[_make_rule(launch_path="auto")],
        active_sessions=[SimpleNamespace(config={"listings": "99,100"})],
    )
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    ctx.registry.create_strategy_session.assert_called_once()


def test_failed_launch_updates_status_and_notifies():
    ctx = _make_ctx(rules=[_make_rule(launch_path="auto")])
    ctx.registry.create_strategy_session.side_effect = RuntimeError("ECS failed")
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    all_kwargs = [call.kwargs for call in ctx.dynamo.update_request.call_args_list]
    assert any(kw.get("status") == "FAILED" for kw in all_kwargs)
    ctx.slack.send_failure_notification.assert_called_once()


def test_multiple_matching_rules_all_fire():
    ctx = _make_ctx(rules=[
        _make_rule(rule_id="rule-1", launch_path="auto"),
        _make_rule(rule_id="rule-2", launch_path="auto"),
    ])
    _process({"rule_type": "test_proc_match", "data": {}}, ctx)
    assert ctx.dynamo.create_launch_request.call_count == 2
    assert ctx.registry.create_strategy_session.call_count == 2
