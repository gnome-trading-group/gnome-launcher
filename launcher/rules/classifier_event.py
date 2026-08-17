from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    RuleContext,
    RuleEvaluation,
    ResolvedStrategyConfig,
    register_rule_type,
    RuleType,
)


@register_rule_type
class ClassifierEventRule(RuleType):
    type = "classifier_event"
    display_name = "Classifier Event"

    data_schema = {
        "type": "object",
        "properties": {
            "event_ids": {"type": "array", "items": {"type": "integer"}},
            "event_names": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["event_ids", "event_names"],
    }

    parameter_schema = {
        "type": "object",
        "properties": {
            "allowed_categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only trigger for events in these categories",
            },
            "min_relationships": {
                "type": "integer",
                "default": 1,
                "description": "Minimum cross-exchange relationships required",
            },
            "required_relationship_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least one of these relationship types must exist",
            },
            "listing_resolution": {
                "type": "string",
                "enum": ["from_event", "static"],
                "default": "from_event",
            },
            "static_listings": {"type": "string"},
            "exchange_filter": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Only include listings from these exchange IDs",
            },
            **STRATEGY_PARAMS_SCHEMA,
        },
        "required": STRATEGY_REQUIRED_PARAMS,
    }

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | None:
        event_ids = data["event_ids"]
        event_names = data["event_names"]

        for event_id, event_name in zip(event_ids, event_names):
            events = ctx.registry.get_event(event_id=event_id)
            if not events:
                continue
            event = events[0]

            allowed = params.get("allowed_categories")
            if allowed and event.category not in allowed:
                continue

            contracts = ctx.registry.get_event_contracts(event_id=event_id)
            security_ids = [c.security_id for c in contracts]

            all_relationships = []
            for sid in security_ids:
                all_relationships.extend(ctx.registry.get_contract_relationships(security_id=sid))

            if len(all_relationships) < params.get("min_relationships", 1):
                continue

            required_types = set(params.get("required_relationship_types", []))
            found_types = {r.relationship_type for r in all_relationships}
            if required_types and not required_types & found_types:
                continue

            listings = self._resolve_listings(contracts, params, ctx)
            if not listings:
                continue

            return RuleEvaluation(
                should_launch=True,
                resolved_config=ResolvedStrategyConfig.from_params(params, listings=listings),
                reason=(
                    f"Event '{event_name}' has {len(all_relationships)} relationships"
                    f" ({', '.join(sorted(found_types))})"
                ),
            )

        return None

    def _resolve_listings(self, contracts, params: dict, ctx: RuleContext) -> str | None:
        if params.get("listing_resolution") == "static":
            return params.get("static_listings")

        listing_ids = []
        exchange_filter = params.get("exchange_filter")
        for contract in contracts:
            listings = ctx.registry.get_listing(security_id=contract.security_id)
            for listing in listings:
                if not exchange_filter or listing.exchange_id in exchange_filter:
                    listing_ids.append(listing.listing_id)

        if not listing_ids:
            return None
        return ",".join(str(lid) for lid in listing_ids)
