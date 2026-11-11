"""
Every few minutes: turn the registry's esports matches into esports_game launch and shutdown triggers.

The classifier reads every venue's match-winner contracts into propositions and joins one match across venues into
one canonical subject (its merge pass and key aliases), deriving EQUIVALENT relationships between the contracts that
pay on the same team. So a match is a subject, and each venue's pair of team contracts for it is a leg:

- events of the scanned leagues (by tag) scheduled from RUN_FOR ago to LOOKAHEAD ahead, their contracts and
  propositions; a venue's leg is its one event holding exactly two match-winner contracts naming different teams;
  a venue with two such events on one subject (two fixtures, or a duplicate listing) is left out, being ambiguous;
- the anchor is Polymarket International's leg, else Kalshi's: the session follows the match's live state on that
  venue's feed (Polymarket's sports websocket by event slug, Kalshi's milestone by event ticker), so a match neither
  lists isn't launched;
- another venue's leg is used only if its contracts are EQUIVALENT to the anchor's, team for team, and neither is
  EQUIVALENT to the anchor's other team, in the relation the stored rows rebuild (gnomepy `relations.close`: the
  classifier stores only the rows nothing else rebuilds, so a Polymarket pair's second team is equivalent only through
  its event's complement, and a flagged row never chains through another flagged one); its teams are put in the
  anchor's order. A venue that fails is left out and logged. A match left with fewer than two legs isn't sent.

One launch trigger per match while it hasn't been resolved and started under RUN_FOR ago; the rule launches once the
anchor's start is within its lead. One shutdown trigger once any leg's event is resolved, or RUN_FOR after the start,
sent for SHUTDOWN_TAIL more (events are read from RUN_FOR + SHUTDOWN_TAIL back, or a match would leave the scan the
moment its stop came due).
Each scan resends the same triggers; dedup and the active-session check make repeats no-ops. Trigger data holds ids
only (legs sorted by venue), so it stays the same while a match is moved; the deadline (start + RUN_FOR) moves.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import boto3
from gnomepy.registry import RegistryClient
from gnomepy.registry.relations import Edge, close

from launcher.queues import send_trigger

logger = logging.getLogger(__name__)

RULE_TYPE = "esports_game"
# League -> the tag the classifier gives its events.
LEAGUES = {"cs2": "cs2", "dota2": "dota 2", "valorant": "valorant"}
VENUES = {"POLYMARKET_INTL": "polymarket", "KALSHI": "kalshi", "POLYMARKET_US": "polymarket_us"}
ANCHORS = ("polymarket", "kalshi")
MARKET = "match"
# Rules launch up to 30 minutes ahead (lead_minutes); a trigger needs to exist by then.
LOOKAHEAD = timedelta(minutes=30)
# A best-of-five runs about five hours; a session past this is freed even if no venue has resolved the match.
RUN_FOR = timedelta(hours=6)
# How long past RUN_FOR a match's shutdown keeps being sent: scans may be missed.
SHUTDOWN_TAIL = timedelta(hours=2)
WORKERS = 8


@dataclass(frozen=True)
class Event:
    event_id: int
    venue: str
    league: str
    start: datetime
    resolved: bool
    native_event_id: str


@dataclass(frozen=True)
class Winner:
    """A match-winner contract: the security paying if `entity` wins."""
    security_id: int
    event_id: int
    subject_id: int
    entity: str


@dataclass(frozen=True)
class Leg:
    venue: str
    event: Event
    team_a: int  # security ids
    team_b: int


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def winners(propositions: dict[int, tuple[int, dict]], contracts: dict[int, list[int]]) -> list[Winner]:
    """The match-winner contracts among each event's contracts: WINNER at the match's scope, the Yes side, the team
    known."""
    out = []
    for event_id, security_ids in contracts.items():
        for sid in security_ids:
            if sid not in propositions:
                continue
            subject_id, f = propositions[sid]
            unknown = set(f.get("unknown") or ())
            if (f.get("kind") == "WINNER" and f.get("scope") == MARKET and not f.get("negated") and f.get("entity")
                    and "entity" not in unknown and "subject" not in unknown):
                out.append(Winner(sid, event_id, subject_id, f["entity"]))
    return out


def venue_pairs(subject_winners: list[Winner], events: dict[int, Event]) -> tuple[dict[str, list[Winner]], list[str]]:
    """Each venue's one event of two match-winner contracts naming different teams, and why any venue has none."""
    by_venue: dict[str, list[list[Winner]]] = defaultdict(list)
    per_event: dict[int, list[Winner]] = defaultdict(list)
    for w in subject_winners:
        per_event[w.event_id].append(w)
    for event_id, ws in per_event.items():
        if len(ws) == 2 and ws[0].entity != ws[1].entity:
            by_venue[events[event_id].venue].append(sorted(ws, key=lambda w: w.security_id))
    notes, pairs = [], {}
    for venue, found in by_venue.items():
        if len(found) > 1:
            notes.append(f"{venue}: {len(found)} match-winner events on the subject")
        else:
            pairs[venue] = found[0]
    return pairs, notes


def match_legs(subject_winners: list[Winner], events: dict[int, Event], edges: list[Edge],
               complements: dict[int, int]) -> tuple[list[Leg], list[str]]:
    """One subject's legs, the anchor's first, each in the anchor's team order, and why any venue was left out.
    `edges` are the stored rows among the contracts and `complements` each BINARY event's other security."""
    pairs, notes = venue_pairs(subject_winners, events)
    anchor = next((v for v in ANCHORS if v in pairs), None)
    if anchor is None:
        return [], notes + ["neither Polymarket International nor Kalshi lists the match: no live signal"]
    rel = close(edges, complements)

    def same(x: int, y: int) -> bool:
        return (x, y) in rel and (y, x) in rel

    a, b = (w.security_id for w in pairs[anchor])
    if same(a, b):
        return [], notes + [f"{anchor}'s two teams are related as equivalent: the subject is wrong"]
    legs = [Leg(anchor, events[pairs[anchor][0].event_id], a, b)]
    for venue, (x, y) in sorted(pairs.items()):
        if venue == anchor:
            continue
        x, y = x.security_id, y.security_id
        if same(x, a) and same(y, b) and not same(x, b) and not same(y, a):
            legs.append(Leg(venue, events[pairs[venue][0].event_id], x, y))
        elif same(x, b) and same(y, a) and not same(x, a) and not same(y, b):
            legs.append(Leg(venue, events[pairs[venue][0].event_id], y, x))
        else:
            notes.append(f"{venue}: its teams aren't related to {anchor}'s team for team")
    return legs, notes


def _data(legs: list[Leg], listings: dict[int, int], subject_id: int, slug: str | None) -> dict:
    data = {
        "league": legs[0].event.league, "subject_id": subject_id, "market": MARKET,
        "anchor_event_id": legs[0].event.event_id,
        "event_ids": sorted({leg.event.event_id for leg in legs}),
        "legs": sorted(({"venue": leg.venue, "listing_team_a": listings[leg.team_a],
                         "listing_team_b": listings[leg.team_b]} for leg in legs), key=lambda leg: leg["venue"]),
    }
    if slug:
        data["polymarket_slug"] = slug
    kalshi = next((leg for leg in legs if leg.venue == "kalshi"), None)
    if kalshi is not None:
        data["kalshi_event_ticker"] = kalshi.event.native_event_id
    return data


def shutdown_reason(pairs: list[Leg], now: datetime) -> str | None:
    """Why a match's sessions stop: a venue resolved it, or its anchor started RUN_FOR ago (the first pair is the
    anchor's when there is one); None while it may still trade."""
    if any(leg.event.resolved for leg in pairs):
        return "resolved"
    if now >= pairs[0].event.start + RUN_FOR:
        return "past_start"
    return None


def triggers(legs: list[Leg], every_pair: list[Leg], listings: dict[int, int], subject_id: int, slug: str | None,
             now: datetime) -> list[tuple[str, dict, str | None]]:
    """("launch" | "shutdown", data, deadline) for one match: a launch with the verified legs (the anchor's first),
    or a shutdown with every venue's pair, verified or not. A shutdown only finds the sessions holding its listings,
    and once a venue resolves the match, its contracts' relationships may no longer show the legs."""
    reason = shutdown_reason(every_pair, now)
    if reason is not None:
        held = [leg for leg in every_pair if leg.team_a in listings and leg.team_b in listings]
        return [("shutdown", {**_data(held, listings, subject_id, slug), "reason": reason}, None)] if held else []
    if len(legs) < 2:
        return []
    start = legs[0].event.start
    if start <= now + LOOKAHEAD:
        return [("launch", _data(legs, listings, subject_id, slug), (start + RUN_FOR).isoformat())]
    return []


def scan(registry: RegistryClient, now: datetime) -> list[tuple[str, dict, str | None]]:
    codes = {e.exchange_id: e.exchange_code for e in registry.get_exchange()}
    events: dict[int, Event] = {}
    for league, tag in LEAGUES.items():
        for e in registry.get_event(tags=[tag], start_from=(now - RUN_FOR - SHUTDOWN_TAIL).isoformat(),
                                    start_to=(now + LOOKAHEAD).isoformat()) or []:
            venue = VENUES.get(codes.get(e.exchange_id))
            start = _time(e.start_time)
            if venue and start:
                events[e.event_id] = Event(e.event_id, venue, league, start, bool(e.resolved), e.native_event_id)
    with ThreadPoolExecutor(WORKERS) as pool:
        contracts = dict(zip(events, pool.map(
            lambda eid: [c.security_id for c in registry.get_event_contracts(event_id=eid) or []], events)))
    security_ids = sorted({sid for sids in contracts.values() for sid in sids})
    propositions = {}
    for i in range(0, len(security_ids), 200):
        for p in registry.get_contract_propositions(security_ids=security_ids[i:i + 200]) or []:
            propositions[p.security_id] = (p.subject_id, p.fields or {})
    by_subject: dict[int, list[Winner]] = defaultdict(list)
    for w in winners(propositions, contracts):
        by_subject[w.subject_id].append(w)

    def one(subject_id: int) -> list[tuple[str, dict, str | None]]:
        listings, active = {}, set()
        for w in by_subject[subject_id]:
            found = registry.get_listing(security_id=w.security_id) or []
            if found:
                live = [x for x in found if getattr(x, "active", True)]
                listings[w.security_id] = (live or found)[0].listing_id
                if live:
                    active.add(w.security_id)
        ws = [w for w in by_subject[subject_id] if w.security_id in listings]
        if not ws:
            return []
        rels = registry.get_connected_relationships(security_ids=[w.security_id for w in ws]) or []
        edges = [Edge(r.security_id_a, r.security_id_b, r.relationship_type, tuple(r.flags or ())) for r in rels]
        complements = {}
        for r in rels:
            for sid, other in ((r.security_id_a, r.complement_a), (r.security_id_b, r.complement_b)):
                if other is not None:
                    complements[sid], complements[other] = other, sid
        pairs, _ = venue_pairs(ws, events)
        every_pair = sorted((Leg(venue, events[x.event_id], x.security_id, y.security_id)
                             for venue, (x, y) in pairs.items()),
                            key=lambda leg: (ANCHORS.index(leg.venue) if leg.venue in ANCHORS else len(ANCHORS)))
        if not every_pair:
            return []
        # Whether the match is over is read from every venue, listings retired or not: a venue that resolved it has
        # retired them. Only the legs a session would trade must be active.
        stopping = shutdown_reason(every_pair, now) is not None
        legs, notes = match_legs([w for w in ws if w.security_id in active], events, edges, complements)
        if not stopping:
            for note in notes:
                logger.info("subject %d: %s", subject_id, note)
            if len(legs) < 2:
                return []
        first = legs[0] if legs else every_pair[0]
        slug = None
        if first is not None and first.venue == "polymarket":
            sources = registry.get_contract_sources(security_ids=[first.team_a]) or []
            slug = next(((s.source or {}).get("event", {}).get("slug") for s in sources), None)
        return triggers(legs, every_pair, listings, subject_id, slug, now)

    def safe(subject_id: int) -> list[tuple[str, dict, str | None]]:
        # One match the registry can't answer for must not hold back every other match's triggers.
        try:
            return one(subject_id)
        except Exception:
            logger.exception("subject %d: scan failed; its triggers wait for the next scan", subject_id)
            return []

    with ThreadPoolExecutor(WORKERS) as pool:
        return [t for found in pool.map(safe, sorted(by_subject)) for t in found]


def handler(event, context):
    now = datetime.now(timezone.utc)
    found = scan(RegistryClient(), now)
    queues = {"launch": os.environ["LAUNCHER_QUEUE_URL"], "shutdown": os.environ["SHUTDOWN_QUEUE_URL"]}
    sqs = boto3.client("sqs")
    for action, data, deadline in found:
        send_trigger(sqs, queues[action], RULE_TYPE, action, data, now.isoformat(), deadline)
    counts = {a: sum(1 for x, _, _ in found if x == a) for a in ("launch", "shutdown")}
    logger.info("esports game scan: %s", counts)
    return counts
