from unittest.mock import MagicMock

from launcher.rules.types import ResolvedStrategyConfig
from launcher.sessions import ACTIVE_SESSION_STATUSES, build_session_config, create_session


def _config(**overrides) -> ResolvedStrategyConfig:
    return ResolvedStrategyConfig(
        strategy_id=7,
        strategy_type="java",
        strategy_class="com.example.Strategy",
        mode="paper",
        listings=[1, 2],
        **overrides,
    )


def test_active_statuses_include_the_boot_window():
    assert ACTIVE_SESSION_STATUSES.split(",") == ["SUBMITTED", "STARTING", "RUNNING"]


def test_latency_profile_travels_in_the_session_config():
    assert build_session_config(_config(latency_profile="standard"))["latency.profile"] == "standard"
    assert "latency.profile" not in build_session_config(_config())


def test_create_session_leaves_out_unset_fields():
    registry = MagicMock()

    create_session(registry, "s1", _config())

    kwargs = registry.create_strategy_session.call_args.kwargs
    assert kwargs.keys() == {"session_id", "strategy_id", "mode", "config"}


def test_create_session_passes_set_fields():
    registry = MagicMock()

    create_session(
        registry,
        "s1",
        _config(
            research_commit="abc",
            instance_type="c7i.8xlarge",
            orchestrator_version="1.12.2",
            gnomepy_version="2.20.2",
            availability_zone="us-east-1b",
        ),
    )

    kwargs = registry.create_strategy_session.call_args.kwargs
    assert kwargs["instance_type"] == "c7i.8xlarge"
    assert kwargs["orchestrator_version"] == "1.12.2"
    assert kwargs["gnomepy_version"] == "2.20.2"
    assert kwargs["availability_zone"] == "us-east-1b"
    assert kwargs["research_commit"] == "abc"


def test_from_params_reads_launch_options():
    config = ResolvedStrategyConfig.from_params(
        {
            "strategy_id": 7,
            "strategy_type": "python",
            "strategy_class": "m:C",
            "mode": "paper",
            "instance_type": "c7i.xlarge",
            "latency_profile": "standard",
        },
        listings=[1],
    )
    assert config.instance_type == "c7i.xlarge"
    assert config.latency_profile == "standard"
