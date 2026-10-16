from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

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
    "instance_type": {"type": "string"},
    "latency_profile": {"type": "string", "enum": ["low_latency", "standard"]},
    "orchestrator_version": {"type": "string"},
    "gnomepy_version": {"type": "string"},
    "availability_zone": {"type": "string"},
    "strategy_args": {
        "type": "object",
        "additionalProperties": True,
    },
    "simulation_config": {
        "type": "object",
        "additionalProperties": True,
    },
}

STRATEGY_REQUIRED_PARAMS: list[str] = ["strategy_id", "strategy_type", "strategy_class", "mode"]

# A session's config travels to the orchestrator as environment variables: simulation.profiles.<name>.… becomes
# SIMULATION_PROFILES_<NAME>_…, which bash refuses if the name has a '-', and gnome-core turns back into a property
# by lowercasing it and reading every '_' as '.'. Only lowercase letters and digits survive the round trip.
PROFILE_NAME = re.compile(r"[a-z0-9]+")


def bad_profile_names(names) -> list[str]:
    return sorted({n for n in names if not PROFILE_NAME.fullmatch(n)})


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
    simulation_config: dict[str, Any] = field(default_factory=dict)
    instance_type: str | None = None
    latency_profile: str | None = None
    orchestrator_version: str | None = None
    gnomepy_version: str | None = None
    availability_zone: str | None = None

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
            simulation_config=params.get("simulation_config", {}),
            instance_type=params.get("instance_type"),
            latency_profile=params.get("latency_profile"),
            orchestrator_version=params.get("orchestrator_version"),
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
        names = {k.split(".")[2] for k in params.get("simulation_config", {})
                 if k.startswith("simulation.profiles.") and k.count(".") >= 3}
        bad = bad_profile_names(names)
        return [f"simulation profile names must be lowercase letters and digits only: {', '.join(bad)}"] if bad else []


RULE_TYPE_REGISTRY: dict[str, type[RuleType]] = {}


def register_rule_type(cls: type[RuleType]) -> type[RuleType]:
    RULE_TYPE_REGISTRY[cls.type] = cls
    return cls
