"""
One resting maker session per film with Kalshi Rotten Tomatoes markets, quoting its strikes for Kalshi's liquidity
rewards (gnomepy-research's rt_maker), triggered by the rt_scanner Lambda from rt_films like the `rt` rule.

The trigger is the `rt` rule's data, holding Kalshi's strikes only. The session gets each strike's YES and NO listing
ids as legs, the same shape the `rt` rule gives rt_review_wave, but holds and leases only the YES listings: it quotes
both sides there, and Kalshi books a YES ask as the NO bid the rewards score. The session never ends itself, so any
session of the film that stopped (failed, or stopped by a person or the launcher) is relaunched by a later scan while
the scanner still sends the film; disable the rule to stop quoting. Shutdown is the `rt` rule's, by film.
"""
from __future__ import annotations

from launcher.rules.rt import HOME_VENUE, RtRule, home_region, resolve_legs
from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    NoMatch,
    ResolvedStrategyConfig,
    RuleContext,
    RuleEvaluation,
    register_rule_type,
)


@register_rule_type
class RtMakerRule(RtRule):
    type = "rt_maker"
    display_name = "Rotten Tomatoes film (maker)"

    parameter_schema = {
        "type": "object",
        "properties": {**STRATEGY_PARAMS_SCHEMA},
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
        legs, skipped = resolve_legs(data["markets"], (HOME_VENUE,), ctx)
        if not legs:
            return NoMatch(f"none of the film's {HOME_VENUE} strikes is in the registry"
                           + (f": skipped {', '.join(skipped)}" if skipped else ""))
        config = ResolvedStrategyConfig.from_params(params, listings=[leg["yes_listing"] for leg in legs])
        config.strategy_args = {**config.strategy_args, "film_id": data["film_id"], "legs": legs}
        config.region = home_region(legs, ctx)
        reason = f"RT film {data.get('title') or data['film_id']}: quoting {len(legs)} {HOME_VENUE} strikes"
        if skipped:
            reason += f"; skipped {', '.join(skipped)} (not in the registry)"
        return RuleEvaluation(should_launch=True, resolved_config=config, reason=reason)
