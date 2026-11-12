import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from launcher.handlers.custom.rt_scanner import latest, triggers, wave_started
from launcher.lifecycle import dedup_key
from launcher.rules.rt import RtRule
from launcher.rules.types import NoMatch
from launcher.rules.validation import validate_data

NOW = pd.Timestamp("2026-11-13T12:00:00Z")
FILM = "the-hunger-games-sunrise-on-the-reaping"
MARKETS = [{"venue": "kalshi", "strike": 60, "yes": "KXRT-HUN-60:yes", "no": "KXRT-HUN-60:no"},
           {"venue": "polymarket", "strike": 60, "yes": "0xc:1", "no": "0xc:2"},
           {"venue": "polymarket_us", "strike": 60, "yes": "rotc-t-hun-gte60:long", "no": "rotc-t-hun-gte60:short"}]
# venue -> exchange id, and each esid's listing id.
EXCHANGES = {"Kalshi": 5, "Polymarket (International)": 4, "Polymarket (US)": 6}
REGIONS = {4: "eu-west-1", 5: "us-east-1", 6: "us-east-1"}
LISTED = {(5, "KXRT-HUN-60:yes"): 11, (5, "KXRT-HUN-60:no"): 12, (4, "0xc:1"): 21, (4, "0xc:2"): 22,
          (6, "rotc-t-hun-gte60:long"): 31, (6, "rotc-t-hun-gte60:short"): 32}


def _data(markets=MARKETS):
    return {"film_id": FILM, "title": "The Hunger Games: Sunrise on the Reaping", "rt_slug": "the_hunger_games",
            "markets": list(markets)}


def _params(**overrides):
    return {"strategy_id": 9, "strategy_type": "python", "mode": "paper",
            "strategy_class": "gnomepy_research.sessions.rt_review_wave.strategy:RtReviewWave", **overrides}


def _ctx(listed=LISTED, sessions=()):
    ctx = MagicMock()
    by_id = {lid: ex for (ex, _), lid in listed.items()}

    def get_listing(listing_id=None, exchange_id=None, exchange_security_id=None):
        if listing_id is not None:
            return [SimpleNamespace(listing_id=listing_id, exchange_id=by_id[listing_id])] if listing_id in by_id else []
        lid = listed.get((exchange_id, exchange_security_id))
        return [SimpleNamespace(listing_id=lid, exchange_id=exchange_id)] if lid else []

    def get_exchange(exchange_id=None, exchange_name=None):
        if exchange_name is not None:
            return [SimpleNamespace(exchange_id=EXCHANGES[exchange_name])] if exchange_name in EXCHANGES else []
        return [SimpleNamespace(region=REGIONS[exchange_id])]
    ctx.registry.get_listing.side_effect = get_listing
    ctx.registry.get_exchange.side_effect = get_exchange
    ctx.registry.get_strategy_sessions.side_effect = lambda strategy_id, status: [
        s for s in sessions if s.status in status.split(",")]
    return ctx


def test_one_session_per_film_holds_each_venues_strikes_and_runs_in_kalshis_region():
    evaluation = RtRule().evaluate(_data(), _params(), _ctx())
    config = evaluation.resolved_config
    assert config.strategy_args == {"film_id": FILM, "legs": [
        {"venue": "kalshi", "strike": 60, "yes_listing": 11, "no_listing": 12},
        {"venue": "polymarket", "strike": 60, "yes_listing": 21, "no_listing": 22},
        {"venue": "polymarket_us", "strike": 60, "yes_listing": 31, "no_listing": 32}]}
    assert config.listings == [11, 12, 21, 22, 31, 32] and config.region == "us-east-1"


def test_a_strike_missing_a_side_in_the_registry_is_skipped_and_said_so():
    listed = {k: v for k, v in LISTED.items() if v != 22}
    evaluation = RtRule().evaluate(_data(), _params(venues=["kalshi", "polymarket"]), _ctx(listed))
    assert [leg["venue"] for leg in evaluation.resolved_config.strategy_args["legs"]] == ["kalshi"]
    assert "skipped polymarket >=60" in evaluation.reason


def test_without_kalshi_the_registry_places_the_session():
    evaluation = RtRule().evaluate(_data(MARKETS[1:]), _params(), _ctx())
    assert evaluation.resolved_config.region is None


def test_nothing_launches_without_registered_strikes_or_after_the_films_session_stopped():
    assert isinstance(RtRule().evaluate(_data(), _params(), _ctx(listed={})), NoMatch)
    stopped = SimpleNamespace(session_id="s1", status="STOPPED", config={"listings": "11,12,21"})
    result = RtRule().evaluate(_data(), _params(), _ctx(sessions=[stopped]))
    assert isinstance(result, NoMatch) and "already traded" in result.reason
    failed = SimpleNamespace(session_id="s2", status="FAILED", config={"listings": "11,12,21"})
    assert not isinstance(RtRule().evaluate(_data(), _params(), _ctx(sessions=[failed])), NoMatch), "relaunched"


def test_a_closed_film_stops_its_running_sessions_only():
    mine = SimpleNamespace(session_id="s1", status="RUNNING", config={"strategy.args.film_id": FILM})
    other = SimpleNamespace(session_id="s2", status="RUNNING", config={"strategy.args.film_id": "dune-part-three"})
    shutdown = RtRule().evaluate_shutdown({**_data(), "reason": "closed"}, _params(), _ctx(sessions=[mine, other]))
    assert shutdown.target_session_ids == ["s1"] and "no longer has open markets" in shutdown.reason
    assert isinstance(RtRule().evaluate_shutdown(_data(), _params(), _ctx(sessions=[other])), NoMatch)


# ---- the scanner ---------------------------------------------------------------------------------------------------

def _film(film_id=FILM, close_in=pd.Timedelta(days=10), open_=True, seen_ago=pd.Timedelta(hours=1), markets=MARKETS):
    return {"film_id": film_id, "title": film_id.title(), "rt_slug": None, "close": NOW + close_in, "open": open_,
            "markets": json.dumps(list(reversed(markets))), "first_seen_at": NOW - seen_ago}


def _review(i, seen_ago, published_ago=None, is_review=True, film_id=FILM):
    return {"url": f"u{i}", "film_id": film_id, "published_at": NOW - (published_ago or seen_ago),
            "first_seen_at": NOW - seen_ago, "is_review": is_review}


def _scan(films, reviews=()):
    return triggers(pd.DataFrame(films), pd.DataFrame(list(reviews)), NOW)


def test_a_film_closing_within_13_days_with_no_wave_yet_is_launched_with_sorted_markets():
    (only,) = _scan([_film()])
    action, data = only
    assert action == "launch" and [m["venue"] for m in data["markets"]] == ["kalshi", "polymarket", "polymarket_us"]
    assert _scan([_film(close_in=pd.Timedelta(days=20))]) == [], "too far out"
    assert _scan([_film(markets=[])]) == [], "nothing to trade"


def test_a_film_whose_wave_has_started_is_not_launched():
    wave = [_review(i, pd.Timedelta(minutes=30 - i)) for i in range(3)]
    assert _scan([_film()], wave) == []
    scattered = [_review(i, pd.Timedelta(days=i * 2 + 1)) for i in range(3)]
    assert _scan([_film()], scattered)[0][0] == "launch", "three reviews over a week are no wave"
    backlog = [_review(i, pd.Timedelta(minutes=5), published_ago=pd.Timedelta(days=30)) for i in range(5)]
    assert _scan([_film()], backlog)[0][0] == "launch", "old reviews read late don't start a wave"
    not_reviews = [_review(i, pd.Timedelta(minutes=30 - i), is_review=False) for i in range(3)]
    assert _scan([_film()], not_reviews)[0][0] == "launch"


def test_closed_and_past_close_films_are_shut_down_for_two_days():
    assert _scan([_film(open_=False)]) == [("shutdown", {**_scan([_film()])[0][1], "reason": "closed"})]
    assert _scan([_film(open_=False, seen_ago=pd.Timedelta(days=3))]) == []
    assert _scan([_film(close_in=-pd.Timedelta(hours=1))])[0][1]["reason"] == "past_close"


def test_every_scan_sends_the_same_trigger_for_an_unchanged_film():
    first, again = _scan([_film()]), _scan([_film(seen_ago=pd.Timedelta(hours=5))])
    assert dedup_key("rt", "launch", first[0][1]) == dedup_key("rt", "launch", again[0][1])


def test_the_newest_row_of_each_film_counts():
    rows = pd.DataFrame([_film(open_=True, seen_ago=pd.Timedelta(hours=3)), _film(open_=False)])
    assert latest(rows, "film_id")["open"].tolist() == [False]
    assert wave_started(pd.DataFrame([_review(i, pd.Timedelta(minutes=i)) for i in range(3)]))


def test_the_scanners_triggers_match_the_rules_data_schema():
    for _, data in _scan([_film()]) + _scan([_film(open_=False)]):
        assert validate_data("rt", json.loads(json.dumps(data, default=str))) == []


def test_rows_written_before_films_had_markets_launch_nothing_and_still_shut_down():
    old = pd.DataFrame([{k: v for k, v in _film().items() if k != "markets"}]).assign(markets=None)
    assert triggers(old, pd.DataFrame(), NOW) == []
    closed = pd.DataFrame([{k: v for k, v in _film(open_=False).items() if k != "markets"}]).assign(markets=None)
    assert [a for a, _ in triggers(closed, pd.DataFrame(), NOW)] == ["shutdown"]


def test_a_film_rt_already_scores_is_not_launched():
    scores = pd.DataFrame([{"film_id": FILM, "liked": 47, "not_liked": 13}])
    assert triggers(pd.DataFrame([_film()]), pd.DataFrame(), NOW, scores) == []
    few = pd.DataFrame([{"film_id": FILM, "liked": 2, "not_liked": 0}])
    assert triggers(pd.DataFrame([_film()]), pd.DataFrame(), NOW, few)[0][0] == "launch"
    assert triggers(pd.DataFrame([_film(open_=False)]), pd.DataFrame(), NOW, scores)[0][0] == "shutdown"
