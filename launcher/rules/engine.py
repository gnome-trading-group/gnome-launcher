import logging
from datetime import datetime, timezone

from launcher.models import LaunchRule
from launcher.rules.types import RULE_TYPE_REGISTRY, RuleContext, RuleMatch

logger = logging.getLogger(__name__)


def evaluate_rules(rules: list[LaunchRule], data: dict, ctx: RuleContext) -> list[RuleMatch]:
    matches = []
    for rule in rules:
        if not _passes_rate_limits(rule, ctx):
            continue

        rule_type_cls = RULE_TYPE_REGISTRY.get(rule["rule_type"])
        if not rule_type_cls:
            logger.warning("Unknown rule type: %s", rule["rule_type"])
            continue

        try:
            evaluation = rule_type_cls().evaluate(data, rule["parameters"], ctx)
        except Exception:
            logger.exception("Rule evaluation failed for rule %s", rule.get("rule_id"))
            continue

        if evaluation and evaluation.should_launch:
            matches.append(RuleMatch(rule=rule, evaluation=evaluation))

    return matches


def _passes_rate_limits(rule: LaunchRule, ctx: RuleContext) -> bool:
    max_concurrent = rule.get("max_concurrent_sessions")
    if max_concurrent:
        strategy_id = rule.get("parameters", {}).get("strategy_id")
        if strategy_id:
            active = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status="RUNNING")
            if len(active) >= max_concurrent:
                logger.info(
                    "Rule %s: max_concurrent_sessions %d reached (%d active)",
                    rule.get("rule_id"), max_concurrent, len(active),
                )
                return False

    cooldown = rule.get("cooldown_minutes", 0)
    if cooldown > 0:
        last_launch = ctx.dynamo.get_latest_launch_for_rule(rule["rule_id"])
        if last_launch and _minutes_since(last_launch["date_created"]) < cooldown:
            logger.info(
                "Rule %s: cooldown %d minutes not yet elapsed",
                rule.get("rule_id"), cooldown,
            )
            return False

    return True


def _minutes_since(iso_timestamp: str) -> float:
    then = datetime.fromisoformat(iso_timestamp)
    now = datetime.now(timezone.utc)
    return (now - then).total_seconds() / 60
