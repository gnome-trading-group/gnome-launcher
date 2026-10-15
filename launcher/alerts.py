"""What the launcher reports to the alerting system (gnomepy's send_alert): launches and stops it made on its own, and
the ones that failed. Approval and schedule messages stay the launcher's own interactive Slack messages.

Alerting must never stop the launcher doing its job, so a report that can't be sent is only logged."""

import logging

import requests
from gnomepy.registry import RegistryClient

from launcher.rules.types import ResolvedStrategyConfig

logger = logging.getLogger(__name__)


def _actor(rule_name: str) -> str:
    return f"launcher:{rule_name}"


def _describe(error: Exception) -> str:
    # The registry says why it refused a launch (a missing risk policy, a stale balance, no active account) in the body.
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"{error} {error.response.text}".strip()
    return str(error)


def _send(registry: RegistryClient, alert_type: str, title: str, **fields) -> None:
    try:
        registry.send_alert(alert_type, title, **fields)
    except Exception:
        logger.warning("Couldn't report %s: %s", alert_type, title, exc_info=True)


def launched(registry: RegistryClient, rule_name: str, reason: str, config: ResolvedStrategyConfig, session_id: str) -> None:
    _send(
        registry, "LAUNCH_SUCCEEDED", f"{rule_name} launched {config.strategy_class}",
        key=f"LAUNCH_SUCCEEDED:{session_id}", body=reason, actor=_actor(rule_name),
        strategy_id=config.strategy_id, session_id=session_id, mode=config.mode,
        labels={"rule": rule_name, "listings": ", ".join(str(x) for x in config.listings)},
    )


def launch_failed(registry: RegistryClient, rule_name: str, request_id: str, error: Exception | str,
                  config: ResolvedStrategyConfig | None = None) -> None:
    _send(
        registry, "LAUNCH_FAILED", f"{rule_name} couldn't launch",
        key=f"LAUNCH_FAILED:{request_id}", body=error if isinstance(error, str) else _describe(error),
        actor=_actor(rule_name), strategy_id=config.strategy_id if config else None,
        mode=config.mode if config else None, labels={"rule": rule_name},
    )


def stopped(registry: RegistryClient, rule_name: str, reason: str, session_ids: list[str]) -> None:
    for session_id in session_ids:
        _send(
            registry, "SESSION_STOPPED_AUTO", f"{rule_name} stopped {session_id}",
            key=f"SESSION_STOPPED_AUTO:{session_id}", body=reason, actor=_actor(rule_name), session_id=session_id,
            labels={"rule": rule_name},
        )


def shutdown_failed(registry: RegistryClient, rule_name: str, errors: list[dict]) -> None:
    # One per session, so each carries its session (and, through it, whether it's live and pages).
    for failure in errors:
        _send(
            registry, "SHUTDOWN_FAILED", f"{rule_name} couldn't stop {failure['session_id']}",
            key=f"SHUTDOWN_FAILED:{failure['session_id']}", body=failure["error"], actor=_actor(rule_name),
            session_id=failure["session_id"], labels={"rule": rule_name},
        )
