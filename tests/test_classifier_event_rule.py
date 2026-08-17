from types import SimpleNamespace
from unittest.mock import MagicMock

from launcher.rules.classifier_event import ClassifierEventRule
from launcher.rules.types import RuleContext


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
    assert result is None


def test_no_match_when_insufficient_relationships():
    ctx = _make_ctx(relationships=[], contracts=[_contract(10)])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(min_relationships=1), ctx)
    assert result is None


def test_no_match_when_required_type_missing():
    ctx = _make_ctx(relationships=[_relationship("equivalence")])
    rule = ClassifierEventRule()
    data = {"event_ids": [1], "event_names": ["Test Event"]}
    result = rule.evaluate(data, _base_params(required_relationship_types=["complement"]), ctx)
    assert result is None


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
    assert result.resolved_config.listings == "100"


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
    assert result.resolved_config.listings == "999,1000"


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
