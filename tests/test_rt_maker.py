import json
from types import SimpleNamespace

import pandas as pd

from launcher.handlers.custom.rt_scanner import maker_triggers
from launcher.lifecycle import dedup_key
from launcher.rules.rt_maker import RtMakerRule
from launcher.rules.types import NoMatch
from launcher.rules.validation import validate_data
from tests.test_rt import FILM, LISTED, MARKETS, NOW, _ctx, _data, _film

PARAMS = {"strategy_id": 10, "strategy_type": "python", "mode": "paper",
          "strategy_class": "gnomepy_research.sessions.rt_maker.strategy:RtMaker"}


def test_the_session_quotes_kalshis_strikes_holding_only_their_yes_listings():
    evaluation = RtMakerRule().evaluate(_data(), PARAMS, _ctx())
    config = evaluation.resolved_config
    assert config.strategy_args == {"film_id": FILM, "legs": [
        {"venue": "kalshi", "strike": 60, "yes_listing": 11, "no_listing": 12}]}
    assert config.listings == [11] and config.region == "us-east-1"


def test_nothing_launches_without_kalshi_strikes_in_the_registry():
    assert isinstance(RtMakerRule().evaluate(_data(MARKETS[1:]), PARAMS, _ctx()), NoMatch)
    listed = {k: v for k, v in LISTED.items() if v != 11}
    result = RtMakerRule().evaluate(_data(), PARAMS, _ctx(listed))
    assert isinstance(result, NoMatch) and "kalshi >=60" in result.reason


def test_a_session_stopped_any_way_is_relaunched():
    ended = SimpleNamespace(session_id="s1", status="STOPPED", stopped_by="strategy", config={"listings": "11"})
    assert not isinstance(RtMakerRule().evaluate(_data(), PARAMS, _ctx(sessions=[ended])), NoMatch)


def test_a_closed_film_stops_its_maker_session():
    mine = SimpleNamespace(session_id="s1", status="RUNNING", config={"strategy.args.film_id": FILM})
    shutdown = RtMakerRule().evaluate_shutdown({**_data(), "reason": "closed"}, PARAMS, _ctx(sessions=[mine]))
    assert shutdown.target_session_ids == ["s1"]


def _scan(films):
    return maker_triggers(pd.DataFrame(films), NOW)


def test_an_open_film_with_kalshi_strikes_is_launched_however_far_its_close_and_whatever_its_reviews():
    (only,) = _scan([_film()])
    action, data = only
    assert action == "launch" and [m["venue"] for m in data["markets"]] == ["kalshi"]
    assert _scan([_film(close_in=pd.Timedelta(days=40))])[0][0] == "launch"
    assert _scan([_film(markets=MARKETS[1:])]) == [], "no Kalshi strikes"


def test_closed_and_past_close_films_are_shut_down():
    assert _scan([_film(open_=False)])[0] == ("shutdown", {**_scan([_film()])[0][1], "reason": "closed"})
    assert _scan([_film(close_in=-pd.Timedelta(hours=1))])[0][1]["reason"] == "past_close"
    assert _scan([_film(open_=False, seen_ago=pd.Timedelta(days=3))]) == []


def test_the_maker_triggers_match_the_rules_data_schema_and_repeat_identically():
    for _, data in _scan([_film()]) + _scan([_film(open_=False)]):
        assert validate_data("rt_maker", json.loads(json.dumps(data, default=str))) == []
    first, again = _scan([_film()]), _scan([_film(seen_ago=pd.Timedelta(hours=5))])
    assert dedup_key("rt_maker", "launch", first[0][1]) == dedup_key("rt_maker", "launch", again[0][1])
