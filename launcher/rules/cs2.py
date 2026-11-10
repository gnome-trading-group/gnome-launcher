"""
One session per CS2 match, holding every venue's listings for it, triggered by the cs2_scanner Lambda from
gnome-pipelines' cs2_upcoming_matches.

The trigger carries identifiers only: the HLTV match, which market, and per venue its two team listings.
Kickoff, lineups and prices are not in it; the session reads them from the cs2_upcoming tables as they change.
Keeping the trigger the same for the life of a match is what lets the launcher's dedup and its active-session
check recognise every later scan as a repeat.

A rule trades one market (series now; map markets are a later strategy) on the venues it names; the session
gets the legs of those venues whose listings are in the registry. It pays each venue's taker fee on that venue's
listings, and in paper mode fills each listing as its exchange is recorded in the registry, under any of the
rule's simulation overrides for that exchange. A rule whose strategy trades the match while it is played sets
stop_past_kickoff false, and its sessions then stop only once the match is finished or gone; with launch_live it
is also launched on a match already being played (phase "live" triggers), which no other rule acts on. Venues sit in different regions (Polymarket International in
eu-west-1, Kalshi and Polymarket US in us-east-1) and the registry places a session only by a region it is given
or one its listings share, so the session is placed in HOME_VENUE's region whenever it trades there.
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
# The session prices off this venue's mid (cs2_series blends with it), so it runs where this venue is collected and
# traded; the other venues' books reach it over the platform's region peering.
HOME_VENUE = "polymarket"
# HLTV ignores the slug but needs one: /matches/<id>/ alone serves the home page.
HLTV_MATCH_URL = "https://www.hltv.org/matches/{match_id}/match"
MARKETS = ("series", "game1")
SHUTDOWN_REASONS = {"finished": "is over", "gone": "has left HLTV's listing",
                    "past_kickoff": "started over an hour ago"}
DEFAULT_MARKET = "series"


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


@register_rule_type
class CS2Rule(RuleType):
    type = "cs2"
    display_name = "CS2 match"
    deadline_label = "launch cutoff"

    data_schema = {
        "type": "object",
        "properties": {
            "match_id": {"type": "integer", "description": "HLTV match id"},
            "market": {"type": "string", "enum": list(MARKETS)},
            "legs": {
                "type": "array",
                "description": "per venue with the market, its listings for each HLTV team winning",
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
            "phase": {"type": "string", "enum": ["live"],
                      "description": "launch triggers: the match is being played (absent: before kickoff)"},
            "reason": {"type": "string", "enum": ["finished", "gone", "past_kickoff"],
                       "description": "shutdown triggers: a venue paid out the series, the match left HLTV's "
                                      "listing, or kickoff was over an hour ago"},
        },
        "required": ["match_id", "market", "legs"],
    }

    parameter_schema = {
        "type": "object",
        "properties": {
            "market": {"type": "string", "enum": list(MARKETS), "default": DEFAULT_MARKET,
                       "description": "The market this rule's strategy trades"},
            "venues": {"type": "array", "items": {"type": "string", "enum": list(VENUES)},
                       "default": list(VENUES), "description": "Venues to launch on"},
            "launch_live": {"type": "boolean", "default": False,
                            "description": "Also launch on matches already being played; needs stop_past_kickoff "
                                           "false, or a session launched an hour after kickoff is stopped at once"},
            "stop_past_kickoff": {"type": "boolean", "default": True,
                                  "description": "Stop sessions an hour after kickoff; false keeps them until the "
                                                 "match is over, for strategies that trade it while it is played"},
            **STRATEGY_PARAMS_SCHEMA,
        },
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def validate_parameters(self, params: dict) -> list[str]:
        if params.get("launch_live") and params.get("stop_past_kickoff", True):
            return ["launch_live needs stop_past_kickoff false: its sessions would be stopped as they launch"]
        return []

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
        if data.get("phase") == "live" and not params.get("launch_live", False):
            return NoMatch("this rule launches before kickoff only, and the match is being played")
        market = data["market"]
        if market != params.get("market", DEFAULT_MARKET):
            return NoMatch(f"this rule trades the {params.get('market', DEFAULT_MARKET)} market, not {market}")
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
        if not legs:
            return NoMatch("no venue of this rule has the match's listings in the registry"
                           + (f": {', '.join(skipped)}" if skipped else ""))

        config = ResolvedStrategyConfig.from_params(params, listings=_listings(legs))
        config.strategy_args = {**config.strategy_args, "match_id": int(data["match_id"]), "legs": legs}
        config.region = _region(legs, ctx)
        reason = f"CS2 match {data['match_id']} {market} on {', '.join(leg['venue'] for leg in legs)}"
        if skipped:
            reason += f"; skipped {', '.join(skipped)}"
        return RuleEvaluation(should_launch=True, resolved_config=config, reason=reason)

    def links(self, data: dict) -> list[dict]:
        return [{"label": f"HLTV match {data['match_id']}", "url": HLTV_MATCH_URL.format(match_id=int(data["match_id"]))}]

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | NoMatch:
        if data["market"] != params.get("market", DEFAULT_MARKET):
            return NoMatch(f"this rule trades the {params.get('market', DEFAULT_MARKET)} market, not {data['market']}")
        if data.get("reason") == "past_kickoff" and not params.get("stop_past_kickoff", True):
            return NoMatch("this rule's sessions trade the match while it is played; they stop once it is over")
        strategy_id = params["strategy_id"]
        running = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status=ACTIVE_SESSION_STATUSES)
        listings = set(_listings(data["legs"]))
        targets = [s.session_id for s in running or [] if listing_ids(s.config.get("listings", [])) & listings]
        if not targets:
            return NoMatch("no running session of the strategy holds these listings")
        return ShutdownEvaluation(should_shutdown=True, target_session_ids=targets,
                                  reason=f"CS2 match {data['match_id']} " + SHUTDOWN_REASONS.get(
                                      data.get("reason"), "has started or gone"))
