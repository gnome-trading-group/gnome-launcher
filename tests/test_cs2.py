import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from launcher.handlers.custom.cs2_scanner import latest, triggers
from launcher.lifecycle import dedup_key
from launcher.rules.cs2 import CS2Rule
from launcher.rules.types import NoMatch

NOW = pd.Timestamp("2026-10-16T00:00:00Z")
SIMULATION = {"exchanges": {"KALSHI": {"queue_model": {"type": "optimistic"}}}}
LEGS = [{"venue": "kalshi", "listing_team_a": 11, "listing_team_b": 12},
        {"venue": "polymarket", "listing_team_a": 21, "listing_team_b": 22},
        {"venue": "polymarket_us", "listing_team_a": 31, "listing_team_b": 32}]
REGISTERED = (11, 12, 21, 22, 31, 32)


def _data(market="series", legs=LEGS):
    return {"match_id": 2399262, "market": market, "legs": list(legs)}


def _params(**overrides):
    return {"strategy_id": 7, "strategy_type": "python", "mode": "paper",
            "strategy_class": "gnomepy_research.sessions.cs2_series.strategy:CS2Series",
            "research_commit": "933d27f", **overrides}


# Listings 2x are Polymarket International's (exchange 4, eu-west-1); the rest are in us-east-1.
REGIONS = {4: "eu-west-1", 5: "us-east-1", 6: "us-east-1"}


def _ctx(registered=REGISTERED, sessions=()):
    ctx = MagicMock()
    ctx.registry.get_listing.side_effect = lambda listing_id: [
        SimpleNamespace(listing_id=listing_id, exchange_id={1: 5, 2: 4, 3: 6}[listing_id // 10])] \
        if listing_id in registered else []
    ctx.registry.get_exchange.side_effect = lambda exchange_id: [SimpleNamespace(region=REGIONS[exchange_id])]
    ctx.registry.get_strategy_sessions.return_value = list(sessions)
    return ctx


def test_launches_one_session_for_the_match_holding_every_venues_listings():
    evaluation = CS2Rule().evaluate(_data(), _params(), _ctx())

    config = evaluation.resolved_config
    assert config.listings == [11, 12, 21, 22, 31, 32] and config.research_commit == "933d27f"
    assert config.strategy_args == {"match_id": 2399262, "legs": LEGS}
    # Nothing to set: each listing simulates as its exchange is recorded in the registry.
    assert config.simulation == {}
    assert evaluation.reason == "CS2 match 2399262 series on kalshi, polymarket, polymarket_us"


def test_the_session_runs_in_polymarket_internationals_region_when_it_trades_there():
    assert CS2Rule().evaluate(_data(), _params(), _ctx()).resolved_config.region == "eu-west-1"
    us_only = CS2Rule().evaluate(_data(legs=[LEGS[0], LEGS[2]]), _params(), _ctx()).resolved_config
    assert us_only.region is None, "the registry places a session whose listings share a region"


def test_the_session_carries_the_rules_simulation_overrides():
    assert CS2Rule().evaluate(_data(), _params(simulation=SIMULATION), _ctx()).resolved_config.simulation == SIMULATION


def test_only_the_rules_venues_with_registered_listings_are_traded():
    evaluation = CS2Rule().evaluate(_data(), _params(venues=["kalshi", "polymarket"]), _ctx(registered=(11, 12, 21)))

    assert evaluation.resolved_config.strategy_args["legs"] == LEGS[:1]
    assert evaluation.resolved_config.listings == [11, 12]
    assert "skipped polymarket (listings [22] aren't in the registry)" in evaluation.reason


def test_skips_other_markets_and_matches_with_no_tradable_venue():
    rule = CS2Rule()
    assert "series market" in rule.evaluate(_data(market="game1"), _params(), _ctx()).reason
    assert isinstance(rule.evaluate(_data(), _params(venues=["kalshi"]), _ctx(registered=(11,))), NoMatch)
    assert isinstance(rule.evaluate(_data(legs=LEGS[2:]), _params(venues=["kalshi"]), _ctx()), NoMatch)


def test_shuts_down_the_sessions_holding_any_of_the_matchs_listings():
    sessions = [SimpleNamespace(session_id="a", config={"listings": "11,12,21,22"}),
                SimpleNamespace(session_id="b", config={"listings": [32, 40]}),
                SimpleNamespace(session_id="c", config={"listings": [1, 2]})]

    evaluation = CS2Rule().evaluate_shutdown(_data(), _params(), _ctx(sessions=sessions))

    assert evaluation.target_session_ids == ["a", "b"]
    assert evaluation.reason == "CS2 match 2399262 has started or gone"
    assert isinstance(CS2Rule().evaluate_shutdown(_data(market="game1"), _params(), _ctx(sessions=sessions)), NoMatch)


def _row(match_id, kickoff, status="upcoming", legs=(("kalshi", "series"),), generated_at=NOW):
    markets = [{"venue": v, "market": m, "listing_team_a": 11, "listing_team_b": 12} for v, m in legs]
    return {"match_id": match_id, "kickoff": pd.Timestamp(kickoff), "status": status,
            "markets": json.dumps(markets), "generated_at": pd.Timestamp(generated_at)}


def test_scanner_launches_fresh_upcoming_matches_inside_the_lead_and_skips_the_rest():
    rows = pd.DataFrame([
        _row(1, "2026-10-16T01:30Z", legs=(("polymarket", "series"), ("kalshi", "series"), ("kalshi", "game1"))),
        _row(2, "2026-10-16T02:01Z"),
        _row(3, "2026-10-16T01:30Z", legs=()),
        _row(4, "2026-10-16T00:05Z"),
        _row(5, "2026-10-16T01:30Z", generated_at="2026-10-15T22:30Z"),
    ])

    found = triggers(rows, NOW)

    assert [(a, d["match_id"], d["market"]) for a, d, _ in found] == [("launch", 1, "series"), ("launch", 1, "game1")]
    assert found[0][1] == {"match_id": 1, "market": "series", "legs": [
        {"venue": "kalshi", "listing_team_a": 11, "listing_team_b": 12},
        {"venue": "polymarket", "listing_team_a": 11, "listing_team_b": 12}]}, "one trigger, legs sorted by venue"
    assert found[0][2] == "2026-10-16T01:20:00+00:00", "a request still awaiting approval at the cutoff can't launch"


def test_scanner_sends_the_same_trigger_whatever_order_the_venues_were_written_in():
    a = triggers(pd.DataFrame([_row(1, "2026-10-16T01:30Z", legs=(("polymarket", "series"), ("kalshi", "series")))]), NOW)
    b = triggers(pd.DataFrame([_row(1, "2026-10-16T01:30Z", legs=(("kalshi", "series"), ("polymarket", "series")))]), NOW)
    assert dedup_key("cs2", "launch", a[0][1]) == dedup_key("cs2", "launch", b[0][1])


def test_scanner_launches_nothing_inside_the_launch_cutoff():
    rows = pd.DataFrame([_row(1, "2026-10-16T00:11Z"), _row(2, "2026-10-16T00:10Z"), _row(3, "2026-10-15T23:59Z")])

    assert [(a, d["match_id"]) for a, d, _ in triggers(rows, NOW)] == [("launch", 1)]


def test_scanner_shuts_down_missing_and_long_started_matches_but_leaves_live_ones():
    rows = pd.DataFrame([
        _row(1, "2026-10-15T20:00Z", status="missing"),
        _row(2, "2026-10-15T23:00Z", generated_at="2026-10-15T22:00Z"),
        _row(3, "2026-10-15T23:30Z", status="live"),
        _row(4, "2026-10-15T23:30Z"),
    ])

    assert [(d["match_id"], d["reason"], deadline) for a, d, deadline in triggers(rows, NOW) if a == "shutdown"] == [
        (1, "gone", None), (2, "past_kickoff", None)]


def test_scanner_stops_a_finished_match_even_while_hltv_still_lists_it():
    rows = pd.DataFrame([_row(1, "2026-10-15T22:00Z", status="finished"),
                         _row(2, "2026-10-15T23:45Z", status="finished")])

    assert [(a, d["match_id"], d["reason"]) for a, d, _ in triggers(rows, NOW)] == [
        ("shutdown", 1, "finished"), ("shutdown", 2, "finished")]


def test_launch_triggers_carry_no_reason():
    assert "reason" not in triggers(pd.DataFrame([_row(1, "2026-10-16T01:30Z")]), NOW)[0][1]


def test_scanner_reads_each_matchs_newest_row():
    rows = pd.DataFrame([
        _row(1, "2026-10-16T01:00Z", status="live", generated_at="2026-10-15T23:45Z"),
        _row(1, "2026-10-16T01:00Z", generated_at="2026-10-15T23:30Z"),
    ])

    assert latest(rows).status.tolist() == ["live"]


def test_a_rule_trading_the_match_in_play_ignores_the_hour_after_kickoff_but_not_the_end():
    sessions = [SimpleNamespace(session_id="a", config={"listings": [11, 12]})]
    rule, params, ctx = CS2Rule(), _params(stop_past_kickoff=False), _ctx(sessions=sessions)

    assert isinstance(rule.evaluate_shutdown({**_data(), "reason": "past_kickoff"}, params, ctx), NoMatch)
    for reason in ("finished", "gone"):
        assert rule.evaluate_shutdown({**_data(), "reason": reason}, params, ctx).target_session_ids == ["a"]


def test_a_default_rule_still_stops_an_hour_after_kickoff():
    sessions = [SimpleNamespace(session_id="a", config={"listings": [11, 12]})]
    evaluation = CS2Rule().evaluate_shutdown({**_data(), "reason": "past_kickoff"}, _params(), _ctx(sessions=sessions))

    assert evaluation.target_session_ids == ["a"]
    assert evaluation.reason == "CS2 match 2399262 started over an hour ago"


def test_scanner_launches_live_matches_for_in_play_rules_and_still_flags_kickoff_passed():
    rows = pd.DataFrame([
        _row(1, "2026-10-15T23:30Z", status="live"),
        _row(2, "2026-10-15T22:30Z", status="live"),
        _row(3, "2026-10-15T22:30Z", status="live", generated_at="2026-10-15T22:30Z"),
    ])

    assert [(a, d["match_id"], d.get("phase"), d.get("reason"), deadline) for a, d, deadline in triggers(rows, NOW)] == [
        ("launch", 1, "live", None, None),
        ("launch", 2, "live", None, None), ("shutdown", 2, None, "past_kickoff", None),
        ("shutdown", 3, None, "past_kickoff", None)]


def test_a_live_trigger_is_not_the_pre_kickoff_trigger():
    pre = triggers(pd.DataFrame([_row(1, "2026-10-16T01:30Z")]), NOW)[0][1]
    live = triggers(pd.DataFrame([_row(1, "2026-10-15T23:50Z", status="live")]), NOW)[0][1]
    assert dedup_key("cs2", "launch", pre) != dedup_key("cs2", "launch", live)


def test_only_rules_that_launch_live_act_on_a_match_being_played():
    live = {**_data(), "phase": "live"}
    assert isinstance(CS2Rule().evaluate(live, _params(), _ctx()), NoMatch)
    evaluation = CS2Rule().evaluate(live, _params(launch_live=True, stop_past_kickoff=False), _ctx())
    assert evaluation.should_launch and evaluation.resolved_config.strategy_args["match_id"] == 2399262


def test_launch_live_needs_sessions_that_outlive_kickoff():
    assert CS2Rule().validate_parameters(_params(launch_live=True))
    assert CS2Rule().validate_parameters(_params(launch_live=True, stop_past_kickoff=False)) == []


def test_a_rule_can_wait_for_the_match_to_start():
    params = _params(launch_before_kickoff=False, launch_live=True, stop_past_kickoff=False)
    assert isinstance(CS2Rule().evaluate(_data(), params, _ctx()), NoMatch)
    assert CS2Rule().evaluate({**_data(), "phase": "live"}, params, _ctx()).should_launch
    assert CS2Rule().validate_parameters(params) == []
    assert CS2Rule().validate_parameters(_params(launch_before_kickoff=False)), "it would never launch"
