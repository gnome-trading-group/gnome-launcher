import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from launcher.handlers.custom.cs2_scanner import latest, triggers
from launcher.rules.cs2 import CS2Rule

NOW = pd.Timestamp("2026-10-16T00:00:00Z")
PROFILES = {
    "simulation.profiles.kalshi.fee.model": "parametric",
    "simulation.profiles.polymarketintl.fee.model": "parametric",
}


def _data(venue="kalshi", market="series"):
    return {"match_id": 2399262, "venue": venue, "market": market, "listing_team_a": 11, "listing_team_b": 12}


def _params(**overrides):
    return {"strategy_id": 7, "strategy_type": "python", "mode": "paper",
            "strategy_class": "gnomepy_research.sessions.cs2_series.strategy:CS2Series",
            "research_commit": "933d27f", "simulation_config": dict(PROFILES), **overrides}


def _ctx(registered=(11, 12), sessions=()):
    ctx = MagicMock()
    ctx.registry.get_listing.side_effect = lambda listing_id: [SimpleNamespace(listing_id=listing_id)] \
        if listing_id in registered else []
    ctx.registry.get_strategy_sessions.return_value = list(sessions)
    return ctx


def test_launches_one_session_with_the_venues_profile():
    evaluation = CS2Rule().evaluate(_data(), _params(), _ctx())

    config = evaluation.resolved_config
    assert config.listings == [11, 12] and config.research_commit == "933d27f"
    assert config.strategy_args == {"match_id": 2399262, "listing_team_a": 11, "listing_team_b": 12}
    assert config.simulation_config["simulation.listing.11.profile"] == "kalshi"
    assert config.simulation_config["simulation.listing.12.profile"] == "kalshi"


def test_rule_overrides_profiles():
    params = _params(simulation_profiles={"kalshi": "polymarketintl"})

    config = CS2Rule().evaluate(_data(), params, _ctx()).resolved_config

    assert config.simulation_config["simulation.listing.11.profile"] == "polymarketintl"


def test_skips_other_markets_venues_and_unregistered_listings():
    rule = CS2Rule()
    assert rule.evaluate(_data(market="game1"), _params(), _ctx()) is None
    assert rule.evaluate(_data(venue="polymarket_us"), _params(venues=["kalshi"]), _ctx()) is None
    assert rule.evaluate(_data(), _params(), _ctx(registered=(11,))) is None


def test_refuses_paper_without_the_venues_profile_defined():
    assert CS2Rule().evaluate(_data(venue="polymarket_us"), _params(), _ctx()) is None
    live = CS2Rule().evaluate(_data(venue="polymarket_us"), _params(mode="live", simulation_config={}), _ctx())
    assert live.resolved_config.simulation_config == {}


def test_refuses_profile_names_that_wont_survive_the_trip_to_the_orchestrator():
    for name in ("polymarket_intl", "polymarket-intl", "Kalshi"):
        params = _params(simulation_profiles={"kalshi": name},
                         simulation_config={**PROFILES, f"simulation.profiles.{name}.fee.model": "parametric"})
        assert CS2Rule().evaluate(_data(), params, _ctx()) is None, name


def test_shuts_down_the_sessions_holding_the_matchs_listings():
    sessions = [SimpleNamespace(session_id="a", config={"listings": "11,12"}),
                SimpleNamespace(session_id="b", config={"listings": [12, 13]}),
                SimpleNamespace(session_id="c", config={"listings": [1, 2]})]

    evaluation = CS2Rule().evaluate_shutdown(_data(), _params(), _ctx(sessions=sessions))

    assert evaluation.target_session_ids == ["a", "b"]
    assert CS2Rule().evaluate_shutdown(_data(market="game1"), _params(), _ctx(sessions=sessions)) is None


def _row(match_id, kickoff, status="upcoming", legs=(("kalshi", "series"),), generated_at=NOW):
    markets = [{"venue": v, "market": m, "listing_team_a": 11, "listing_team_b": 12} for v, m in legs]
    return {"match_id": match_id, "kickoff": pd.Timestamp(kickoff), "status": status,
            "markets": json.dumps(markets), "generated_at": pd.Timestamp(generated_at)}


def test_scanner_launches_fresh_upcoming_legs_inside_the_lead_and_skips_the_rest():
    rows = pd.DataFrame([
        _row(1, "2026-10-16T08:00Z", legs=(("kalshi", "series"), ("polymarket", "series"))),
        _row(2, "2026-10-16T14:00Z"),
        _row(3, "2026-10-16T08:00Z", legs=()),
        _row(4, "2026-10-15T23:50Z"),
        _row(5, "2026-10-16T08:00Z", generated_at="2026-10-15T22:30Z"),
    ])

    found = triggers(rows, NOW)

    assert [(a, d["match_id"], d["venue"]) for a, d in found] == [("launch", 1, "kalshi"), ("launch", 1, "polymarket")]
    assert found[0][1] == {"match_id": 1, "venue": "kalshi", "market": "series", "listing_team_a": 11, "listing_team_b": 12}


def test_scanner_shuts_down_live_missing_and_long_started_matches():
    rows = pd.DataFrame([
        _row(1, "2026-10-16T01:00Z", status="live"),
        _row(2, "2026-10-15T20:00Z", status="missing"),
        _row(3, "2026-10-15T23:00Z", generated_at="2026-10-15T12:00Z"),
    ])

    assert [(a, d["match_id"]) for a, d in triggers(rows, NOW)] == [("shutdown", 1), ("shutdown", 2), ("shutdown", 3)]


def test_scanner_reads_each_matchs_newest_row():
    rows = pd.DataFrame([
        _row(1, "2026-10-16T08:00Z", status="live", generated_at="2026-10-15T23:45Z"),
        _row(1, "2026-10-16T08:00Z", generated_at="2026-10-15T23:30Z"),
    ])

    assert latest(rows).status.tolist() == ["live"]
