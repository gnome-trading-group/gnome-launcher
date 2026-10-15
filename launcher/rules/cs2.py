"""
One session per CS2 match per venue, triggered by the cs2_scanner Lambda from gnome-pipelines'
cs2_upcoming_matches.

The trigger carries identifiers only: the HLTV match, the venue, which market and its two team
listings. Kickoff, lineups and prices are not in it; the session reads them from the cs2_upcoming
tables as they change. Keeping the trigger the same for the life of a match is what lets the
launcher's dedup and its active-session check recognise every later scan as a repeat.

A rule trades one market (series now; map markets are a later strategy). Each venue's session
runs in that venue's region (the registry places a session by its listings' exchange), pays that
venue's taker fee, and in paper mode fills against that venue's simulation profile, which the
rule's simulation_config must define.
"""
from __future__ import annotations

import logging

from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    ResolvedStrategyConfig,
    RuleContext,
    RuleEvaluation,
    RuleType,
    ShutdownEvaluation,
    register_rule_type,
)
from launcher.sessions import ACTIVE_SESSION_STATUSES, listing_ids

logger = logging.getLogger(__name__)

VENUES = ("polymarket", "kalshi", "polymarket_us")
MARKETS = ("series", "game1")
DEFAULT_MARKET = "series"
# Taker fee rates (fee per share = rate * p * (1 - p)), as in gnomepy-research's profiles. They belong on the
# registry's listing specs eventually; until then a rule can override them with fee_rates.
DEFAULT_FEE_RATES = {"polymarket": 0.07, "kalshi": 0.07, "polymarket_us": 0.0695}
DEFAULT_PROFILES = {"polymarket": "polymarket_intl", "kalshi": "kalshi", "polymarket_us": "polymarket_us"}


def _listings(data: dict) -> list[int]:
    return [int(data["listing_team_a"]), int(data["listing_team_b"])]


@register_rule_type
class CS2Rule(RuleType):
    type = "cs2"
    display_name = "CS2 match"

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
            "fee_rates": {"type": "object", "additionalProperties": {"type": "number"},
                          "description": "Taker fee rate per venue, over the defaults"},
            "simulation_profiles": {"type": "object", "additionalProperties": {"type": "string"},
                                    "description": "Paper-mode simulation profile per venue, over the defaults; "
                                                   "each must be defined in simulation_config"},
            **STRATEGY_PARAMS_SCHEMA,
        },
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | None:
        venue, market = data["venue"], data["market"]
        if market != params.get("market", DEFAULT_MARKET) or venue not in params.get("venues", VENUES):
            return None
        listings = _listings(data)
        missing = [lid for lid in listings if not ctx.registry.get_listing(listing_id=lid)]
        if missing:
            logger.warning("match %s on %s: listings %s not in the registry - not launching",
                           data["match_id"], venue, missing)
            return None

        config = ResolvedStrategyConfig.from_params(params, listings=listings)
        config.strategy_args = {
            **config.strategy_args,
            "match_id": int(data["match_id"]),
            "listing_team_a": listings[0],
            "listing_team_b": listings[1],
            "fee_rate": {**DEFAULT_FEE_RATES, **params.get("fee_rates", {})}[venue],
        }
        if config.mode == "paper":
            profile = {**DEFAULT_PROFILES, **params.get("simulation_profiles", {})}[venue]
            if not any(k.startswith(f"simulation.profiles.{profile}.") for k in config.simulation_config):
                logger.error("match %s on %s: simulation profile %r isn't defined in the rule's simulation_config",
                             data["match_id"], venue, profile)
                return None
            config.simulation_config = {
                **config.simulation_config,
                **{f"simulation.listing.{lid}.profile": profile for lid in listings},
            }
        return RuleEvaluation(should_launch=True, resolved_config=config,
                              reason=f"CS2 match {data['match_id']} {market} on {venue}")

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | None:
        strategy_id = params.get("strategy_id")
        if not strategy_id or data["market"] != params.get("market", DEFAULT_MARKET):
            return None
        running = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status=ACTIVE_SESSION_STATUSES)
        listings = set(_listings(data))
        targets = [s.session_id for s in running or [] if listing_ids(s.config.get("listings", [])) & listings]
        if not targets:
            return None
        return ShutdownEvaluation(should_shutdown=True, target_session_ids=targets,
                                  reason=f"CS2 match {data['match_id']} on {data['venue']} has started or gone")
