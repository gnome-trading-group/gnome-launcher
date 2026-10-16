from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    ResolvedStrategyConfig,
    RuleContext,
    RuleEvaluation,
    RuleType,
    register_rule_type,
)


@register_rule_type
class DirectLaunchRule(RuleType):
    """Launches exactly what the trigger describes: a deploy routed through the launcher, so it is recorded, deduped
    and tracked like any other launch. The rule only supplies a default strategy type and class."""

    type = "direct_launch"
    display_name = "Direct Launch"

    data_schema = {
        "type": "object",
        "properties": {
            **STRATEGY_PARAMS_SCHEMA,
            "listings": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["strategy_id", "mode", "listings"],
    }

    parameter_schema = {
        "type": "object",
        "properties": {
            "default_strategy_type": {"type": "string", "enum": ["java", "python"]},
            "default_strategy_class": {"type": "string"},
        },
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation:
        config = ResolvedStrategyConfig.from_params({
            "strategy_type": params.get("default_strategy_type", "python"),
            "strategy_class": params.get("default_strategy_class", ""),
            **data,
        }, listings=data["listings"])
        return RuleEvaluation(should_launch=True, resolved_config=config, reason="Direct launch request")
