"""Derive Basecamp mutation context from Hermes task-local runtime state."""

from __future__ import annotations

from gateway.session_context import get_session_env

try:
    from .event_model import parse_target
    from .policy import ExecutionContext, TriggerClass
except ImportError:  # pragma: no cover - installed flat-module wheel
    from event_model import parse_target
    from policy import ExecutionContext, TriggerClass

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _target_project(variable: str) -> str:
    raw_target = get_session_env(variable, "").strip()
    try:
        _, project_id, _ = parse_target(raw_target)
    except ValueError as exc:
        raise PermissionError(f"Trusted Hermes {variable} is not a valid Basecamp target") from exc
    return project_id


def derive_execution_context(project_id: str, profile_id: str) -> ExecutionContext:
    """Return policy context only when trusted Hermes routing proves the project."""
    if not project_id.isdigit():
        raise PermissionError("Basecamp execution project must be numeric")

    cron_session = get_session_env("HERMES_CRON_SESSION", "").strip().lower() in _TRUE_VALUES
    if cron_session:
        platform = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
        if platform != "basecamp":
            raise PermissionError("Basecamp scheduled mutation requires Basecamp auto-delivery")
        if _target_project("HERMES_CRON_AUTO_DELIVER_CHAT_ID") != project_id:
            raise PermissionError("Basecamp scheduled target belongs to another project")
        return ExecutionContext(TriggerClass.APPROVED_SCHEDULE, project_id, profile_id)

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform != "basecamp":
        raise PermissionError("Basecamp ordinary mutation requires an addressed Basecamp session")
    if _target_project("HERMES_SESSION_CHAT_ID") != project_id:
        raise PermissionError("Basecamp session target belongs to another project")
    return ExecutionContext(TriggerClass.DIRECT_MENTION, project_id, profile_id)
