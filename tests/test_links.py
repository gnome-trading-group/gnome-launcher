import json
from types import SimpleNamespace

import pytest

from launcher import slack_client
from launcher.handlers import shutdown_processor, trigger_processor
from launcher.handlers.api import launch_requests
from launcher.links import request_links
from launcher.rules.types import ResolvedStrategyConfig
from tests.conftest import STRATEGY
from tests.test_tracking import api

CONTROLLER = "https://controller.gnometrading.group"
CS2 = {"match_id": 2399544, "venue": "polymarket", "market": "series", "listing_team_a": 11, "listing_team_b": 12}


@pytest.fixture
def security_master(ctx):
    """Two listings of one Polymarket event (and the same securities' Kalshi event, which they must not pick up)."""
    registry = ctx.registry
    registry.listings = {11: SimpleNamespace(listing_id=11, security_id=101, exchange_id=3),
                         12: SimpleNamespace(listing_id=12, security_id=102, exchange_id=3)}
    registry.contracts = [SimpleNamespace(event_id=900, security_id=101, outcome_label="Vitality"),
                          SimpleNamespace(event_id=901, security_id=101, outcome_label="Vitality (Kalshi)"),
                          SimpleNamespace(event_id=900, security_id=102, outcome_label="NAVI")]
    registry.events = {
        900: SimpleNamespace(event_id=900, title="Vitality vs NAVI", exchange_id=3,
                             native_url="https://polymarket.com/event/cs2-vit-navi"),
        901: SimpleNamespace(event_id=901, title="Vitality vs NAVI", exchange_id=5, native_url="https://kalshi.com/x"),
    }
    registry.exchanges = {3: SimpleNamespace(exchange_name="POLYMARKET_INTL")}
    registry.strategies = {7: SimpleNamespace(name="cs2_series")}
    return registry


def test_a_cs2_request_links_its_match_event_strategy_and_each_listing_by_outcome(ctx, security_master):
    links = request_links("cs2", CS2, 7, [11, 12], ctx.registry)

    assert links["titles"] == ["Vitality vs NAVI"]
    assert links["strategy"] == {"label": "cs2_series (ID 7)", "url": f"{CONTROLLER}/strategies/7"}
    assert links["listings"] == [
        {"label": "11 · Vitality", "url": f"{CONTROLLER}/security-master/listings/11"},
        {"label": "12 · NAVI", "url": f"{CONTROLLER}/security-master/listings/12"},
    ]
    assert links["pages"] == [
        {"label": "HLTV match 2399544", "url": "https://www.hltv.org/matches/2399544/match"},
        {"label": "POLYMARKET_INTL event", "url": "https://polymarket.com/event/cs2-vit-navi"},
        {"label": "POLYMARKET_INTL event in controller", "url": f"{CONTROLLER}/predictions/events/900"},
    ]


def test_a_listing_outside_any_event_is_labelled_with_its_security_and_exchange(ctx, security_master):
    ctx.registry.listings[3] = SimpleNamespace(listing_id=3, security_id=301, exchange_id=8)
    ctx.registry.securities[301] = SimpleNamespace(symbol="BTC-USDC-PERP")
    ctx.registry.exchanges[8] = SimpleNamespace(exchange_name="Hyperliquid")

    links = request_links("probe", {}, 7, [3], ctx.registry)

    assert links["listings"] == [{"label": "3 · BTC-USDC-PERP (Hyperliquid)", "url": f"{CONTROLLER}/security-master/listings/3"}]
    assert links["titles"] == [] and links["pages"] == []


def test_a_cross_venue_request_names_each_listings_venue_and_tells_its_events_apart(ctx, security_master):
    registry = ctx.registry
    registry.listings[21] = SimpleNamespace(listing_id=21, security_id=201, exchange_id=5)
    registry.listings[31] = SimpleNamespace(listing_id=31, security_id=301, exchange_id=5)
    registry.contracts += [SimpleNamespace(event_id=910, security_id=201, outcome_label="Vitality"),
                           SimpleNamespace(event_id=911, security_id=301, outcome_label="Yes")]
    registry.events[910] = SimpleNamespace(event_id=910, title="Vitality vs NAVI (Kalshi)", exchange_id=5,
                                           native_url="https://kalshi.com/a")
    registry.events[911] = SimpleNamespace(event_id=911, title="Will the IEM Cologne final go to five maps?",
                                           exchange_id=5, native_url=None)
    registry.exchanges[5] = SimpleNamespace(exchange_name="Kalshi")

    links = request_links("probe", {}, 7, [11, 21, 31], registry)

    assert [listing["label"] for listing in links["listings"]] == [
        "11 · Vitality (POLYMARKET_INTL)", "21 · Vitality (Kalshi)", "31 · Yes (Kalshi)"]
    assert links["titles"] == ["Vitality vs NAVI", "Vitality vs NAVI (Kalshi)", "Will the IEM Cologne final go to five maps?"]
    assert [page["label"] for page in links["pages"]] == [
        "POLYMARKET_INTL event", "POLYMARKET_INTL event in controller",
        "Kalshi: Vitality vs NAVI (Kalshi)", "Kalshi: Vitality vs NAVI (Kalshi) in controller",
        "Kalshi: Will the IEM Cologne final go to five m… in controller",
    ]


def test_a_large_request_stays_inside_slacks_limits_and_points_at_the_controller(ctx):
    links = {"titles": [f"Event {i}" for i in range(5)], "strategy": None,
             "listings": [{"label": f"{i} · " + "x" * 200, "url": f"{CONTROLLER}/l/{i}"} for i in range(40)],
             "pages": [{"label": f"page {i}", "url": f"https://example.com/{i}"} for i in range(30)]}
    config = ResolvedStrategyConfig(**STRATEGY, listings=list(range(40)))

    blocks = slack_client._build_approval_blocks("req-1", "rule", "rule-1", "why", config, links)

    headline, listings = blocks[1]["text"]["text"], blocks[2]["fields"][3]["text"]
    assert headline.count("Event") == 3 and "+2 more in the controller" in headline
    assert listings.count("<") == 10 and "+30 more in the controller" in listings and len(listings) < 2000
    assert "+20 more in the controller" in blocks[3]["elements"][0]["text"]


def test_a_registry_outage_still_links_every_listing_by_id(ctx):
    def down(**_):
        raise ConnectionError("registry down")
    ctx.registry.get_listing = down
    ctx.registry.get_strategies = down

    links = request_links("cs2", CS2, 7, [11, 12], ctx.registry)

    assert links["strategy"] == {"label": "Strategy 7", "url": f"{CONTROLLER}/strategies/7"}
    assert [listing["label"] for listing in links["listings"]] == ["11", "12"]
    assert [page["label"] for page in links["pages"]] == ["HLTV match 2399544"] and links["titles"] == []


def _texts(blocks):
    out = []
    for block in blocks:
        for item in [block.get("text")] + block.get("fields", []) + block.get("elements", []):
            if isinstance(item, dict) and "text" in item:
                out.append(item["text"] if isinstance(item["text"], str) else item["text"].get("text", ""))
    return "\n".join(out)


def test_the_approval_message_links_its_rule_strategy_listings_and_pages(ctx, security_master):
    links = request_links("cs2", CS2, 7, [11, 12], ctx.registry)
    config = ResolvedStrategyConfig(**STRATEGY, listings=[11, 12])

    blocks = slack_client._build_approval_blocks("req-1", "CS2 <paper>", "rule-1", "CS2 match 2399544 series on polymarket",
                                                 config, links)
    text = _texts(blocks)

    assert f"<{CONTROLLER}/launcher/rules/rule-1|CS2 &lt;paper&gt;>" in text
    assert f"<{CONTROLLER}/strategies/7|cs2_series (ID 7)>" in text
    assert f"<{CONTROLLER}/security-master/listings/11|11 · Vitality>" in text
    assert "<https://www.hltv.org/matches/2399544/match|HLTV match 2399544>" in text
    assert "*CS2 match 2399544 series on polymarket*\nVitality vs NAVI" in text
    assert f"<{CONTROLLER}/launcher/requests/req-1|Open in controller>" in text
    assert [b["action_id"] for b in blocks[-1]["elements"]] == ["launch_approve", "launch_reject"]


def test_a_request_made_before_links_were_stored_still_links_its_listings(ctx):
    config = ResolvedStrategyConfig(**STRATEGY, listings=[11])

    text = _texts(slack_client._build_approval_blocks("req-1", "rule", "rule-1", "why", config, {}))

    assert f"<{CONTROLLER}/security-master/listings/11|11>" in text and f"<{CONTROLLER}/strategies/7|Strategy 7>" in text


def test_requests_store_their_links_and_hand_them_to_slack(ctx, make_rule, security_master):
    rule = make_rule(launch_path="approval")

    trigger_processor.process({"rule_type": "probe", "data": {"listings": [11, 12]}}, ctx)

    [request] = ctx.dynamo.requests_in_status("PENDING_APPROVAL")
    [(kind, sent)] = ctx.slack.sent
    assert request["links"]["strategy"]["label"] == "cs2_series (ID 7)"
    assert [listing["label"] for listing in request["links"]["listings"]] == ["11 · Vitality", "12 · NAVI"]
    assert kind == "approval" and sent["links"] == request["links"] and sent["rule_id"] == rule["rule_id"]


def test_a_shutdown_request_links_its_strategy_and_rule(ctx, make_rule, security_master):
    rule = make_rule(shutdown_path="approval")
    trigger_processor.process({"rule_type": "probe", "data": {"listings": [11]}}, ctx)

    shutdown_processor.process({"rule_type": "probe", "data": {"listings": [11]}}, ctx)

    [request] = ctx.dynamo.requests_in_status("PENDING_SHUTDOWN_APPROVAL")
    kind, sent = ctx.slack.sent[-1]
    assert kind == "shutdown_approval" and sent["rule_id"] == rule["rule_id"]
    assert request["links"]["strategy"]["label"] == "cs2_series (ID 7)" and request["links"]["listings"] == []


@pytest.fixture
def slack_on(ctx, monkeypatch):
    monkeypatch.setattr(launch_requests, "SlackClient", lambda: ctx.slack)
    monkeypatch.setattr(launch_requests, "RegistryClient", lambda: ctx.registry)
    monkeypatch.setattr(launch_requests, "_resolve_slack",
                        lambda request, text: ctx.slack.update_message(request.get("slack_message_ts"), text))


def test_resending_posts_a_new_message_retires_the_old_one_and_adds_missing_links(ctx, make_rule, security_master,
                                                                                    slack_on):
    make_rule(launch_path="approval")
    trigger_processor.process({"rule_type": "probe", "data": {"listings": [11]}}, ctx)
    [pending] = ctx.dynamo.requests_in_status("PENDING_APPROVAL")
    ctx.dynamo.annotate(pending["request_id"], links=None)

    status, body = api(launch_requests.handler, "POST", {"id": pending["request_id"], "action": "resend"})

    kinds = [kind for kind, _ in ctx.slack.sent]
    assert status == 200 and kinds == ["approval", "approval", "update"]
    assert ctx.slack.sent[-1][1] == {"message_ts": pending["slack_message_ts"],
                                     "text": "Resent by ui:mason@example.com; use the newer message"}
    assert body["slack_message_ts"] == "ts-2" and body["links"]["listings"][0]["label"] == "11 · Vitality"
    assert json.loads(json.dumps(ctx.dynamo.get_request(pending["request_id"])["links"])) == body["links"]


def test_only_a_request_awaiting_approval_can_be_resent(ctx, make_rule, slack_on):
    make_rule()
    trigger_processor.process({"rule_type": "probe", "data": {"listings": [11]}}, ctx)
    [launched] = ctx.dynamo.requests_in_status("LAUNCHED")

    status, body = api(launch_requests.handler, "POST", {"id": launched["request_id"], "action": "resend"})

    assert status == 409 and "LAUNCHED" in body["message"] and not ctx.slack.sent
