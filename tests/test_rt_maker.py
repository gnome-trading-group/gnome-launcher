import json
from types import SimpleNamespace

import pandas as pd

from launcher.handlers.custom import rt_scanner
from launcher.handlers.custom.rt_scanner import maker_groups, maker_triggers
from launcher.lifecycle import dedup_key
from launcher.rules.rt_maker import RtMakerRule
from launcher.rules.types import NoMatch
from launcher.rules.validation import validate_data
from tests.test_rt import FILM, LISTED, MARKETS, NOW, _ctx, _film

PARAMS = {"strategy_id": 10, "strategy_type": "python", "mode": "paper",
          "strategy_class": "gnomepy_research.sessions.rt_maker.strategy:RtMaker"}
OTHER = "nosferatu"
OTHER_MARKETS = [{"venue": "kalshi", "strike": 70, "yes": "KXRT-NOS-70:yes", "no": "KXRT-NOS-70:no"}]
BOTH_LISTED = {**LISTED, (5, "KXRT-NOS-70:yes"): 41, (5, "KXRT-NOS-70:no"): 42}
IN_WINDOW = pd.Timestamp("2026-11-13T09:30:00Z")


def _group_film(film_id=FILM, markets=MARKETS, close="2026-11-23T14:00:00+00:00"):
    return {"film_id": film_id, "title": film_id.title(), "rt_slug": film_id.replace("-", "_"), "close": close,
            "markets": [m for m in markets if m["venue"] == "kalshi"]}


def _launch(*films, group=0):
    return {"group": group, "films": list(films) or [_group_film()]}


def _session(session_id, listings, status="RUNNING"):
    return SimpleNamespace(session_id=session_id, status=status, config={"listings": listings})


def test_a_group_session_holds_every_films_yes_listings_with_each_film_and_its_close():
    data = _launch(_group_film(), _group_film(OTHER, OTHER_MARKETS, None))
    config = RtMakerRule().evaluate(data, PARAMS, _ctx(BOTH_LISTED)).resolved_config
    assert config.strategy_args == {"films": [
        {"film_id": FILM, "close": "2026-11-23T14:00:00+00:00", "strikes": {"60": 11}},
        {"film_id": OTHER, "close": None, "strikes": {"70": 41}}]}
    assert config.listings == [11, 41] and config.region == "us-east-1"


def test_a_full_groups_args_stay_well_inside_the_hosts_user_data():
    films = [_group_film(f"the-film-number-{k}-with-a-long-slug", _markets(f"F{k}", 10)) for k in range(10)]
    listed = {(5, m[side]): 2_700_000 + i * 2 + j for k, f in enumerate(films) for i, m in enumerate(f["markets"], k * 100)
              for j, side in enumerate(("yes", "no"))}
    config = RtMakerRule().evaluate(_launch(*films), PARAMS, _ctx(listed)).resolved_config
    assert len(config.listings) == rt_scanner.MAX_STRIKES
    # The registry refuses a session whose base64 user data passes 16 KB; listings and streams take about 3 KB of it.
    assert len(json.dumps(config.strategy_args)) < 4_000


def test_a_film_without_kalshi_strikes_in_the_registry_is_left_out_and_said_so():
    data = _launch(_group_film(), _group_film(OTHER, OTHER_MARKETS))
    evaluation = RtMakerRule().evaluate(data, PARAMS, _ctx(LISTED))
    assert [f["film_id"] for f in evaluation.resolved_config.strategy_args["films"]] == [FILM]
    assert "Nosferatu has no kalshi strike" in evaluation.reason
    listed = {k: v for k, v in LISTED.items() if v != 11}
    result = RtMakerRule().evaluate(_launch(), PARAMS, _ctx(listed))
    assert isinstance(result, NoMatch) and "kalshi >=60" in result.reason


def test_a_session_stopped_any_way_is_relaunched():
    ended = SimpleNamespace(session_id="s1", status="STOPPED", stopped_by="strategy", config={"listings": "11"})
    assert not isinstance(RtMakerRule().evaluate(_launch(), PARAMS, _ctx(sessions=[ended])), NoMatch)


def test_a_relaunch_stops_only_sessions_whose_group_has_changed():
    same = {"groups": [[_group_film()]]}
    grown = {"groups": [[_group_film(), _group_film(OTHER, OTHER_MARKETS)]]}
    running = [_session("s1", "11"), _session("s2", "41", status="STARTING")]
    assert isinstance(RtMakerRule().evaluate_shutdown(
        {"groups": [[_group_film()], [_group_film(OTHER, OTHER_MARKETS)]]}, PARAMS, _ctx(BOTH_LISTED, running)), NoMatch)
    assert RtMakerRule().evaluate_shutdown(same, PARAMS, _ctx(BOTH_LISTED, running)).target_session_ids == ["s2"]
    stale = RtMakerRule().evaluate_shutdown(grown, PARAMS, _ctx(BOTH_LISTED, [_session("s1", "11")]))
    assert stale.target_session_ids == ["s1"] and "changed" in stale.reason


def test_once_no_film_is_open_every_session_is_stopped():
    shutdown = RtMakerRule().evaluate_shutdown({"groups": []}, PARAMS, _ctx(sessions=[_session("s1", "11")]))
    assert shutdown.target_session_ids == ["s1"] and "no film is open" in shutdown.reason
    assert isinstance(RtMakerRule().evaluate_shutdown({"groups": []}, PARAMS, _ctx()), NoMatch)


def test_links_name_every_films_rotten_tomatoes_page():
    links = RtMakerRule().links(_launch(_group_film(), _group_film(OTHER, OTHER_MARKETS)))
    assert [link["url"] for link in links] == [
        "https://www.rottentomatoes.com/m/the_hunger_games_sunrise_on_the_reaping",
        "https://www.rottentomatoes.com/m/nosferatu"]


# ---- the scanner ---------------------------------------------------------------------------------------------------

def _markets(code, n):
    return [{"venue": "kalshi", "strike": 50 + 5 * i, "yes": f"KXRT-{code}-{50 + 5 * i}:yes",
             "no": f"KXRT-{code}-{50 + 5 * i}:no"} for i in range(n)]


def _groups(films, now=NOW):
    return [[f["film_id"] for f in group] for group in maker_groups(pd.DataFrame(films), now)]


def test_every_open_film_with_kalshi_strikes_is_quoted_however_far_its_close():
    films = [_film("a"), _film("far", close_in=pd.Timedelta(days=40)), _film("pm-only", markets=MARKETS[1:]),
             _film("closed", open_=False), _film("past", close_in=-pd.Timedelta(hours=1))]
    assert _groups(films) == [["a", "far"]]
    (film,) = maker_groups(pd.DataFrame([_film()]), NOW)[0]
    assert [m["venue"] for m in film["markets"]] == ["kalshi"]
    assert film["close"] == (NOW + pd.Timedelta(days=10)).isoformat()


def test_films_are_packed_in_close_order_first_fit_and_never_split(monkeypatch):
    monkeypatch.setattr(rt_scanner, "MAX_STRIKES", 5)
    films = [_film("late", close_in=pd.Timedelta(days=30), markets=_markets("L", 1)),
             _film("first", close_in=pd.Timedelta(days=2), markets=_markets("F", 3)),
             _film("second", close_in=pd.Timedelta(days=5), markets=_markets("S", 3)),
             _film("third", close_in=pd.Timedelta(days=9), markets=_markets("T", 2)),
             _film("huge", close_in=pd.Timedelta(days=20), markets=_markets("H", 7))]
    assert _groups(films) == [["first", "third"], ["second", "late"], ["huge"]]


def test_a_launch_goes_per_group_and_the_relaunch_only_in_its_hour():
    films = pd.DataFrame([_film()])
    assert [action for action, _ in maker_triggers(films, NOW)] == ["launch"]
    in_window = maker_triggers(films, IN_WINDOW)
    assert [action for action, _ in in_window] == ["launch", "shutdown"]
    assert in_window[1][1] == {"groups": [in_window[0][1]["films"]]}
    assert maker_triggers(pd.DataFrame([_film(open_=False)]), IN_WINDOW) == [("shutdown", {"groups": []})]


def test_the_maker_triggers_match_the_rules_data_schema_and_repeat_identically():
    for _, data in maker_triggers(pd.DataFrame([_film(), _film(OTHER)]), IN_WINDOW):
        assert validate_data("rt_maker", json.loads(json.dumps(data, default=str))) == []
    assert validate_data("rt_maker", {"films": []}) != []
    first = maker_triggers(pd.DataFrame([_film()]), NOW)
    again = maker_triggers(pd.DataFrame([_film(seen_ago=pd.Timedelta(hours=5))]), NOW)
    assert dedup_key("rt_maker", "launch", first[0][1]) == dedup_key("rt_maker", "launch", again[0][1])
