from types import SimpleNamespace
from unittest.mock import MagicMock

from launcher.rules.classifier_event import ClassifierEventRule


def _event(category="crypto"):
    return SimpleNamespace(category=category)


def _contract(security_id):
    return SimpleNamespace(security_id=security_id)


def _relationship(relationship_type):
    return SimpleNamespace(relationship_type=relationship_type)


def _listing(listing_id, exchange_id):
    return SimpleNamespace(listing_id=listing_id, exchange_id=exchange_id)


def _make_ctx(relationships=None, contracts=None, listings_by_security=None):
    ctx = MagicMock()
    ctx.registry.get_event.return_value = [_event("crypto")]
    ctx.registry.get_event_contracts.return_value = (
        contracts if contracts is not None else [_contract(10), _contract(11)]
    )
    ctx.registry.get_contract_relationships.return_value = (
        relationships if relationships is not None else [_relationship("equivalence")]
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
    ctx = _make_ctx(relationships=[_relationship("equivalence")])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(required_relationship_types=["complement"]), ctx)
    assert result.reason == "Test Event: none of the required relationship types"


def test_match_when_required_type_present():
    ctx = _make_ctx(relationships=[
        _relationship("equivalence"),
        _relationship("complement"),
    ])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(required_relationship_types=["complement"]), ctx)
    assert result is not None


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
    ctx.registry.get_contract_relationships.return_value = [_relationship("equivalence")]
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
