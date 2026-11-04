from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gnomepy.registry import ContractRelationship

from launcher.rules.classifier_event import ClassifierEventRule


def _event(category="crypto"):
    return SimpleNamespace(category=category)


def _contract(security_id, event_id=1):
    return SimpleNamespace(security_id=security_id, event_id=event_id)


def _relationship(relationship_type, a=10, b=20, flags=()):
    return ContractRelationship(relationship_id=a * 1000 + b, security_id_a=a, security_id_b=b,
                                relationship_type=relationship_type, method="derived", date_created="",
                                flags=list(flags))


def _listing(listing_id, exchange_id):
    return SimpleNamespace(listing_id=listing_id, exchange_id=exchange_id)


def _make_ctx(relationships=None, contracts=None, listings_by_security=None):
    ctx = MagicMock()
    ctx.registry.get_event.return_value = [_event("crypto")]
    ctx.registry.get_event_contracts.return_value = (
        contracts if contracts is not None else [_contract(10), _contract(11)]
    )
    ctx.registry.get_security.return_value = []
    ctx.registry.get_connected_relationships.return_value = (
        relationships if relationships is not None else [_relationship("EQUIVALENT")]
    )
    _listings = listings_by_security or {
        10: [_listing(100, 1)],
        11: [_listing(101, 2)],
    }
    ctx.registry.get_listing.side_effect = lambda security_id: _listings.get(security_id, [])
    return ctx


def _base_params(**overrides):
    return {
        "strategy_id": 42,
        "strategy_type": "python",
        "strategy_class": "ArbitrageStrategy",
        "mode": "paper",
        **overrides,
    }


def test_match_returns_evaluation():
    ctx = _make_ctx()
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(), ctx)

    assert result is not None
    assert result.should_launch is True
    assert result.resolved_config.strategy_id == 42
    assert result.resolved_config.mode == "paper"


def test_no_match_when_category_not_allowed():
    ctx = _make_ctx()
    ctx.registry.get_event.return_value = [_event("sports")]
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(allowed_categories=["crypto"]), ctx)
    assert result.reason == "Test Event: category sports isn't allowed"


def test_no_match_when_insufficient_relationships():
    ctx = _make_ctx(relationships=[], contracts=[_contract(10)])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(min_relationships=1), ctx)
    assert result.reason == "Test Event: 0 relationship(s), fewer than required"


def test_no_match_when_required_type_missing():
    ctx = _make_ctx(relationships=[_relationship("EQUIVALENT")])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(required_relationship_types=["IMPLIES"]), ctx)
    assert result.reason == "Test Event: none of the required relationship types"


def test_match_when_required_type_present():
    ctx = _make_ctx(relationships=[_relationship("EQUIVALENT"), _relationship("IMPLIES", a=11, b=30)])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(required_relationship_types=["IMPLIES"]), ctx)
    assert result is not None


def test_pairs_implied_through_stored_rows_count():
    ctx = _make_ctx(relationships=[_relationship("IMPLIES", a=10, b=20), _relationship("IMPLIES", a=20, b=30)])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(min_relationships=2), ctx)
    assert result.should_launch is True
    assert "has 2 relationships" in result.reason


def test_flagged_pairs_do_not_count():
    ctx = _make_ctx(relationships=[_relationship("EQUIVALENT", flags=["RESOLUTION_SOURCE_DIFFERS"])])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(min_relationships=1), ctx)
    assert result.reason == "Test Event: 0 relationship(s), fewer than required"


def test_exchange_filter_applied():
    ctx = _make_ctx()
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(exchange_filter=[1]), ctx)
    assert result is not None
    assert result.resolved_config.listings == [100]


def test_static_listings():
    ctx = _make_ctx()
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(
        data,
        _base_params(listing_resolution="static", static_listings="999,1000"),
        ctx,
    )
    assert result is not None
    assert result.resolved_config.listings == [999, 1000]


def test_first_matching_event_wins():
    ctx = _make_ctx()
    ctx.registry.get_event.side_effect = [
        [_event("sports")],
        [_event("crypto")],
    ]
    ctx.registry.get_event_contracts.return_value = [_contract(10)]
    ctx.registry.get_connected_relationships.return_value = [_relationship("EQUIVALENT")]
    ctx.registry.get_listing.return_value = [_listing(100, 1)]

    rule = ClassifierEventRule()
    data = {"event_ids": [1, 2], "event_names": ["Sports Event", "Crypto Event"]}
    result = rule.evaluate(data, _base_params(allowed_categories=["crypto"]), ctx)
    assert result is not None
    assert "Crypto Event" in result.reason


def _session(session_id, listings):
    return SimpleNamespace(session_id=session_id, config={"listings": listings})


def _shutdown_ctx(sessions, event_by_listing):
    ctx = MagicMock()
    ctx.registry.get_strategy_sessions.return_value = sessions
    ctx.registry.get_listing.side_effect = lambda listing_id: [SimpleNamespace(listing_id=listing_id, security_id=listing_id)]
    ctx.registry.get_event_contracts.side_effect = lambda security_id: (
        [SimpleNamespace(event_id=event_by_listing[security_id], security_id=security_id)]
        if security_id in event_by_listing else []
    )
    return ctx


def test_shutdown_targets_sessions_holding_a_resolved_event():
    sessions = [_session("a", [100]), _session("b", "101,102"), _session("c", [103])]
    ctx = _shutdown_ctx(sessions, {100: 1, 101: 2, 102: 3, 103: 4})
    data = {"event_ids": [1, 3], "event_names": ["One", "Three"]}
    result = ClassifierEventRule().evaluate_shutdown(data, _base_params(), ctx)
    assert result.target_session_ids == ["a", "b"]


def test_shutdown_lookups_scale_with_held_listings_not_resolved_events():
    sessions = [_session("a", [100]), _session("b", [100])]
    ctx = _shutdown_ctx(sessions, {100: 7})
    data = {"event_ids": list(range(1000)), "event_names": [f"Event {i}" for i in range(1000)]}
    result = ClassifierEventRule().evaluate_shutdown(data, _base_params(), ctx)
    assert result.target_session_ids == ["a", "b"]
    assert ctx.registry.get_listing.call_count == 1
    assert ctx.registry.get_event_contracts.call_count == 1


def test_shutdown_no_match_when_no_session_holds_a_resolved_event():
    ctx = _shutdown_ctx([_session("a", [100])], {100: 1})
    data = {"event_ids": [2], "event_names": ["Two"]}
    result = ClassifierEventRule().evaluate_shutdown(data, _base_params(), ctx)
    assert result.reason == "no running session holds the resolved events' listings"


def _scheduled(event_id, start, title="TYLOO vs. Liquid"):
    return [SimpleNamespace(event_id=event_id, title=title, start_time=start)]


def test_the_schedule_is_the_earliest_upcoming_start_the_venues_list():
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    later = soon + timedelta(hours=3)
    starts = {1: _scheduled(1, later.isoformat()), 2: _scheduled(2, soon.strftime("%Y-%m-%dT%H:%M:%S")),
              3: _scheduled(3, None), 4: _scheduled(4, "2020-01-01T00:00:00")}
    ctx = MagicMock()
    ctx.registry.get_event.side_effect = lambda event_ids: [e for i in event_ids for e in starts[i]]
    with patch("launcher.rules.classifier_event._get_anthropic_client") as llm:
        result = ClassifierEventRule().compute_schedule_time({"event_ids": [1, 2, 3, 4], "event_names": ["x"]}, {}, ctx)
    assert result.scheduled_time == soon.replace(microsecond=0) and "TYLOO" in result.reason
    llm.assert_not_called()
    ctx.registry.get_event.assert_called_once_with(event_ids=[1, 2, 3, 4])


def test_every_event_of_a_large_batch_is_considered():
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    ctx = MagicMock()
    ctx.registry.get_event.side_effect = lambda event_ids: [
        SimpleNamespace(event_id=i, title=f"E{i}", start_time=soon.isoformat() if i == 40 else None) for i in event_ids]
    result = ClassifierEventRule().compute_schedule_time({"event_ids": list(range(1, 51)), "event_names": ["x"]}, {}, ctx)
    assert result.reason == "Scheduled start of E40"


def test_without_a_listed_start_the_names_are_read_instead():
    ctx = MagicMock()
    ctx.registry.get_event.side_effect = lambda event_ids: [e for i in event_ids for e in _scheduled(i, None)]
    with patch("launcher.rules.classifier_event._get_anthropic_client") as llm:
        llm.return_value.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(text='{"start_time": null}')])
        assert ClassifierEventRule().compute_schedule_time({"event_ids": [1], "event_names": ["x"]}, {}, ctx) is None
    llm.assert_called_once()
