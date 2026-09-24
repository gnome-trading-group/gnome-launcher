from typing import Literal, TypedDict


class LaunchRequest(TypedDict):
    request_id: str
    action: Literal["launch", "shutdown"]
    status: Literal[
        "PENDING_APPROVAL", "APPROVED", "REJECTED", "LAUNCHING", "LAUNCHED", "FAILED",
        "SCHEDULED",
        "PENDING_SHUTDOWN_APPROVAL", "SHUTDOWN_APPROVED", "SHUTDOWN_REJECTED",
        "STOPPING", "STOPPED", "STOP_FAILED",
    ]
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
    date_created: str
    date_modified: str
    ttl: int


class LaunchRule(TypedDict):
    rule_id: str
    name: str
    description: str | None
    rule_type: str
    status: Literal["active", "disabled"]
    launch_path: Literal["auto", "approval"]
    shutdown_path: Literal["auto", "approval"]
    schedule_enabled: bool
    max_concurrent_sessions: int | None
    cooldown_minutes: int
    dedup_window_minutes: int
    parameters: dict
    date_created: str
    date_modified: str
