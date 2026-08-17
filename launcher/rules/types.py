from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from launcher.dynamo import DynamoClient
    from launcher.slack_client import SlackClient

from gnomepy.registry import RegistryClient

from launcher.models import LaunchRule


STRATEGY_PARAMS_SCHEMA: dict = {
    "strategy_id": {"type": "integer"},
    "strategy_type": {"type": "string", "enum": ["java", "python"]},
    "strategy_class": {"type": "string"},
    "mode": {"type": "string", "enum": ["paper", "live"]},
    "research_commit": {"type": "string"},
    "strategy_args": {
        "type": "object",
        "additionalProperties": {"type": "string"},
    },
    "simulation_config": {
        "type": "object",
        "additionalProperties": {"type": "string"},
    },
}

STRATEGY_REQUIRED_PARAMS: list[str] = ["strategy_id", "strategy_type", "strategy_class", "mode"]


@dataclass
class ResolvedStrategyConfig:
    strategy_id: int
    strategy_type: str
    strategy_class: str
    mode: str
    listings: str
    research_commit: str | None = None
    region: str | None = None
    strategy_args: dict[str, str] = field(default_factory=dict)
    simulation_config: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: dict, *, listings: str) -> ResolvedStrategyConfig:
        return cls(
            strategy_id=params["strategy_id"],
            strategy_type=params["strategy_type"],
            strategy_class=params["strategy_class"],
            mode=params["mode"],
            listings=listings,
            research_commit=params.get("research_commit"),
            strategy_args=params.get("strategy_args", {}),
            simulation_config=params.get("simulation_config", {}),
        )


@dataclass
class RuleEvaluation:
    should_launch: bool
    resolved_config: ResolvedStrategyConfig
    reason: str


@dataclass
class RuleMatch:
    rule: LaunchRule
    evaluation: RuleEvaluation


@dataclass
class RuleContext:
    registry: RegistryClient
    dynamo: "DynamoClient"
    slack: "SlackClient"


class RuleType:
    type: str
    display_name: str
    parameter_schema: dict
    data_schema: dict

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | None:
        raise NotImplementedError


RULE_TYPE_REGISTRY: dict[str, type[RuleType]] = {}


def register_rule_type(cls: type[RuleType]) -> type[RuleType]:
    RULE_TYPE_REGISTRY[cls.type] = cls
    return cls
