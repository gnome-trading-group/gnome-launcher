from __future__ import annotations

from typing import Any

from gnomepy.registry import RegistryClient

from launcher.rules.types import ResolvedStrategyConfig

# A session holds its strategy's slot from the moment it is requested, not only once it reports RUNNING:
# counting RUNNING alone lets a second launch through while the first instance is still booting.
ACTIVE_SESSION_STATUSES = "SUBMITTED,STARTING,RUNNING"


def build_session_config(resolved_config: ResolvedStrategyConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "strategy.id": str(resolved_config.strategy_id),
        "mode": resolved_config.mode,
        "listings": resolved_config.listings,
        "strategy.type": resolved_config.strategy_type,
        "strategy.class": resolved_config.strategy_class,
    }
    if resolved_config.latency_profile:
        result["latency.profile"] = resolved_config.latency_profile
    for k, v in resolved_config.strategy_args.items():
        result[f"strategy.args.{k}"] = v
    for k, v in resolved_config.simulation_config.items():
        result[k] = v
    return result


def create_session(registry: RegistryClient, session_id: str, resolved_config: ResolvedStrategyConfig) -> dict:
    # Unset fields are left out rather than passed as None, so a gnomepy that predates them still works.
    optional = {
        "research_commit": resolved_config.research_commit,
        "region": resolved_config.region,
        "instance_type": resolved_config.instance_type,
        "orchestrator_version": resolved_config.orchestrator_version,
        "gnomepy_version": resolved_config.gnomepy_version,
        "availability_zone": resolved_config.availability_zone,
    }
    return registry.create_strategy_session(
        session_id=session_id,
        strategy_id=resolved_config.strategy_id,
        mode=resolved_config.mode,
        config=build_session_config(resolved_config),
        **{key: value for key, value in optional.items() if value is not None},
    )
