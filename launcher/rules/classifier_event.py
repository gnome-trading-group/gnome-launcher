import json
import logging
import os
from datetime import datetime, timezone

import anthropic
import boto3

from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    RuleContext,
    RuleEvaluation,
    ResolvedStrategyConfig,
    ScheduleResult,
    ShutdownEvaluation,
    register_rule_type,
    RuleType,
)

logger = logging.getLogger(__name__)

_ANTHROPIC_API_KEY_SECRET = os.environ.get("ANTHROPIC_API_KEY_SECRET", "anthropic-api-key")
_MODEL = "claude-haiku-4-5-20251001"

_cached_api_key: str | None = None


def _get_anthropic_client() -> anthropic.Anthropic:
    global _cached_api_key
    if not _cached_api_key:
        sm = boto3.client("secretsmanager")
        _cached_api_key = sm.get_secret_value(SecretId=_ANTHROPIC_API_KEY_SECRET)["SecretString"]
    return anthropic.Anthropic(api_key=_cached_api_key)


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
            "listing_profile_mapping": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Maps exchange_id (as string) to simulation profile name for dynamically resolved listings",
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

            listings, listing_profile_entries = self._resolve_listings(contracts, params, ctx)
            if not listings:
                continue

            resolved_config = ResolvedStrategyConfig.from_params(params, listings=listings)
            if listing_profile_entries:
                resolved_config.simulation_config = {**resolved_config.simulation_config, **listing_profile_entries}

            return RuleEvaluation(
                should_launch=True,
                resolved_config=resolved_config,
                reason=(
                    f"Event '{event_name}' has {len(all_relationships)} relationships"
                    f" ({', '.join(sorted(found_types))})"
                ),
            )

        return None

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | None:
        event_ids = data["event_ids"]
        event_names = data["event_names"]

        strategy_id = params.get("strategy_id")
        if not strategy_id:
            return None

        running_sessions = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status="RUNNING")
        if not running_sessions:
            return None

        resolved_listing_ids: set[int] = set()
        for event_id in event_ids:
            contracts = ctx.registry.get_event_contracts(event_id=event_id)
            for contract in contracts:
                listings = ctx.registry.get_listing(security_id=contract.security_id)
                resolved_listing_ids.update(l.listing_id for l in listings)

        if not resolved_listing_ids:
            return None

        target_session_ids = [
            s.session_id
            for s in running_sessions
            if set(s.config.get("listings", [])) & resolved_listing_ids
        ]
        if not target_session_ids:
            return None

        names_preview = ", ".join(event_names[:3]) + ("..." if len(event_names) > 3 else "")
        return ShutdownEvaluation(
            should_shutdown=True,
            target_session_ids=target_session_ids,
            reason=f"Events resolved: {names_preview}",
        )

    def compute_schedule_time(self, data: dict, params: dict, ctx: RuleContext) -> ScheduleResult | None:
        event_ids = data["event_ids"]
        event_names = data["event_names"]

        contract_names: list[str] = []
        for event_id in event_ids:
            contracts = ctx.registry.get_event_contracts(event_id=event_id)
            contract_names.extend(c.name for c in contracts if hasattr(c, "name") and c.name)

        if not contract_names and event_names:
            contract_names = list(event_names)

        if not contract_names:
            return None

        contracts_text = "\n".join(f"- {n}" for n in contract_names[:10])
        prompt = (
            "Extract the event start date and time from the following prediction market contract names. "
            "Return a JSON object with a single field 'start_time' (ISO 8601, UTC) "
            f"or null if no specific start time can be determined.\n\nContracts:\n{contracts_text}"
        )

        try:
            client = _get_anthropic_client()
            message = client.messages.create(
                model=_MODEL,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
            result = json.loads(raw)
            start_time_str = result.get("start_time")
            if not start_time_str:
                return None

            start_dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if start_dt <= datetime.now(timezone.utc):
                return None

            names_preview = ", ".join(event_names[:2]) + ("..." if len(event_names) > 2 else "")
            return ScheduleResult(
                scheduled_time=start_dt,
                reason=f"Extracted start time for: {names_preview}",
            )
        except Exception:
            logger.exception("Failed to extract schedule time for events %s", event_ids)
            return None

    def _resolve_listings(
        self, contracts, params: dict, ctx: RuleContext
    ) -> tuple[list[int] | None, dict[str, str]]:
        if params.get("listing_resolution") == "static":
            raw = params.get("static_listings")
            if isinstance(raw, str):
                return [int(x.strip()) for x in raw.split(",") if x.strip()], {}
            return raw, {}

        listing_profile_mapping: dict[str, str] = params.get("listing_profile_mapping", {})
        listing_ids = []
        listing_profile_entries: dict[str, str] = {}
        exchange_filter = params.get("exchange_filter")
        for contract in contracts:
            resolved = ctx.registry.get_listing(security_id=contract.security_id)
            for listing in resolved:
                if not exchange_filter or listing.exchange_id in exchange_filter:
                    listing_ids.append(listing.listing_id)
                    profile = listing_profile_mapping.get(str(listing.exchange_id))
                    if profile:
                        listing_profile_entries[f"simulation.listing.{listing.listing_id}.profile"] = profile

        if not listing_ids:
            return None, {}
        return listing_ids, listing_profile_entries
