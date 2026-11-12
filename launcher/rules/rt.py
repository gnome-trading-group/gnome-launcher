"""
One session per film with Rotten Tomatoes markets, holding every venue's strikes, triggered by the rt_scanner Lambda
from the rt_films table gnome-pipelines' rt_reviews source writes.

The trigger carries identifiers only: the film and, per strike on each venue, the exchange security ids of its YES and
NO sides as the venue names them. The session reads the film's reviews from rt_reviews itself. Keeping the trigger the
same for as long as the venues list the same strikes is what lets the launcher's dedup and its active-session check
recognise every later scan as a repeat.

A rule trades the venues it names; the session gets each strike of those venues whose two sides are in the registry
(a strike missing either side is skipped). The strategy (rt_review_wave) waits for the film's first review wave,
trades its window and ends itself, so a film whose session has stopped is never launched again: the wave it waited
for has passed. A session that failed is relaunched by a later scan. Polymarket International sits in eu-west-1,
Kalshi and Polymarket US in us-east-1; the session is placed in HOME_VENUE's region whenever it trades there, and the
other venues' books reach it over the platform's region peering.
"""
from __future__ import annotations

import logging

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

VENUES = ("kalshi", "polymarket", "polymarket_us")
# The registry's exchange_name of each venue.
EXCHANGE_NAMES = {"kalshi": "Kalshi", "polymarket": "Polymarket (International)", "polymarket_us": "Polymarket (US)"}
# Most of the money the research found to take was on Kalshi.
HOME_VENUE = "kalshi"
STOPPED = "STOPPED"
SHUTDOWN_REASONS = {"closed": "no longer has open markets", "past_close": "is past its markets' close"}
RT_URL = "https://www.rottentomatoes.com/m/{slug}"


def _listings(legs: list[dict]) -> list[int]:
    return [int(leg[key]) for leg in legs for key in ("yes_listing", "no_listing")]


def _region(legs: list[dict], ctx: RuleContext) -> str | None:
    """HOME_VENUE's exchange region when the session trades it; otherwise the registry places it by its listings."""
    home = next((leg for leg in legs if leg["venue"] == HOME_VENUE), None)
    if home is None:
        return None
    listing = ctx.registry.get_listing(listing_id=int(home["yes_listing"]))[0]
    exchanges = ctx.registry.get_exchange(exchange_id=listing.exchange_id)
    if not exchanges or not getattr(exchanges[0], "region", None):
        raise ValueError(f"exchange {listing.exchange_id} of listing {home['yes_listing']} has no region")
    return exchanges[0].region


def _listing_id(ctx: RuleContext, exchange_id: int, esid: str) -> int | None:
    found = ctx.registry.get_listing(exchange_id=exchange_id, exchange_security_id=esid)
    return int(found[0].listing_id) if found else None


@register_rule_type
class RtRule(RuleType):
    type = "rt"
    display_name = "Rotten Tomatoes film"
    deadline_label = "launch cutoff"

    data_schema = {
        "type": "object",
        "properties": {
            "film_id": {"type": "string", "description": "the film, as rt_films names it"},
            "title": {"type": "string"},
            "rt_slug": {"type": ["string", "null"]},
            "markets": {
                "type": "array",
                "description": "every venue's \"at least X\" strikes, by the exchange security ids of their two sides",
                "items": {
                    "type": "object",
                    "properties": {
                        "venue": {"type": "string", "enum": list(VENUES)},
                        "strike": {"type": "integer"},
                        "yes": {"type": "string"},
                        "no": {"type": "string"},
                    },
                    "required": ["venue", "strike", "yes", "no"],
                },
            },
            "reason": {"type": "string", "enum": list(SHUTDOWN_REASONS),
                       "description": "shutdown triggers: the venues no longer list the film, or its close has passed"},
        },
        "required": ["film_id", "markets"],
    }

    parameter_schema = {
        "type": "object",
        "properties": {
            "venues": {"type": "array", "items": {"type": "string", "enum": list(VENUES)},
                       "default": list(VENUES), "description": "Venues to launch on"},
            **STRATEGY_PARAMS_SCHEMA,
        },
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
        venues = params.get("venues", VENUES)
        exchange_ids: dict[str, int | None] = {}
        legs, skipped = [], []
        for m in data["markets"]:
            venue = m["venue"]
            if venue not in venues:
                continue
            if venue not in exchange_ids:
                found = ctx.registry.get_exchange(exchange_name=EXCHANGE_NAMES[venue])
                exchange_ids[venue] = int(found[0].exchange_id) if found else None
            if exchange_ids[venue] is None:
                skipped.append(f"{venue} (no {EXCHANGE_NAMES[venue]} exchange in the registry)")
                continue
            yes, no = (_listing_id(ctx, exchange_ids[venue], m[side]) for side in ("yes", "no"))
            if yes is None or no is None:
                skipped.append(f"{venue} >={m['strike']}")
                continue
            legs.append({"venue": venue, "strike": int(m["strike"]), "yes_listing": yes, "no_listing": no})
        if not legs:
            return NoMatch("none of this rule's venues has the film's strikes in the registry"
                           + (f": skipped {', '.join(skipped)}" if skipped else ""))
        stopped = [s.session_id for s in ctx.registry.get_strategy_sessions(strategy_id=params["strategy_id"],
                                                                             status=STOPPED) or []
                   if listing_ids(s.config.get("listings", [])) & set(_listings(legs))]
        if stopped:
            return NoMatch(f"session {stopped[0]} of this strategy already traded the film's wave and stopped")

        config = ResolvedStrategyConfig.from_params(params, listings=_listings(legs))
        config.strategy_args = {**config.strategy_args, "film_id": data["film_id"], "legs": legs}
        config.region = _region(legs, ctx)
        reason = (f"RT film {data.get('title') or data['film_id']}: {len(legs)} strikes on "
                  f"{', '.join(sorted({leg['venue'] for leg in legs}))}")
        if skipped:
            reason += f"; skipped {', '.join(skipped)} (not in the registry)"
        return RuleEvaluation(should_launch=True, resolved_config=config, reason=reason)

    def links(self, data: dict) -> list[dict]:
        if not data.get("rt_slug"):
            return []
        return [{"label": f"{data.get('title') or data['film_id']} on Rotten Tomatoes",
                 "url": RT_URL.format(slug=data["rt_slug"])}]

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | NoMatch:
        running = ctx.registry.get_strategy_sessions(strategy_id=params["strategy_id"], status=ACTIVE_SESSION_STATUSES)
        # By film rather than by listing: a closed film's trigger may no longer list the strikes its session holds.
        targets = [s.session_id for s in running or [] if s.config.get("strategy.args.film_id") == data["film_id"]]
        if not targets:
            return NoMatch("no running session of the strategy trades this film")
        return ShutdownEvaluation(should_shutdown=True, target_session_ids=targets,
                                  reason=f"RT film {data.get('title') or data['film_id']} "
                                         + SHUTDOWN_REASONS.get(data.get("reason"), "has closed"))
