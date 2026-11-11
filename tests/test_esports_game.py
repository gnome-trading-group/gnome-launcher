from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from gnomepy.registry.relations import Edge

from launcher.handlers.custom.game_scanner import (
    RUN_FOR,
    Event,
    Leg,
    Winner,
    match_legs,
    scan,
    triggers,
    winners,
)
from launcher.rules.esports_game import EsportsGameRule
from launcher.rules.types import NoMatch

NOW = datetime(2026, 11, 12, 15, 50, tzinfo=timezone.utc)
START = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)

# DO KONCA vs PARIVISION as prod holds it: Polymarket International (event 1), Polymarket US (2), Kalshi (3).
EVENTS = {
    1: Event(1, "polymarket", "dota2", START, False, "0x0452"),
    2: Event(2, "polymarket_us", "dota2", START, False, "aec-dota2-pari-konca-2026-11-12"),
    3: Event(3, "kalshi", "dota2", START, False, "KXDOTA2GAME-26NOV121100KONCAPARI"),
}
WINNERS = [Winner(11, 1, 7, "parivision"), Winner(12, 1, 7, "do-konca"),
           Winner(21, 2, 7, "pari"), Winner(22, 2, 7, "konca"),
           Winner(32, 3, 7, "konca"), Winner(31, 3, 7, "pari")]
# The rows prod stores for it, all flagged: the classifier stores only what nothing else rebuilds, so Polymarket US's
# DO KONCA is equivalent to Polymarket International's only through the complements of PARIVISION's row.
VOID = ("VOID_RULES_DIFFER",)
STORED = [(21, 31), (22, 32), (11, 21), (11, 31), (12, 32)]
# Each two-security event's other security: Polymarket's markets are binary, Kalshi's esports events aren't.
COMPLEMENTS = {11: 12, 12: 11, 21: 22, 22: 21}
LISTINGS = {11: 111, 12: 112, 21: 121, 22: 122, 31: 131, 32: 132}


def edges(rows, flags=VOID):
    return [Edge(a, b, "EQUIVALENT", flags) for a, b in rows]


def legs_of(winners_, rows, flags=VOID, events=EVENTS):
    return match_legs(winners_, events, edges(rows, flags), COMPLEMENTS)


def test_legs_are_each_venues_pair_in_the_anchors_team_order():
    legs, notes = legs_of(WINNERS, STORED)
    assert [(leg.venue, leg.team_a, leg.team_b) for leg in legs] == [
        ("polymarket", 11, 12), ("kalshi", 31, 32), ("polymarket_us", 21, 22)]
    assert notes == []


def test_one_stored_row_joins_two_polymarket_pairs_through_their_complements():
    legs, _ = legs_of([w for w in WINNERS if w.event_id != 3], [(11, 21)])
    assert [(leg.venue, leg.team_a, leg.team_b) for leg in legs] == [("polymarket", 11, 12), ("polymarket_us", 21, 22)]


def test_two_flagged_rows_never_chain_into_a_leg():
    # Kalshi is related only to Polymarket US, which is related to Polymarket International: two flagged hops.
    rows = [(31, 21), (32, 22), (11, 21)]
    assert [leg.venue for leg in legs_of(WINNERS, rows)[0]] == ["polymarket", "polymarket_us"]
    assert [leg.venue for leg in legs_of(WINNERS, rows, flags=())[0]] == ["polymarket", "kalshi", "polymarket_us"]


def test_a_venue_whose_opponent_is_another_team_is_left_out():
    # Kalshi lists PARIVISION against someone else: one team matches, the other doesn't.
    legs, notes = legs_of(WINNERS, [(11, 31), (11, 21)])
    assert [leg.venue for leg in legs] == ["polymarket", "polymarket_us"]
    assert notes == ["kalshi: its teams aren't related to polymarket's team for team"]


def test_kalshi_anchors_a_match_polymarket_international_doesnt_list():
    legs, _ = legs_of([w for w in WINNERS if w.event_id != 1], STORED)
    assert [(leg.venue, leg.team_a, leg.team_b) for leg in legs] == [("kalshi", 31, 32), ("polymarket_us", 21, 22)]


def test_a_match_only_polymarket_us_lists_has_no_live_signal():
    legs, notes = legs_of([w for w in WINNERS if w.event_id == 2], STORED)
    assert legs == [] and "no live signal" in notes[0]


def test_a_venue_with_two_match_events_on_the_subject_is_ambiguous():
    events = {**EVENTS, 4: Event(4, "kalshi", "dota2", START + timedelta(hours=4), False, "KXDOTA2GAME-X")}
    legs, notes = legs_of(WINNERS + [Winner(41, 4, 7, "pari"), Winner(42, 4, 7, "konca")],
                          STORED + [(41, 11), (42, 12)], events=events)
    assert [leg.venue for leg in legs] == ["polymarket", "polymarket_us"]
    assert notes == ["kalshi: 2 match-winner events on the subject"]


def test_an_anchor_whose_teams_read_alike_launches_nothing():
    legs, notes = legs_of(WINNERS, STORED + [(11, 12)], flags=())
    assert legs == [] and "subject is wrong" in notes[0]


def test_winners_are_match_scope_yes_sides_with_a_known_team():
    props = {1: (7, {"kind": "WINNER", "scope": "match", "entity": "pari"}),
             2: (7, {"kind": "WINNER", "scope": "match", "entity": "pari", "negated": True}),
             3: (7, {"kind": "WINNER", "scope": "map:1", "entity": "pari"}),
             4: (7, {"kind": "WINNER", "scope": "match", "entity": "x", "unknown": ["entity"]}),
             5: (7, {"kind": "THRESHOLD", "scope": "match", "measure": "total_maps"})}
    assert [w.security_id for w in winners(props, {1: [1, 2, 3, 4, 5]})] == [1]


def _legs(resolved=False):
    events = {k: Event(e.event_id, e.venue, e.league, e.start, resolved and k == 3, e.native_event_id)
              for k, e in EVENTS.items()}
    legs, _ = match_legs(WINNERS, events, edges(STORED), COMPLEMENTS)
    return legs


def test_launches_from_the_lookahead_with_ids_only():
    ((action, data, deadline),) = triggers(_legs(), _legs(), LISTINGS, 7, "dota2-pari-konca-2026-11-12", NOW)
    assert action == "launch" and deadline == (START + RUN_FOR).isoformat()
    assert data == {
        "league": "dota2", "subject_id": 7, "market": "match", "anchor_event_id": 1, "event_ids": [1, 2, 3],
        "legs": [{"venue": "kalshi", "listing_team_a": 131, "listing_team_b": 132},
                 {"venue": "polymarket", "listing_team_a": 111, "listing_team_b": 112},
                 {"venue": "polymarket_us", "listing_team_a": 121, "listing_team_b": 122}],
        "polymarket_slug": "dota2-pari-konca-2026-11-12", "kalshi_event_ticker": "KXDOTA2GAME-26NOV121100KONCAPARI",
    }
    assert triggers(_legs(), _legs(), LISTINGS, 7, None, START - timedelta(hours=1)) == []


def test_stops_once_a_venue_resolves_it_or_it_started_too_long_ago():
    ((action, data, _),) = triggers([], _legs(resolved=True), LISTINGS, 7, None, NOW)
    assert action == "shutdown" and data["reason"] == "resolved" and len(data["legs"]) == 3
    ((action, data, _),) = triggers(_legs(), _legs(), LISTINGS, 7, None, START + RUN_FOR)
    assert action == "shutdown" and data["reason"] == "past_start"


def test_scan_reads_the_registry_into_triggers():
    registry = MagicMock()
    registry.get_exchange.return_value = [SimpleNamespace(exchange_id=4, exchange_code="POLYMARKET_INTL"),
                                          SimpleNamespace(exchange_id=5, exchange_code="KALSHI"),
                                          SimpleNamespace(exchange_id=6, exchange_code="POLYMARKET_US")]
    exchange = {1: 4, 2: 6, 3: 5}
    registry.get_event.side_effect = lambda tags, **_: [
        SimpleNamespace(event_id=e.event_id, exchange_id=exchange[e.event_id], start_time=START.isoformat(),
                        resolved=False, native_event_id=e.native_event_id) for e in EVENTS.values()] \
        if tags == ["dota 2"] else []
    registry.get_event_contracts.side_effect = lambda event_id: [
        SimpleNamespace(security_id=w.security_id) for w in WINNERS if w.event_id == event_id]
    registry.get_contract_propositions.side_effect = lambda security_ids: [
        SimpleNamespace(security_id=w.security_id, subject_id=7,
                        fields={"kind": "WINNER", "scope": "match", "entity": w.entity})
        for w in WINNERS if w.security_id in security_ids]
    registry.get_connected_relationships.return_value = [
        SimpleNamespace(security_id_a=a, security_id_b=b, relationship_type="EQUIVALENT", flags=list(VOID),
                        complement_a=COMPLEMENTS.get(a), complement_b=COMPLEMENTS.get(b)) for a, b in STORED]
    registry.get_listing.side_effect = lambda security_id: [SimpleNamespace(listing_id=LISTINGS[security_id], active=True)]
    registry.get_contract_sources.return_value = [SimpleNamespace(source={"event": {"slug": "dota2-pari-konca"}})]

    ((action, data, _),) = scan(registry, NOW)
    assert action == "launch" and data["polymarket_slug"] == "dota2-pari-konca"
    assert [leg["venue"] for leg in data["legs"]] == ["kalshi", "polymarket", "polymarket_us"]


def _registry():
    registry = MagicMock()
    registry.get_exchange.return_value = [SimpleNamespace(exchange_id=4, exchange_code="POLYMARKET_INTL"),
                                          SimpleNamespace(exchange_id=5, exchange_code="KALSHI"),
                                          SimpleNamespace(exchange_id=6, exchange_code="POLYMARKET_US")]
    exchange = {1: 4, 2: 6, 3: 5}
    registry.get_event.side_effect = lambda tags, **_: [
        SimpleNamespace(event_id=e.event_id, exchange_id=exchange[e.event_id], start_time=START.isoformat(),
                        resolved=False, native_event_id=e.native_event_id) for e in EVENTS.values()] \
        if tags == ["dota 2"] else []
    registry.get_event_contracts.side_effect = lambda event_id: [
        SimpleNamespace(security_id=w.security_id) for w in WINNERS if w.event_id == event_id]
    registry.get_contract_propositions.side_effect = lambda security_ids: [
        SimpleNamespace(security_id=w.security_id, subject_id=7,
                        fields={"kind": "WINNER", "scope": "match", "entity": w.entity})
        for w in WINNERS if w.security_id in security_ids]
    registry.get_connected_relationships.return_value = [
        SimpleNamespace(security_id_a=a, security_id_b=b, relationship_type="EQUIVALENT", flags=list(VOID),
                        complement_a=COMPLEMENTS.get(a), complement_b=COMPLEMENTS.get(b)) for a, b in STORED]
    registry.get_listing.side_effect = lambda security_id: [SimpleNamespace(listing_id=LISTINGS[security_id], active=True)]
    registry.get_contract_sources.return_value = []
    return registry


def test_kalshi_anchors_when_polymarket_internationals_listings_arent_registered():
    registry = _registry()
    registry.get_listing.side_effect = lambda security_id: [] if security_id in (11, 12) else [
        SimpleNamespace(listing_id=LISTINGS[security_id])]
    ((action, data, _),) = scan(registry, NOW)
    assert action == "launch" and [leg["venue"] for leg in data["legs"]] == ["kalshi", "polymarket_us"]
    assert "polymarket_slug" not in data and data["kalshi_event_ticker"] == "KXDOTA2GAME-26NOV121100KONCAPARI"


def test_a_match_past_its_run_is_still_scanned_so_its_stop_is_sent():
    registry = _registry()
    later = START + RUN_FOR + timedelta(minutes=30)
    ((action, data, _),) = scan(registry, later)
    assert action == "shutdown" and data["reason"] == "past_start"
    start_from = registry.get_event.call_args_list[0].kwargs["start_from"]
    assert datetime.fromisoformat(start_from) < START, "the scan reads far enough back to still see the match"


def test_an_inactive_listing_is_no_leg():
    registry = _registry()
    registry.get_listing.side_effect = lambda security_id: [
        SimpleNamespace(listing_id=LISTINGS[security_id], active=security_id not in (21, 22))]
    ((_, data, _),) = scan(registry, NOW)
    assert [leg["venue"] for leg in data["legs"]] == ["kalshi", "polymarket"]


def test_a_match_the_registry_fails_on_doesnt_stop_the_scan():
    registry = _registry()
    registry.get_connected_relationships.side_effect = RuntimeError("registry 502")
    assert scan(registry, NOW) == []


# ---- the rule ----------------------------------------------------------------------------------------------------

LEGS = [{"venue": "kalshi", "listing_team_a": 131, "listing_team_b": 132},
        {"venue": "polymarket", "listing_team_a": 111, "listing_team_b": 112},
        {"venue": "polymarket_us", "listing_team_a": 121, "listing_team_b": 122}]
REGIONS = {4: "eu-west-1", 5: "us-east-1", 6: "us-east-1"}


def _data(**overrides):
    return {"league": "dota2", "subject_id": 7, "market": "match", "anchor_event_id": 1, "event_ids": [1, 2, 3],
            "legs": LEGS,
            "polymarket_slug": "dota2-pari-konca-2026-11-12", "kalshi_event_ticker": "KXDOTA2GAME-26NOV121100KONCAPARI",
            **overrides}


def _params(**overrides):
    return {"strategy_id": 9, "strategy_type": "python", "mode": "paper",
            "strategy_class": "gnomepy_research.sessions.esports_cross_venue_arb.strategy:EsportsCrossVenueArb",
            "strategy_args": {"min_edge": 0.02}, **overrides}


def _ctx(start=START, registered=(111, 112, 121, 122, 131, 132), sessions=(), stopped=()):
    ctx = MagicMock()
    ctx.registry.get_event.side_effect = lambda event_ids: [SimpleNamespace(start_time=start.isoformat())] \
        if event_ids == [1] else [SimpleNamespace(start_time=(start - timedelta(hours=5)).isoformat())]
    ctx.registry.get_listing.side_effect = lambda listing_id: [
        SimpleNamespace(listing_id=listing_id, exchange_id={11: 4, 12: 6, 13: 5}[listing_id // 10])] \
        if listing_id in registered else []
    ctx.registry.get_exchange.side_effect = lambda exchange_id: [SimpleNamespace(region=REGIONS[exchange_id])]
    ctx.registry.get_strategy_sessions.side_effect = lambda strategy_id, status: \
        list(stopped) if status == "STOPPED" else list(sessions)
    return ctx


def test_follows_the_anchors_start_not_another_venues():
    # Event 2 (Polymarket US) says five hours earlier; the anchor's start decides.
    assert isinstance(EsportsGameRule().evaluate(_data(), _params(), _ctx(), now=START - timedelta(hours=4)), NoMatch)


def test_doesnt_relaunch_on_a_match_its_strategy_already_stopped_on():
    done = [SimpleNamespace(session_id="s9", config={"listings": [131, 132]})]
    evaluation = EsportsGameRule().evaluate(_data(), _params(), _ctx(stopped=done), now=NOW)
    assert isinstance(evaluation, NoMatch) and "s9" in evaluation.reason
    other = [SimpleNamespace(session_id="s8", config={"listings": [999]})]
    assert not isinstance(EsportsGameRule().evaluate(_data(), _params(), _ctx(stopped=other), now=NOW), NoMatch)


def test_launches_within_its_lead_with_the_legs_and_how_to_follow_the_match():
    evaluation = EsportsGameRule().evaluate(_data(), _params(), _ctx(), now=NOW)
    config = evaluation.resolved_config
    assert config.listings == [131, 132, 111, 112, 121, 122] and config.region == "eu-west-1"
    assert config.strategy_args == {
        "min_edge": 0.02, "legs": LEGS, "league": "dota2", "subject_id": 7, "scheduled_start": START.isoformat(),
        "polymarket_slug": "dota2-pari-konca-2026-11-12", "kalshi_event_ticker": "KXDOTA2GAME-26NOV121100KONCAPARI"}


def test_waits_until_the_start_is_within_its_lead():
    early = EsportsGameRule().evaluate(_data(), _params(), _ctx(), now=START - timedelta(minutes=25))
    assert isinstance(early, NoMatch) and "launches 10 minutes before" in early.reason
    assert not isinstance(EsportsGameRule().evaluate(_data(), _params(lead_minutes=30), _ctx(),
                                                     now=START - timedelta(minutes=25)), NoMatch)


def test_a_moved_start_moves_the_launch():
    later = EsportsGameRule().evaluate(_data(), _params(), _ctx(start=START + timedelta(hours=1)), now=NOW)
    assert isinstance(later, NoMatch)


def test_skips_other_leagues_and_markets_and_matches_on_fewer_than_two_of_its_venues():
    rule = EsportsGameRule()
    assert "not dota2" in rule.evaluate(_data(), _params(leagues=["cs2"]), _ctx(), now=NOW).reason
    assert isinstance(rule.evaluate(_data(), _params(venues=["kalshi"]), _ctx(), now=NOW), NoMatch)
    partial = rule.evaluate(_data(), _params(), _ctx(registered=(131, 132, 111)), now=NOW)
    assert "fewer than two" in partial.reason and "polymarket (listings [112]" in partial.reason


def test_a_us_only_session_is_placed_by_its_listings():
    evaluation = EsportsGameRule().evaluate(_data(legs=[LEGS[0], LEGS[2]]), _params(), _ctx(), now=NOW)
    assert evaluation.resolved_config.region is None


def test_shuts_down_the_sessions_holding_the_matchs_listings():
    running = [SimpleNamespace(session_id="s1", config={"listings": [111, 112]}),
               SimpleNamespace(session_id="s2", config={"listings": [999]})]
    evaluation = EsportsGameRule().evaluate_shutdown(_data(reason="resolved"), _params(), _ctx(sessions=running))
    assert evaluation.target_session_ids == ["s1"] and "resolved by a venue" in evaluation.reason
    assert isinstance(EsportsGameRule().evaluate_shutdown(_data(), _params(), _ctx()), NoMatch)


def test_links_to_the_venues_event_pages():
    assert [link["url"] for link in EsportsGameRule().links(_data())] == [
        "https://polymarket.com/event/dota2-pari-konca-2026-11-12",
        "https://kalshi.com/markets/kxdota2game/kxdota2game-26nov121100koncapari"]
