from typing import Literal, TypedDict

RequestStatus = Literal[
    "PENDING_APPROVAL", "APPROVED", "REJECTED", "SCHEDULED", "CANCELLED", "LAUNCHING", "LAUNCHED", "FAILED",
    "PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_APPROVED", "SHUTDOWN_REJECTED", "STOPPING", "STOPPED", "STOP_FAILED",
]
RuleStatus = Literal["active", "disabled", "archived"]

TriggerOutcome = Literal[
    "REQUESTED",     # the rule made a launch or shutdown request (request_id says which)
    "DUPLICATE",     # the rule already acted on this trigger inside its dedup window
    "RATE_LIMITED",  # the rule's concurrency cap or cooldown
    "NO_MATCH",      # the rule looked and passed, with its reason
    "BLOCKED",       # another session already holds the listings
    "ERROR",         # the rule raised
    "INVALID",       # the trigger didn't match its rule type's data_schema (trigger-level)
    "NO_RULES",      # no rule of the type to ask (trigger-level)
]

# A request in one of these went nowhere: it holds no session and doesn't count as a repeat of its trigger.
DEAD_END_STATUSES = ("FAILED", "REJECTED", "CANCELLED", "SHUTDOWN_REJECTED", "STOP_FAILED")


class RequestEvent(TypedDict):
    at: str
    status: str | None
    actor: str
    note: str | None


class LaunchRequest(TypedDict):
    request_id: str
    trigger_id: str | None
    rule_revision: int | None
    retry_of: str | None
    action: Literal["launch", "shutdown"]
    status: RequestStatus
    rule_type: str
    data: dict
    dedup_key: str
    resolved_config: dict
    matched_rule_id: str
    matched_rule_name: str
    launch_path: Literal["auto", "approval"]
    slack_message_ts: str | None
    slack_channel: str | None
    approved_by: str | None
    rejected_by: str | None
    session_id: str | None
    target_session_ids: list | None
    scheduled_time: str | None
    schedule_name: str | None
    launch_error: str | None
    # See launcher.links; None on requests made before links were stored.
    links: dict | None
    events: list[RequestEvent]
    # Copied from the session by the sweeper while it runs; `tracking` is set until the session has ended.
    tracking: str | None
    session_status: str | None
    session_started_at: str | None
    session_stopped_at: str | None
    session_failure_reason: str | None
    session_stop_reason: str | None
    session_stopped_by: str | None
    date_created: str
    date_modified: str


class LaunchRule(TypedDict):
    rule_id: str
    name: str
    description: str | None
    rule_type: str
    status: RuleStatus
    launch_path: Literal["auto", "approval"]
    shutdown_path: Literal["auto", "approval"]
    schedule_enabled: bool
    max_concurrent_sessions: int | None
    cooldown_minutes: int
    dedup_window_minutes: int
    parameters: dict
    revision: int
    created_by: str
    modified_by: str
    date_created: str
    date_modified: str
