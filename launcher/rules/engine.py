import logging
from datetime import datetime, timedelta, timezone

from launcher.models import LaunchRule
from launcher.rules.types import RULE_TYPE_REGISTRY, NoMatch, RuleContext, RuleEvaluation
from launcher.sessions import ACTIVE_SESSION_STATUSES

logger = logging.getLogger(__name__)

# What counts as the rule having launched, for its cooldown: anything that made, or is about to make, a session.
_LAUNCHING_STATUSES = ("LAUNCHING", "LAUNCHED", "SCHEDULED", "APPROVED")


class RateLimited(NoMatch):
    """The rule matched nothing yet: its concurrency cap or cooldown stopped it before it looked."""


def evaluate_rule(rule: LaunchRule, data: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
    """Raises if the rule type itself fails, so the caller can record the error against the rule."""
    limited = rate_limit(rule, ctx)
    if limited:
        return RateLimited(limited)
    return RULE_TYPE_REGISTRY[rule["rule_type"]]().evaluate(data, rule["parameters"], ctx)


def rate_limit(rule: LaunchRule, ctx: RuleContext) -> str | None:
    """Why the rule may not launch right now, or None."""
    max_concurrent = rule.get("max_concurrent_sessions")
    strategy_id = rule["parameters"].get("strategy_id")
    if max_concurrent is not None and strategy_id is not None:
        active = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status=ACTIVE_SESSION_STATUSES)
        if len(active) >= max_concurrent:
            return f"{len(active)} active session(s), at the cap of {max_concurrent}"

    cooldown = rule.get("cooldown_minutes") or 0
    if cooldown > 0:
        since = datetime.now(timezone.utc) - timedelta(minutes=cooldown)
        recent = [r for r in ctx.dynamo.requests_for_rule_since(rule["rule_id"], since)
                  if r.get("action", "launch") == "launch" and r["status"] in _LAUNCHING_STATUSES]
        if recent:
            return f"launched {recent[0]['date_created']}, inside the {cooldown}-minute cooldown"

    return None
