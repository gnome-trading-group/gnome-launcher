from launcher.rules.types import RuleContext, RuleEvaluation, ResolvedStrategyConfig, register_rule_type, RuleType


@register_rule_type
class DirectLaunchRule(RuleType):
    type = "direct_launch"
    display_name = "Direct Launch"

    data_schema = {
        "type": "object",
        "properties": {
            "strategy_id": {"type": "integer"},
            "strategy_type": {"type": "string"},
            "strategy_class": {"type": "string"},
            "mode": {"type": "string"},
            "listings": {"type": "string"},
            "research_commit": {"type": "string"},
            "strategy_args": {"type": "object", "additionalProperties": {"type": "string"}},
            "simulation_config": {"type": "object", "additionalProperties": {"type": "string"}},
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

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | None:
        return RuleEvaluation(
            should_launch=True,
            resolved_config=ResolvedStrategyConfig(
                strategy_id=data["strategy_id"],
                strategy_type=data.get("strategy_type", params.get("default_strategy_type", "python")),
                strategy_class=data.get("strategy_class", params.get("default_strategy_class", "")),
                mode=data["mode"],
                listings=data["listings"],
                research_commit=data.get("research_commit"),
                strategy_args=data.get("strategy_args", {}),
                simulation_config=data.get("simulation_config", {}),
            ),
            reason="Direct launch request",
        )
