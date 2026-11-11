"""
One session per esports match (CS2, Dota 2, Valorant), holding every venue's two team listings for it, triggered by
the game_scanner Lambda from the registry: the classifier joins each match across venues into one proposition
subject, and the scanner picks each venue's pair of team contracts by the relationships stored between them.

The trigger carries identifiers only (league, subject, the legs' events, each venue's two team listings, and how the
session finds the match's live state), so it stays the same for the life of a match and the launcher's dedup and
active-session check recognise every later scan as a repeat. The scheduled start is read from the registry when the
trigger is evaluated: venues move a match, and a moved start must move the launch, not make a new trigger.

The scanner sends a match's trigger from LOOKAHEAD before its start; the rule launches once the anchor venue's start
(the venue whose live feed the session follows) is within its lead_minutes: another venue's placeholder start could
otherwise launch it hours early and make it give up before play. It doesn't launch again on listings a session of its
strategy has already been stopped on: a session ends itself once the match is over, and the match stays unresolved
(and the trigger keeps coming) for a while after. A failed session is relaunched. The session is launched before play, not once it is live: it watches the match itself (Polymarket's
sports websocket, or Kalshi's live data) and trades nothing until play has begun, since a match that is never played
settles differently by venue. It stops when a venue resolves the match, or RUN_FOR after the scheduled start.

Venues sit in different regions (Polymarket International in eu-west-1, Kalshi and Polymarket US in us-east-1) and
the registry places a session only by a region it is given or one its listings share, so the session is placed in
HOME_VENUE's region whenever it trades there.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    NoMatch,
    ResolvedStrategyConfig,
    RuleContext,
    RuleEvaluation,
    RuleType,
    ShutdownEvaluation,
    register_rule_type,
)
from launcher.sessions import ACTIVE_SESSION_STATUSES, listing_ids

logger = logging.getLogger(__name__)

LEAGUES = ("cs2", "dota2", "valorant")
VENUES = ("polymarket", "kalshi", "polymarket_us")
MARKETS = ("match",)
HOME_VENUE = "polymarket"
STOPPED = "STOPPED"
DEFAULT_LEAD_MINUTES = 10
POLYMARKET_EVENT_URL = "https://polymarket.com/event/{slug}"
KALSHI_EVENT_URL = "https://kalshi.com/markets/{series}/{ticker}"
SHUTDOWN_REASONS = {"resolved": "was resolved by a venue", "past_start": "started too long ago"}


def _listings(legs: list[dict]) -> list[int]:
    return [int(leg[key]) for leg in legs for key in ("listing_team_a", "listing_team_b")]


def _region(legs: list[dict], ctx: RuleContext) -> str | None:
    """HOME_VENUE's exchange region when the session trades it; otherwise the registry places it by its listings."""
    home = next((leg for leg in legs if leg["venue"] == HOME_VENUE), None)
    if home is None:
        return None
    listing = ctx.registry.get_listing(listing_id=int(home["listing_team_a"]))[0]
    exchanges = ctx.registry.get_exchange(exchange_id=listing.exchange_id)
    if not exchanges or not getattr(exchanges[0], "region", None):
        raise ValueError(f"exchange {listing.exchange_id} of listing {home['listing_team_a']} has no region")
    return exchanges[0].region


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def scheduled_start(event_id: int, ctx: RuleContext) -> datetime | None:
    """The anchor event's scheduled start, as the registry has it now."""
    starts = [t for e in ctx.registry.get_event(event_ids=[int(event_id)]) or []
              if (t := _parse_time(getattr(e, "start_time", None))) is not None]
    return starts[0] if starts else None


@register_rule_type
class EsportsGameRule(RuleType):
    type = "esports_game"
    display_name = "Esports match"
    deadline_label = "end of the launch window"

    data_schema = {
        "type": "object",
        "properties": {
            "league": {"type": "string", "enum": list(LEAGUES)},
            "subject_id": {"type": "integer", "description": "the match's canonical proposition subject"},
            "market": {"type": "string", "enum": list(MARKETS)},
            "anchor_event_id": {"type": "integer",
                                "description": "the anchor venue's event, whose scheduled start the launch follows"},
            "event_ids": {"type": "array", "items": {"type": "integer"}, "description": "the legs' events"},
            "polymarket_slug": {"type": "string",
                                "description": "Polymarket's event slug, whose gameId the session follows live"},
            "kalshi_event_ticker": {"type": "string",
                                    "description": "Kalshi's event ticker, whose milestone the session follows live"},
            "legs": {
                "type": "array",
                "description": "per venue, its listings for each team winning; team A is the same team on every venue",
                "items": {
                    "type": "object",
                    "properties": {
                        "venue": {"type": "string", "enum": list(VENUES)},
                        "listing_team_a": {"type": "integer"},
                        "listing_team_b": {"type": "integer"},
                    },
                    "required": ["venue", "listing_team_a", "listing_team_b"],
                },
            },
            "reason": {"type": "string", "enum": list(SHUTDOWN_REASONS),
                       "description": "shutdown triggers: a venue resolved the match, or it started too long ago"},
        },
        "required": ["league", "subject_id", "market", "anchor_event_id", "event_ids", "legs"],
    }

    parameter_schema = {
        "type": "object",
        "properties": {
            "leagues": {"type": "array", "items": {"type": "string", "enum": list(LEAGUES)},
                        "default": list(LEAGUES), "description": "Leagues to launch on"},
            "market": {"type": "string", "enum": list(MARKETS), "default": "match",
                       "description": "The market this rule's strategy trades"},
            "venues": {"type": "array", "items": {"type": "string", "enum": list(VENUES)},
                       "default": list(VENUES), "description": "Venues to launch on"},
            "lead_minutes": {"type": "integer", "minimum": 0, "maximum": 30, "default": DEFAULT_LEAD_MINUTES,
                             "description": "Launch this long before the scheduled start (the scanner looks 30 "
                                            "minutes ahead); the session waits for play before trading"},
            **STRATEGY_PARAMS_SCHEMA,
        },
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext, now: datetime | None = None) \
            -> RuleEvaluation | NoMatch:
        league = data["league"]
        if league not in params.get("leagues", LEAGUES):
            return NoMatch(f"this rule launches on {', '.join(params.get('leagues', LEAGUES))}, not {league}")
        market = data["market"]
        if market != params.get("market", "match"):
            return NoMatch(f"this rule trades the {params.get('market', 'match')} market, not {market}")
        start = scheduled_start(data["anchor_event_id"], ctx)
        if start is None:
            return NoMatch("the match's anchor event has no scheduled start")
        lead = timedelta(minutes=params.get("lead_minutes", DEFAULT_LEAD_MINUTES))
        now = now or datetime.now(timezone.utc)
        if start - now > lead:
            return NoMatch(f"the match starts at {start:%H:%M} UTC; this rule launches {lead.seconds // 60} minutes "
                           "before")
        venues = params.get("venues", VENUES)
        legs, skipped = [], []
        for leg in data["legs"]:
            if leg["venue"] not in venues:
                continue
            missing = [lid for lid in _listings([leg]) if not ctx.registry.get_listing(listing_id=lid)]
            if missing:
                skipped.append(f"{leg['venue']} (listings {missing} aren't in the registry)")
                continue
            legs.append({"venue": leg["venue"], "listing_team_a": int(leg["listing_team_a"]),
                         "listing_team_b": int(leg["listing_team_b"])})
        if len(legs) < 2:
            return NoMatch(f"fewer than two of this rule's venues have the match's listings registered"
                           + (f": skipped {', '.join(skipped)}" if skipped else ""))
        stopped = [x.session_id for x in ctx.registry.get_strategy_sessions(strategy_id=params["strategy_id"],
                                                                             status=STOPPED) or []
                   if listing_ids(x.config.get("listings", [])) & set(_listings(legs))]
        if stopped:
            return NoMatch(f"session {stopped[0]} of this strategy already ran on the match and was stopped")

        config = ResolvedStrategyConfig.from_params(params, listings=_listings(legs))
        config.strategy_args = {
            **config.strategy_args, "legs": legs, "league": league, "subject_id": int(data["subject_id"]),
            "scheduled_start": start.isoformat(),
            **{k: data[k] for k in ("polymarket_slug", "kalshi_event_ticker") if data.get(k)},
        }
        config.region = _region(legs, ctx)
        reason = f"{league} match (subject {data['subject_id']}) {market} on {', '.join(leg['venue'] for leg in legs)}"
        if skipped:
            reason += f"; skipped {', '.join(skipped)}"
        return RuleEvaluation(should_launch=True, resolved_config=config, reason=reason)

    def links(self, data: dict) -> list[dict]:
        out = []
        if data.get("polymarket_slug"):
            out.append({"label": "Polymarket event",
                        "url": POLYMARKET_EVENT_URL.format(slug=data["polymarket_slug"])})
        if data.get("kalshi_event_ticker"):
            ticker = data["kalshi_event_ticker"]
            out.append({"label": "Kalshi event",
                        "url": KALSHI_EVENT_URL.format(series=ticker.split("-")[0].lower(), ticker=ticker.lower())})
        return out

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | NoMatch:
        if data["league"] not in params.get("leagues", LEAGUES):
            return NoMatch(f"this rule launches on {', '.join(params.get('leagues', LEAGUES))}, not {data['league']}")
        if data["market"] != params.get("market", "match"):
            return NoMatch(f"this rule trades the {params.get('market', 'match')} market, not {data['market']}")
        running = ctx.registry.get_strategy_sessions(strategy_id=params["strategy_id"], status=ACTIVE_SESSION_STATUSES)
        listings = set(_listings(data["legs"]))
        targets = [s.session_id for s in running or [] if listing_ids(s.config.get("listings", [])) & listings]
        if not targets:
            return NoMatch("no running session of the strategy holds these listings")
        return ShutdownEvaluation(should_shutdown=True, target_session_ids=targets,
                                  reason=f"{data['league']} match (subject {data['subject_id']}) "
                                         + SHUTDOWN_REASONS.get(data.get("reason"), "is over"))
