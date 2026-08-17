from unittest.mock import MagicMock

from launcher.rules.engine import evaluate_rules
from launcher.rules.types import RuleContext, RuleEvaluation, ResolvedStrategyConfig, RuleType, RULE_TYPE_REGISTRY


def _make_ctx():
    ctx = MagicMock()
    ctx.registry.get_strategy_sessions.return_value = []
    ctx.dynamo.get_latest_launch_for_rule.return_value = None
    return ctx


def _make_resolved_config(**kwargs):
    defaults = dict(
        strategy_id=1, strategy_type="python", strategy_class="TestStrategy",
        mode="paper", listings="1,2", research_commit=None, region=None,
        strategy_args={}, simulation_config={},
    )
    return ResolvedStrategyConfig(**{**defaults, **kwargs})


def _make_rule(rule_type="test_type", launch_path="auto", **kwargs):
    return {
        "rule_id": "rule-1",
        "name": "Test Rule",
        "rule_type": rule_type,
        "launch_path": launch_path,
        "parameters": {},
        "status": "active",
        **kwargs,
    }


class AlwaysMatchRule(RuleType):
    type = "test_always_match"
    display_name = "Always Match"
    parameter_schema = {}
    data_schema = {}

    def evaluate(self, data, params, ctx):
        return RuleEvaluation(
            should_launch=True,
            resolved_config=_make_resolved_config(),
            reason="always matches",
        )


class NeverMatchRule(RuleType):
    type = "test_never_match"
    display_name = "Never Match"
    parameter_schema = {}
    data_schema = {}

    def evaluate(self, data, params, ctx):
        return None


def setup_function():
    RULE_TYPE_REGISTRY["test_always_match"] = AlwaysMatchRule
    RULE_TYPE_REGISTRY["test_never_match"] = NeverMatchRule


def test_evaluate_returns_all_matches():
    ctx = _make_ctx()
    rules = [
        _make_rule(rule_type="test_always_match", rule_id="rule-1"),
        _make_rule(rule_type="test_always_match", rule_id="rule-2"),
    ]
    matches = evaluate_rules(rules, {}, ctx)
    assert len(matches) == 2


def test_evaluate_skips_no_match_rules():
    ctx = _make_ctx()
    rules = [
        _make_rule(rule_type="test_never_match"),
    ]
    matches = evaluate_rules(rules, {}, ctx)
    assert len(matches) == 0


def test_evaluate_skips_unknown_rule_type():
    ctx = _make_ctx()
    rules = [_make_rule(rule_type="does_not_exist")]
    matches = evaluate_rules(rules, {}, ctx)
    assert len(matches) == 0


def test_evaluate_skips_when_max_concurrent_reached():
    ctx = _make_ctx()
    ctx.registry.get_strategy_sessions.return_value = [{"status": "RUNNING"}]
    rules = [_make_rule(rule_type="test_always_match", max_concurrent_sessions=1, parameters={"strategy_id": 1})]
    matches = evaluate_rules(rules, {}, ctx)
    assert len(matches) == 0


def test_evaluate_allows_when_under_max_concurrent():
    ctx = _make_ctx()
    ctx.registry.get_strategy_sessions.return_value = []
    rules = [_make_rule(rule_type="test_always_match", max_concurrent_sessions=2, parameters={"strategy_id": 1})]
    matches = evaluate_rules(rules, {}, ctx)
    assert len(matches) == 1


def test_mixed_rules_returns_only_matches():
    ctx = _make_ctx()
    rules = [
        _make_rule(rule_type="test_always_match", rule_id="r1"),
        _make_rule(rule_type="test_never_match", rule_id="r2"),
        _make_rule(rule_type="test_always_match", rule_id="r3"),
    ]
    matches = evaluate_rules(rules, {}, ctx)
    assert len(matches) == 2
    matched_ids = {m.rule["rule_id"] for m in matches}
    assert matched_ids == {"r1", "r3"}
