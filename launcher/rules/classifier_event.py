import json
import logging
import os
from datetime import datetime, timezone

import anthropic
import boto3
from gnomepy.registry import RelationshipGraph

from launcher.rules.types import (
    STRATEGY_PARAMS_SCHEMA,
    STRATEGY_REQUIRED_PARAMS,
    RuleContext,
    NoMatch,
    RuleEvaluation,
    ResolvedStrategyConfig,
    ScheduleResult,
    ShutdownEvaluation,
    register_rule_type,
    RuleType,
)
from launcher.sessions import ACTIVE_SESSION_STATUSES, listing_ids

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
                "description": "Minimum related pairs (equivalent or implied, flagged ones left out) touching the event",
            },
            "required_relationship_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["EQUIVALENT", "IMPLIES"]},
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

    def evaluate(self, data: dict, params: dict, ctx: RuleContext) -> RuleEvaluation | NoMatch:
        event_ids = data["event_ids"]
        event_names = data["event_names"]
        skipped: list[str] = []

        for event_id, event_name in zip(event_ids, event_names):
            events = ctx.registry.get_event(event_id=event_id)
            if not events:
                skipped.append(f"{event_name}: not in the registry")
                continue
            event = events[0]

            allowed = params.get("allowed_categories")
            if allowed and event.category not in allowed:
                skipped.append(f"{event_name}: category {event.category} isn't allowed")
                continue

            contracts = ctx.registry.get_event_contracts(event_id=event_id)
            security_ids = [c.security_id for c in contracts]

            # The registry stores only the fewest rows that rebuild the relationships, so the pairs come from the graph
            # rebuilt around the event, not from a count of rows.
            graph = RelationshipGraph.load(ctx.registry, [event_id])
            own = set(security_ids)
            pairs = [("EQUIVALENT", a, b) for a, b in graph.get_equivalent_pairs() if a in own or b in own]
            pairs += [("IMPLIES", a, b) for a, b in graph.get_implies_pairs() if a in own or b in own]

            if len(pairs) < params.get("min_relationships", 1):
                skipped.append(f"{event_name}: {len(pairs)} relationship(s), fewer than required")
                continue

            required_types = set(params.get("required_relationship_types", []))
            found_types = {relationship_type for relationship_type, _, _ in pairs}
            if required_types and not required_types & found_types:
                skipped.append(f"{event_name}: none of the required relationship types")
                continue

            listings = self._resolve_listings(contracts, params, ctx)
            if not listings:
                skipped.append(f"{event_name}: no listings resolved")
                continue

            resolved_config = ResolvedStrategyConfig.from_params(params, listings=listings)

            return RuleEvaluation(
                should_launch=True,
                resolved_config=resolved_config,
                reason=(
                    f"Event '{event_name}' has {len(pairs)} relationships"
                    f" ({', '.join(sorted(found_types))})"
                ),
            )

        return NoMatch("; ".join(skipped) or "no events")

    def evaluate_shutdown(self, data: dict, params: dict, ctx: RuleContext) -> ShutdownEvaluation | NoMatch:
        resolved_event_ids = set(data["event_ids"])
        event_names = data["event_names"]

        strategy_id = params["strategy_id"]
        running_sessions = ctx.registry.get_strategy_sessions(strategy_id=strategy_id, status=ACTIVE_SESSION_STATUSES)
        if not running_sessions:
            return NoMatch("no running session of the strategy")

        # Walked from the sessions' listings, not the resolved events: one resolution batch can carry hundreds of
        # events, and a call per event outlasts the Lambda timeout.
        events_by_listing: dict[int, set[int]] = {}
        target_session_ids = []
        for session in running_sessions:
            held = listing_ids(session.config.get("listings", []))
            for listing_id in held - events_by_listing.keys():
                events_by_listing[listing_id] = self._listing_event_ids(listing_id, ctx)
            if any(events_by_listing[listing_id] & resolved_event_ids for listing_id in held):
                target_session_ids.append(session.session_id)
        if not target_session_ids:
            return NoMatch("no running session holds the resolved events' listings")

        names_preview = ", ".join(event_names[:3]) + ("..." if len(event_names) > 3 else "")
        return ShutdownEvaluation(
            should_shutdown=True,
            target_session_ids=target_session_ids,
            reason=f"Events resolved: {names_preview}",
        )

    def compute_schedule_time(self, data: dict, params: dict, ctx: RuleContext) -> ScheduleResult | None:
        event_ids = data["event_ids"]
        event_names = data["event_names"]

        if not event_names:
            return None

        contracts_text = "\n".join(f"- {n}" for n in event_names[:10])
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

    def _listing_event_ids(self, listing_id: int, ctx: RuleContext) -> set[int]:
        return {
            contract.event_id
            for listing in ctx.registry.get_listing(listing_id=listing_id)
            for contract in ctx.registry.get_event_contracts(security_id=listing.security_id)
        }

    def _resolve_listings(
        self, contracts, params: dict, ctx: RuleContext
    ) -> list[int] | None:
        if params.get("listing_resolution") == "static":
            raw = params.get("static_listings")
            if isinstance(raw, str):
                return [int(x.strip()) for x in raw.split(",") if x.strip()]
            return raw

        listing_ids = []
        exchange_filter = params.get("exchange_filter")
        for contract in contracts:
            resolved = ctx.registry.get_listing(security_id=contract.security_id)
            for listing in resolved:
                if not exchange_filter or listing.exchange_id in exchange_filter:
                    listing_ids.append(listing.listing_id)

        return listing_ids or None
