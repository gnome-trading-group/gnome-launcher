import logging
from datetime import datetime, timedelta, timezone

from launcher.models import LaunchRule
from launcher.rules.types import RULE_TYPE_REGISTRY, RuleContext, RuleEvaluation
from launcher.sessions import ACTIVE_SESSION_STATUSES

logger = logging.getLogger(__name__)

# What counts as the rule having launched, for its cooldown: anything that made, or is about to make, a session.
_LAUNCHING_STATUSES = ("LAUNCHING", "LAUNCHED", "SCHEDULED", "APPROVED")


def evaluate_rule(rule: LaunchRule, data: dict, ctx: RuleContext) -> RuleEvaluation | None:
    rule_type_cls = RULE_TYPE_REGISTRY.get(rule["rule_type"])
    if not rule_type_cls:
        logger.warning("Unknown rule type: %s", rule["rule_type"])
        return None
    if not passes_rate_limits(rule, ctx):
        return None
    try:
        evaluation = rule_type_cls().evaluate(data, rule["parameters"], ctx)
    except Exception:
        logger.exception("Rule evaluation failed for rule %s", rule.get("rule_id"))
        return None
    return evaluation if evaluation and evaluation.should_launch else None


def passes_rate_limits(rule: LaunchRule, ctx: RuleContext) -> bool:
    max_concurrent = rule.get("max_concurrent_sessions")
    strategy_id = rule["parameters"].get("strategy_id")
    if max_concurrent is not None and strategy_id is not None:
        active = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status=ACTIVE_SESSION_STATUSES)
        if len(active) >= max_concurrent:
            logger.info("Rule %s: max_concurrent_sessions %d reached (%d active)",
                        rule["rule_id"], max_concurrent, len(active))
            return False

    cooldown = rule.get("cooldown_minutes") or 0
    if cooldown > 0:
        since = datetime.now(timezone.utc) - timedelta(minutes=cooldown)
        recent = [r for r in ctx.dynamo.requests_for_rule_since(rule["rule_id"], since)
                  if r.get("action", "launch") == "launch" and r["status"] in _LAUNCHING_STATUSES]
        if recent:
            logger.info("Rule %s: cooldown %d minutes not yet elapsed", rule["rule_id"], cooldown)
            return False

    return True
