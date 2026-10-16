from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from launcher.dynamo import DynamoClient
    from launcher.slack_client import SlackClient

from gnomepy.registry import RegistryClient

from launcher.models import LaunchRule


_MODEL_BLOCK = {"type": "object", "required": ["type"], "properties": {"type": {"type": "string"}}}
# Blocks of how a venue behaves in simulation, in place of its exchange's registry parameters: the shape backtest
# configs take. The registry checks each block's fields when it launches the session.
SIMULATION_OVERRIDES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "fee_model": _MODEL_BLOCK,
        "network_latency": _MODEL_BLOCK,
        "order_processing_latency": _MODEL_BLOCK,
        "market_data_latency": _MODEL_BLOCK,
        "queue_model": _MODEL_BLOCK,
        "self_trade_prevention": {"enum": ["CANCEL_INCOMING", "CANCEL_RESTING"]},
    },
    "additionalProperties": False,
}

STRATEGY_PARAMS_SCHEMA: dict = {
    "strategy_id": {"type": "integer"},
    "strategy_type": {"type": "string", "enum": ["java", "python"]},
    "strategy_class": {"type": "string"},
    "mode": {"type": "string", "enum": ["paper", "live"]},
    "research_commit": {"type": "string"},
    "instance_type": {"type": "string"},
    "latency_profile": {"type": "string", "enum": ["low_latency", "standard"]},
    "orchestrator_version": {"type": "string"},
    "gnomepy_version": {"type": "string"},
    "availability_zone": {"type": "string"},
    "strategy_args": {
        "type": "object",
        "additionalProperties": True,
    },
    # Paper sessions, by exchange code or listing id. The registry pins the session's as_of when it launches it.
    "simulation": {
        "type": "object",
        "properties": {
            "seed": {"type": "integer"},
            "exchanges": {"type": "object", "additionalProperties": SIMULATION_OVERRIDES_SCHEMA},
            "listings": {
                "type": "object",
                "propertyNames": {"pattern": "^[0-9]+$"},
                "additionalProperties": SIMULATION_OVERRIDES_SCHEMA,
            },
        },
        "additionalProperties": False,
    },
    # Orchestrator properties set for the session (overrides.<property> in its config), as a deploy sets them.
    "overrides": {
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
    listings: list[int]
    research_commit: str | None = None
    region: str | None = None
    strategy_args: dict[str, Any] = field(default_factory=dict)
    simulation: dict[str, Any] = field(default_factory=dict)
    instance_type: str | None = None
    latency_profile: str | None = None
    orchestrator_version: str | None = None
    gnomepy_version: str | None = None
    availability_zone: str | None = None
    overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: dict, *, listings: list[int]) -> ResolvedStrategyConfig:
        return cls(
            strategy_id=params["strategy_id"],
            strategy_type=params["strategy_type"],
            strategy_class=params["strategy_class"],
            mode=params["mode"],
            listings=listings,
            research_commit=params.get("research_commit"),
            strategy_args=params.get("strategy_args", {}),
            simulation=params.get("simulation", {}),
            instance_type=params.get("instance_type"),
            latency_profile=params.get("latency_profile"),
            orchestrator_version=params.get("orchestrator_version"),
            overrides=params.get("overrides", {}),
            gnomepy_version=params.get("gnomepy_version"),
            availability_zone=params.get("availability_zone"),
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
class NoMatch:
    """Why a rule didn't act on a trigger, recorded in the trigger log so "why didn't this launch?" has an answer."""
    reason: str


@dataclass
class ShutdownEvaluation:
    should_shutdown: bool
    target_session_ids: list[str]
    reason: str


@dataclass
class ScheduleResult:
    scheduled_time: datetime
    reason: str


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

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
        raise NotImplementedError

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | NoMatch:
        return NoMatch("this rule type never shuts sessions down")

    def compute_schedule_time(self, data: dict, params: dict, ctx: RuleContext) -> ScheduleResult | None:
        return None

    def validate_parameters(self, params: dict) -> list[str]:
        """Checks beyond parameter_schema; each problem as one message."""
        return []


RULE_TYPE_REGISTRY: dict[str, type[RuleType]] = {}


def register_rule_type(cls: type[RuleType]) -> type[RuleType]:
    RULE_TYPE_REGISTRY[cls.type] = cls
    return cls
