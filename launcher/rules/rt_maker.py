"""
Resting maker sessions on the Kalshi Rotten Tomatoes markets of every open film, quoting their strikes for Kalshi's
liquidity rewards (gnomepy-research's rt_maker), triggered by the rt_scanner Lambda from rt_films like the `rt` rule.

The scanner packs the open films into groups small enough for one paper gateway, and sends a launch trigger per group:
its number and its films, each with its close and its Kalshi strikes. The session holds and leases only each strike's
YES listing: it quotes both sides there, and Kalshi books a YES ask as the NO bid the rewards score. Its args give each
film as {film_id, close, strikes: {strike: YES listing id}}, nothing more: a session's whole config travels in its
host's EC2 user data (16 KB), and the `rt` rule's legs (venue and both listings per strike) for a full group would not
fit.

A session's listings are fixed at its launch, so a group whose films or strikes change needs a new session. Once a day,
in the scanner's relaunch hour, it sends every group as a shutdown trigger: a session of the strategy holding anything
but one of those groups is stopped, and the next scan's launch for the group gets past the active-session check once
it has. Outside that hour a changed group waits (its launch is blocked by the session still holding its listings), so
a film opening costs every film's resting orders once a day at most; a film that closed is no longer quoted by the
session meanwhile. A relaunch keeps the films' positions, which the session reads from the ledger by strategy.

The session never ends itself, so one that stopped any other way (failed, or stopped by a person) is relaunched by a
later scan while the scanner still sends its group; disable the rule to stop quoting.
"""
from __future__ import annotations

from launcher.rules.rt import HOME_VENUE, RT_URL, RtRule, home_region, resolve_films
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

FILM_SCHEMA = {
    "type": "object",
    "properties": {
        "film_id": {"type": "string", "description": "the film, as rt_films names it"},
        "title": {"type": "string"},
        "rt_slug": {"type": ["string", "null"]},
        "close": {"type": ["string", "null"], "description": "its markets' close, ISO-8601"},
        "markets": RtRule.data_schema["properties"]["markets"],
    },
    "required": ["film_id", "markets"],
}


def _title(film: dict) -> str:
    return film.get("title") or film["film_id"]


@register_rule_type
class RtMakerRule(RuleType):
    type = "rt_maker"
    display_name = "Rotten Tomatoes films (maker)"

    data_schema = {
        "type": "object",
        "properties": {
            "group": {"type": "integer", "minimum": 0, "description": "launch triggers: the group's number"},
            "films": {"type": "array", "items": FILM_SCHEMA, "minItems": 1,
                      "description": "launch triggers: the group's films"},
            "groups": {"type": "array", "items": {"type": "array", "items": FILM_SCHEMA},
                       "description": "relaunch (shutdown) triggers: every group's films"},
        },
        "oneOf": [{"required": ["group", "films"]}, {"required": ["groups"]}],
    }

    parameter_schema = {
        "type": "object",
        "properties": {**STRATEGY_PARAMS_SCHEMA},
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
        films, notes = [], []
        for film, (legs, skipped) in zip(data["films"], resolve_films(data["films"], (HOME_VENUE,), ctx)):
            if legs:
                films.append({"film_id": film["film_id"], "close": film.get("close"),
                              "strikes": {str(leg["strike"]): leg["yes_listing"] for leg in legs}})
            else:
                notes.append(f"{_title(film)} has no {HOME_VENUE} strike in the registry")
            if skipped:
                notes.append(f"{_title(film)}: skipped {', '.join(skipped)} (not in the registry)")
        if not films:
            return NoMatch(f"none of group {data['group']}'s films has a {HOME_VENUE} strike in the registry"
                           + (f": {'; '.join(notes)}" if notes else ""))
        listings = [listing for film in films for listing in film["strikes"].values()]
        config = ResolvedStrategyConfig.from_params(params, listings=listings)
        config.strategy_args = {**config.strategy_args, "films": films}
        config.region = home_region([{"venue": HOME_VENUE, "yes_listing": listings[0]}], ctx)
        reason = f"RT maker group {data['group']}: {len(films)} films, {len(listings)} {HOME_VENUE} strikes"
        if notes:
            reason += f"; {'; '.join(notes)}"
        return RuleEvaluation(should_launch=True, resolved_config=config, reason=reason)

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | NoMatch:
        running = ctx.registry.get_strategy_sessions(strategy_id=params["strategy_id"], status=ACTIVE_SESSION_STATUSES)
        if not running:
            return NoMatch("no running session of the strategy")
        films = [film for group in data["groups"] for film in group]
        resolved = resolve_films(films, (HOME_VENUE,), ctx)
        wanted, first = [], 0
        for group in data["groups"]:
            wanted.append(frozenset(leg["yes_listing"] for legs, _ in resolved[first:first + len(group)] for leg in legs))
            first += len(group)
        stale = [s.session_id for s in running if frozenset(listing_ids(s.config.get("listings", []))) not in wanted]
        if not stale:
            return NoMatch("every session holds one group's films as they are")
        reason = ("RT maker: no film is open any more" if not films
                  else f"RT maker relaunch: {len(stale)} session(s) hold films or strikes that have since changed")
        return ShutdownEvaluation(should_shutdown=True, target_session_ids=stale, reason=reason)

    def links(self, data: dict) -> list[dict]:
        films = data.get("films") or [film for group in data.get("groups", []) for film in group]
        return [{"label": f"{_title(film)} on Rotten Tomatoes", "url": RT_URL.format(slug=film["rt_slug"])}
                for film in films if film.get("rt_slug")]
