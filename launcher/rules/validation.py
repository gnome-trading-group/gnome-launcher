"""What makes a launch rule valid, checked whenever one is created or changed, and what makes a trigger valid for
its rule type, checked before any rule sees it. A rule that would only fail when a trigger arrives (a missing
strategy_id, a profile the orchestrator can't read) is refused when it is saved instead."""
from __future__ import annotations

from jsonschema import Draft202012Validator

from launcher.rules.catalog import RULE_TYPE_REGISTRY

RULE_DEFAULTS = {
    "description": None,
    "status": "active",
    "shutdown_path": "auto",
    "schedule_enabled": False,
    "max_concurrent_sessions": None,
    "cooldown_minutes": 0,
    "dedup_window_minutes": 60,
}
EDITABLE_FIELDS = ("name", "description", "status", "launch_path", "shutdown_path", "schedule_enabled",
                   "max_concurrent_sessions", "cooldown_minutes", "dedup_window_minutes", "parameters")

RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "description": {"type": ["string", "null"]},
        "rule_type": {"type": "string"},
        "status": {"enum": ["active", "disabled", "archived"]},
        "launch_path": {"enum": ["auto", "approval"]},
        "shutdown_path": {"enum": ["auto", "approval"]},
        "schedule_enabled": {"type": "boolean"},
        "max_concurrent_sessions": {"type": ["integer", "null"], "minimum": 1},
        "cooldown_minutes": {"type": "integer", "minimum": 0},
        "dedup_window_minutes": {"type": "integer", "minimum": 0},
        "parameters": {"type": "object"},
    },
    "required": ["name", "rule_type", "launch_path", "parameters", *RULE_DEFAULTS],
    "additionalProperties": False,
}


def _errors(schema: dict, value, prefix: str = "") -> list[str]:
    out = []
    for e in sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in e.absolute_path)
        where = ".".join(x for x in (prefix, path) if x)
        out.append(f"{where}: {e.message}" if where else e.message)
    return out


def validate_rule(rule: dict) -> list[str]:
    """Every problem with a whole rule (its own fields and its type's parameters); empty when it's valid."""
    problems = _errors(RULE_SCHEMA, rule)
    rule_type_cls = RULE_TYPE_REGISTRY.get(rule.get("rule_type"))
    if rule_type_cls is None:
        return problems + [f"rule_type: unknown rule type {rule.get('rule_type')!r}"]
    params = rule.get("parameters")
    if isinstance(params, dict):
        problems += _errors(rule_type_cls.parameter_schema, params, "parameters")
        problems += [f"parameters: {p}" for p in rule_type_cls().validate_parameters(params)]
    return problems


def validate_data(rule_type: str, data) -> list[str]:
    rule_type_cls = RULE_TYPE_REGISTRY.get(rule_type)
    if rule_type_cls is None:
        return [f"unknown rule type {rule_type!r}"]
    return _errors(rule_type_cls.data_schema, data, "data")
