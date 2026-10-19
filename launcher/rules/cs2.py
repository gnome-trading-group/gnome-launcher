"""
One session per CS2 match per venue, triggered by the cs2_scanner Lambda from gnome-pipelines'
cs2_upcoming_matches.

The trigger carries identifiers only: the HLTV match, the venue, which market and its two team
listings. Kickoff, lineups and prices are not in it; the session reads them from the cs2_upcoming
tables as they change. Keeping the trigger the same for the life of a match is what lets the
launcher's dedup and its active-session check recognise every later scan as a repeat.

A rule trades one market (series now; map markets are a later strategy). Each venue's session
runs in that venue's region (the registry places a session by its listings' exchange), pays that
venue's taker fee, and in paper mode fills as the venue's exchange is recorded in the registry,
under any of the rule's simulation overrides for that exchange.
"""
from __future__ import annotations

import logging

from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    ResolvedStrategyConfig,
    RuleContext,
    RuleEvaluation,
    NoMatch,
    RuleType,
    ShutdownEvaluation,
    register_rule_type,
)
from launcher.sessions import ACTIVE_SESSION_STATUSES, listing_ids

logger = logging.getLogger(__name__)

VENUES = ("polymarket", "kalshi", "polymarket_us")
# HLTV ignores the slug but needs one: /matches/<id>/ alone serves the home page.
HLTV_MATCH_URL = "https://www.hltv.org/matches/{match_id}/match"
MARKETS = ("series", "game1")
DEFAULT_MARKET = "series"


def _listings(data: dict) -> list[int]:
    return [int(data["listing_team_a"]), int(data["listing_team_b"])]


@register_rule_type
class CS2Rule(RuleType):
    type = "cs2"
    display_name = "CS2 match"
    deadline_label = "kickoff"

    data_schema = {
        "type": "object",
        "properties": {
            "match_id": {"type": "integer", "description": "HLTV match id"},
            "venue": {"type": "string", "enum": list(VENUES)},
            "market": {"type": "string", "enum": list(MARKETS)},
            "listing_team_a": {"type": "integer", "description": "the venue's listing for HLTV team A winning"},
            "listing_team_b": {"type": "integer", "description": "the venue's listing for HLTV team B winning"},
        },
        "required": ["match_id", "venue", "market", "listing_team_a", "listing_team_b"],
    }

    parameter_schema = {
        "type": "object",
        "properties": {
            "market": {"type": "string", "enum": list(MARKETS), "default": DEFAULT_MARKET,
                       "description": "The market this rule's strategy trades"},
            "venues": {"type": "array", "items": {"type": "string", "enum": list(VENUES)},
                       "default": list(VENUES), "description": "Venues to launch on"},
            **STRATEGY_PARAMS_SCHEMA,
        },
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
        venue, market = data["venue"], data["market"]
        if market != params.get("market", DEFAULT_MARKET):
            return NoMatch(f"this rule trades the {params.get('market', DEFAULT_MARKET)} market, not {market}")
        if venue not in params.get("venues", VENUES):
            return NoMatch(f"{venue} isn't one of this rule's venues")
        listings = _listings(data)
        missing = [lid for lid in listings if not ctx.registry.get_listing(listing_id=lid)]
        if missing:
            return NoMatch(f"listings {missing} aren't in the registry")

        config = ResolvedStrategyConfig.from_params(params, listings=listings)
        config.strategy_args = {
            **config.strategy_args,
            "match_id": int(data["match_id"]),
            "listing_team_a": listings[0],
            "listing_team_b": listings[1],
        }
        return RuleEvaluation(should_launch=True, resolved_config=config,
                              reason=f"CS2 match {data['match_id']} {market} on {venue}")

    def links(self, data: dict) -> list[dict]:
        return [{"label": f"HLTV match {data['match_id']}", "url": HLTV_MATCH_URL.format(match_id=int(data["match_id"]))}]

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | NoMatch:
        if data["market"] != params.get("market", DEFAULT_MARKET):
            return NoMatch(f"this rule trades the {params.get('market', DEFAULT_MARKET)} market, not {data['market']}")
        strategy_id = params["strategy_id"]
        running = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status=ACTIVE_SESSION_STATUSES)
        listings = set(_listings(data))
        targets = [s.session_id for s in running or [] if listing_ids(s.config.get("listings", [])) & listings]
        if not targets:
            return NoMatch("no running session of the strategy holds these listings")
        return ShutdownEvaluation(should_shutdown=True, target_session_ids=targets,
                                  reason=f"CS2 match {data['match_id']} on {data['venue']} has started or gone")
