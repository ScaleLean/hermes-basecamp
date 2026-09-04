"""Bridge Hermes action-time approvals to exact Basecamp operations."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

try:
    from .policy import ActionApproval, action_digest
except ImportError:  # pragma: no cover
    from policy import ActionApproval, action_digest

_PREFIX = "basecamp-action:"


def approval_rule_key(
    operation: str,
    project_id: str,
    arguments: Mapping[str, Any],
    *,
    profile_id: str = "",
) -> str:
    return f"{_PREFIX}{profile_id}:{operation}:{project_id}:{action_digest(operation, project_id, arguments)}"


def _active_profile_id() -> str:
    """Return the task-local Basecamp identity used to bind approvals."""
    from agent.secret_scope import get_secret

    account_id = str(get_secret("BASECAMP_ACCOUNT_ID", "") or "").strip()
    person_id = str(get_secret("BASECAMP_PERSON_ID", "") or "").strip()
    return f"{account_id}:{person_id}" if account_id and person_id else ""


class ApprovalBroker:
    """Consume one approval response for one exact tool invocation."""

    def __init__(self, *, ttl_seconds: float = 60.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._grants: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def record(self, rule_keys: list[str], choice: str) -> None:
        if choice not in {"once", "session", "always", "smart_approve"}:
            return
        now = time.time()
        with self._lock:
            for key in rule_keys:
                if key.startswith(_PREFIX):
                    self._grants[key] = (now + self.ttl_seconds, choice)

    def consume(
        self,
        operation: str,
        project_id: str,
        arguments: Mapping[str, Any],
        *,
        profile_id: str = "",
    ) -> ActionApproval | None:
        key = approval_rule_key(operation, project_id, arguments, profile_id=profile_id)
        with self._lock:
            grant = self._grants.pop(key, None)
        if grant is None or grant[0] < time.time():
            return None
        return ActionApproval(
            operation,
            project_id,
            action_digest(operation, project_id, arguments),
            f"hermes:{grant[1]}",
            grant[0],
        )


APPROVAL_BROKER = ApprovalBroker()


def pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict[str, str] | None:
    if not tool_name.startswith("basecamp_") or not isinstance(args, Mapping):
        return None
    operation = str(args.get("operation") or "")
    project_id = str(args.get("project_id") or args.get("bucket_id") or "")
    arguments = args.get("arguments")
    if not operation:
        try:
            from .tools import WRITE_TOOLS, _resolve_api_route
        except ImportError:  # pragma: no cover
            from tools import WRITE_TOOLS, _resolve_api_route
        if tool_name in WRITE_TOOLS:
            operation, allowed = WRITE_TOOLS[tool_name]
            arguments = {key: args[key] for key in allowed if key in args and args[key] is not None}
        elif tool_name == "basecamp_api_write":
            try:
                operation, project_id, path_arguments, _ = _resolve_api_route(
                    str(args.get("method") or ""), str(args.get("path") or "")
                )
            except PermissionError as exc:
                return {"action": "block", "message": str(exc)}
            arguments = {**path_arguments, **dict(args.get("body") or {})}
        elif tool_name in {"basecamp_read_history", "basecamp_api_read"}:
            return None
    if not operation or not project_id.isdigit() or not isinstance(arguments, Mapping):
        return {"action": "block", "message": "Invalid Basecamp operation context"}
    try:
        from .policy import RiskClass, default_registry, requires_action_approval
    except ImportError:  # pragma: no cover
        from policy import RiskClass, default_registry, requires_action_approval
    try:
        capability = default_registry().get(operation)
    except PermissionError as exc:
        return {"action": "block", "message": str(exc)}
    if capability.risk is RiskClass.READ:
        return None
    if capability.risk is RiskClass.ADMINLAND_DENIED:
        return {"action": "block", "message": "Basecamp Adminland operations are not automated"}
    if requires_action_approval(capability, arguments):
        return {
            "action": "approve",
            "message": f"Approve exact sensitive Basecamp action {operation} in project {project_id}?",
            "rule_key": approval_rule_key(operation, project_id, arguments, profile_id=_active_profile_id()),
        }
    if capability.risk is RiskClass.ORDINARY_WRITE:
        try:
            from .trusted_context import derive_execution_context
        except ImportError:  # pragma: no cover
            from trusted_context import derive_execution_context
        try:
            derive_execution_context(project_id, "pre-tool-call")
        except PermissionError as exc:
            return {"action": "block", "message": str(exc)}
        return None
    return {"action": "block", "message": "Unsupported Basecamp capability risk"}


def post_approval_response(
    pattern_keys: list[str] | None = None,
    choice: str = "deny",
    **_: Any,
) -> None:
    APPROVAL_BROKER.record(list(pattern_keys or []), choice)
