"""Links shown with a request, in its Slack message and on its controller page, so whoever approves it can check
what it is: its rule, its strategy by name, each listing with what it trades, the rule type's own pages (an HLTV
match), and the listings' events on their venues and in the controller.

They are worked out once, when the request is made, and stored on it as
    {"titles": [str], "strategy": link | None, "listings": [link], "pages": [link]}
where a link is {"label", "url"} and titles are the listings' events' titles, shown once rather than in every label.
Labels name a listing's venue only when the request spans venues, and an event by its title only when two share a
venue, so a one-event request reads short and a cross-venue one is still unambiguous.

Each part is best effort on its own: a registry failure costs a label or a page, never the request, and listings
still link by id."""
import logging
from dataclasses import dataclass

from launcher.config import config
from launcher.rules.catalog import RULE_TYPE_REGISTRY

logger = logging.getLogger(__name__)

SHORT_TITLE = 40


def link(label: str, url: str) -> dict:
    return {"label": label, "url": url}


def controller_url(path: str) -> str:
    return f"{config.CONTROLLER_BASE_URL}{path}"


def request_url(request_id: str) -> str:
    return controller_url(f"/launcher/requests/{request_id}")


def rule_url(rule_id: str) -> str:
    return controller_url(f"/launcher/rules/{rule_id}")


def strategy_url(strategy_id: int) -> str:
    return controller_url(f"/strategies/{strategy_id}")


def listing_url(listing_id: int) -> str:
    return controller_url(f"/security-master/listings/{listing_id}")


def session_url(session_id: str) -> str:
    return controller_url(f"/sessions/{session_id}")


def request_links(rule_type: str, data: dict, strategy_id: int | None, listings: list[int], registry) -> dict:
    found = _look_up(listings, registry)
    events = list({f.event.event_id: f.event for f in found if f.event}.values())
    return {
        "titles": [event.title for event in events],
        "strategy": _strategy_link(strategy_id, registry),
        "listings": _listing_links(found),
        "pages": _rule_pages(rule_type, data) + _event_pages(events, found),
    }


def _strategy_link(strategy_id: int | None, registry) -> dict | None:
    if strategy_id is None:
        return None
    try:
        found = registry.get_strategies(strategy_id=int(strategy_id))
    except Exception:
        logger.warning("Couldn't look up strategy %s for a request's links", strategy_id, exc_info=True)
        found = []
    label = f"{found[0].name} (ID {strategy_id})" if found else f"Strategy {strategy_id}"
    return link(label, strategy_url(int(strategy_id)))


def _rule_pages(rule_type: str, data: dict) -> list[dict]:
    try:
        return RULE_TYPE_REGISTRY[rule_type]().links(data) if rule_type in RULE_TYPE_REGISTRY else []
    except Exception:
        logger.warning("Couldn't build the %s rule's links", rule_type, exc_info=True)
        return []


@dataclass
class _Found:
    listing_id: int
    exchange: str | None = None
    outcome: str | None = None
    symbol: str | None = None
    event: object | None = None


def _look_up(listings: list[int], registry) -> list[_Found]:
    """Each listing's venue and either the event outcome it trades or, outside any event, its security."""
    exchange_names: dict[int, str] = {}
    events: dict[int, object] = {}

    def exchange_name(exchange_id: int) -> str:
        if exchange_id not in exchange_names:
            hits = registry.get_exchange(exchange_id=exchange_id)
            exchange_names[exchange_id] = hits[0].exchange_name if hits else f"Exchange {exchange_id}"
        return exchange_names[exchange_id]

    out = []
    for listing_id in listings:
        found = _Found(listing_id)
        try:
            hits = registry.get_listing(listing_id=listing_id)
            listing = hits[0] if hits else None
            if listing is not None and listing.security_id is not None:
                found.exchange = exchange_name(listing.exchange_id)
                for contract in registry.get_event_contracts(security_id=listing.security_id):
                    if contract.event_id not in events:
                        hits = registry.get_event(event_id=contract.event_id)
                        events[contract.event_id] = hits[0] if hits else None
                    event = events[contract.event_id]
                    # A security can be in events on several venues; the listing trades its own venue's.
                    if event is not None and event.exchange_id == listing.exchange_id:
                        found.event, found.outcome = event, contract.outcome_label
                        break
                if found.event is None:
                    securities = registry.get_security(security_id=listing.security_id)
                    found.symbol = securities[0].symbol if securities else None
        except Exception:
            logger.warning("Couldn't look up listing %s for a request's links", listing_id, exc_info=True)
        out.append(found)
    return out


def _short(title: str) -> str:
    return title if len(title) <= SHORT_TITLE else title[:SHORT_TITLE - 1].rstrip() + "…"


def _listing_links(found: list[_Found]) -> list[dict]:
    many_venues = len({f.exchange for f in found if f.exchange}) > 1
    links = []
    for f in found:
        if f.outcome:
            detail = f"{f.outcome} ({f.exchange})" if many_venues else f.outcome
        elif f.symbol:
            detail = f"{f.symbol} ({f.exchange})"
        else:
            detail = None
        links.append(link(f"{f.listing_id} · {detail}" if detail else str(f.listing_id), listing_url(f.listing_id)))
    return links


def _event_pages(events: list, found: list[_Found]) -> list[dict]:
    venue_of = {f.event.event_id: f.exchange for f in found if f.event}
    per_venue: dict[str, int] = {}
    for event in events:
        per_venue[venue_of[event.event_id]] = per_venue.get(venue_of[event.event_id], 0) + 1
    pages = []
    for event in events:
        venue = venue_of[event.event_id]
        name = f"{venue}: {_short(event.title)}" if per_venue[venue] > 1 else f"{venue} event"
        if event.native_url:
            pages.append(link(name, event.native_url))
        pages.append(link(f"{name} in controller", controller_url(f"/predictions/events/{event.event_id}")))
    return pages
