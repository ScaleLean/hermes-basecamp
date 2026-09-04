"""Ten focused Basecamp tools exposed to Hermes agents."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_hermes_spec = importlib.util.find_spec("hermes_cli")
if __name__ == "tools" and _hermes_spec and _hermes_spec.origin:
    __path__ = [str(Path(_hermes_spec.origin).resolve().parent.parent / "tools")]

try:
    from .operations import BasecampOperations
    from .policy import ExecutionContext, TriggerClass, default_registry, requires_action_approval
    from .trusted_context import derive_execution_context
except ImportError:  # pragma: no cover
    from operations import BasecampOperations
    from policy import ExecutionContext, TriggerClass, default_registry, requires_action_approval
    from trusted_context import derive_execution_context


WRITE_TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "basecamp_create_todo": (
        "todos.create", ("todolist_id", "content", "description", "assignee_ids", "due_on", "starts_on")
    ),
    "basecamp_complete_todo": ("todos.complete", ("todo_id",)),
    "basecamp_reopen_todo": ("todos.restore", ("todo_id",)),
    "basecamp_add_boost": ("boosts.create", ("recording_id", "content")),
    "basecamp_move_card": ("cards.move", ("card_id", "column_id", "position")),
    "basecamp_post_message": ("messages.create", ("board_id", "subject", "content", "category_id")),
    "basecamp_answer_checkin": ("checkins.answer", ("question_id", "content", "group_on")),
}


def _schema(properties: Mapping[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "required": list(required), "additionalProperties": False}


ID = {"type": "string", "pattern": "^[0-9]+$"}
TEXT = {"type": "string", "minLength": 1}
KEY = {"type": "string", "minLength": 8, "maxLength": 200}

TOOL_SCHEMAS = {
    "basecamp_create_todo": _schema(
        {"bucket_id": ID, "todolist_id": ID, "content": TEXT, "description": {"type": "string"},
         "assignee_ids": {"type": "array", "items": {"type": "integer"}},
         "due_on": {"type": "string", "format": "date"}, "starts_on": {"type": "string", "format": "date"},
         "idempotency_key": KEY},
        ("bucket_id", "todolist_id", "content", "idempotency_key"),
    ),
    "basecamp_complete_todo": _schema(
        {"bucket_id": ID, "todo_id": ID, "idempotency_key": KEY}, ("bucket_id", "todo_id", "idempotency_key")
    ),
    "basecamp_reopen_todo": _schema(
        {"bucket_id": ID, "todo_id": ID, "idempotency_key": KEY}, ("bucket_id", "todo_id", "idempotency_key")
    ),
    "basecamp_read_history": _schema(
        {"bucket_id": ID, "recording_id": ID, "type": {"type": "string", "enum": ["comments", "campfire"]},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        ("bucket_id", "recording_id", "type"),
    ),
    "basecamp_add_boost": _schema(
        {"bucket_id": ID, "recording_id": ID, "content": TEXT, "idempotency_key": KEY},
        ("bucket_id", "recording_id", "idempotency_key"),
    ),
    "basecamp_move_card": _schema(
        {"bucket_id": ID, "card_id": ID, "column_id": {"type": "integer"},
         "position": {"type": "integer", "minimum": 1}, "idempotency_key": KEY},
        ("bucket_id", "card_id", "column_id", "idempotency_key"),
    ),
    "basecamp_post_message": _schema(
        {"bucket_id": ID, "board_id": ID, "subject": TEXT, "content": {"type": "string"},
         "category_id": {"type": "integer"}, "idempotency_key": KEY},
        ("bucket_id", "board_id", "subject", "idempotency_key"),
    ),
    "basecamp_answer_checkin": _schema(
        {"bucket_id": ID, "question_id": ID, "content": TEXT,
         "group_on": {"type": "string", "format": "date"}, "idempotency_key": KEY},
        ("bucket_id", "question_id", "content", "idempotency_key"),
    ),
    "basecamp_api_read": _schema(
        {"path": TEXT, "query": {"type": "object", "additionalProperties": {"type": "string"}}}, ("path",)
    ),
    "basecamp_api_write": _schema(
        {"method": {"type": "string", "enum": ["POST", "PUT", "DELETE"]}, "path": TEXT,
         "body": {"type": "object"}, "idempotency_key": KEY}, ("method", "path", "idempotency_key")
    ),
}

DESCRIPTIONS = {
    "basecamp_create_todo": "Create a to-do in an allowlisted Basecamp project.",
    "basecamp_complete_todo": "Complete a Basecamp to-do after successful assigned work.",
    "basecamp_reopen_todo": "Reopen a Basecamp to-do.",
    "basecamp_read_history": "Read recent comments or Campfire lines.",
    "basecamp_add_boost": "Add a boost to a Basecamp recording.",
    "basecamp_move_card": "Move a Basecamp card to a column.",
    "basecamp_post_message": "Post a Basecamp message-board message.",
    "basecamp_answer_checkin": "Answer a Basecamp automatic check-in.",
    "basecamp_api_read": "Read an allowlisted path covered by the Basecamp SDK API inventory.",
    "basecamp_api_write": "Write an allowlisted, non-Adminland Basecamp SDK API path.",
}


async def _focused(client: Any, name: str, values: Mapping[str, Any]) -> dict[str, Any]:
    capability, allowed = WRITE_TOOLS[name]
    bucket_id = str(values["bucket_id"])
    arguments = {key: values[key] for key in allowed if key in values and values[key] is not None}
    profile_id = f"{client.expected.account_id}:{client.expected.person_id}"
    capability_definition = default_registry().get(capability)
    approval = None
    if requires_action_approval(capability_definition, arguments):
        try:
            from .approval import APPROVAL_BROKER
        except ImportError:  # pragma: no cover
            from approval import APPROVAL_BROKER
        approval = APPROVAL_BROKER.consume(capability, bucket_id, arguments, profile_id=profile_id)
    result = await BasecampOperations(client, profile_id=profile_id).execute(
        capability, arguments, derive_execution_context(bucket_id, profile_id),
        approval=approval,
        idempotency_key=str(values["idempotency_key"])
    )
    return {"ok": True, "result": result.value, "verified": result.verified, "replayed": result.replayed}


async def _history(client: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    bucket_id = str(values["bucket_id"])
    profile_id = f"{client.expected.account_id}:{client.expected.person_id}"
    campfire = values["type"] == "campfire"
    operation = "campfire.history" if campfire else "comments.list"
    key = "campfire_id" if campfire else "recording_id"
    arguments = {key: int(values["recording_id"]), "max_items": min(int(values.get("limit") or 20), 50)}
    result = await BasecampOperations(client, profile_id=profile_id).execute(
        operation, arguments, ExecutionContext(TriggerClass.UNATTENDED, bucket_id, profile_id)
    )
    return {"ok": True, "items": result.value}


API_ROUTES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("GET", r"/projects\.json", "projects.list", ()),
    ("GET", r"/projects/(?P<project_id>\d+)\.json", "projects.get", ()),
    ("GET", r"/projects/(?P<project_id>\d+)/people\.json", "people.list", ()),
    ("GET", r"/buckets/(?P<bucket_id>\d+)/todos/(?P<todo_id>\d+)\.json", "todos.get", ()),
    ("GET", r"/buckets/(?P<bucket_id>\d+)/todolists/(?P<todolist_id>\d+)/todos\.json", "todos.list", ()),
    ("GET", r"/buckets/(?P<bucket_id>\d+)/recordings/(?P<recording_id>\d+)/comments\.json", "comments.list", ()),
    ("GET", r"/buckets/(?P<bucket_id>\d+)/chats/(?P<campfire_id>\d+)/lines\.json", "campfire.history", ()),
    ("GET", r"/buckets/(?P<bucket_id>\d+)/documents/(?P<document_id>\d+)\.json", "documents.get", ()),
    ("GET", r"/buckets/(?P<bucket_id>\d+)/card_tables/cards/(?P<card_id>\d+)\.json", "cards.get", ()),
    ("GET", r"/buckets/(?P<bucket_id>\d+)/messages/(?P<message_id>\d+)\.json", "messages.get", ()),
    ("POST", r"/buckets/(?P<bucket_id>\d+)/recordings/(?P<recording_id>\d+)/comments\.json", "comments.create", ("content",)),
    ("PUT", r"/buckets/(?P<bucket_id>\d+)/todos/(?P<todo_id>\d+)\.json", "todos.update", ()),
    ("POST", r"/buckets/(?P<bucket_id>\d+)/todolists/(?P<todolist_id>\d+)/todos\.json", "todos.create", ("content",)),
    ("POST", r"/buckets/(?P<bucket_id>\d+)/todos/(?P<todo_id>\d+)/completion\.json", "todos.complete", ()),
    ("DELETE", r"/buckets/(?P<bucket_id>\d+)/todos/(?P<todo_id>\d+)/completion\.json", "todos.restore", ()),
    ("POST", r"/buckets/(?P<bucket_id>\d+)/message_boards/(?P<board_id>\d+)/messages\.json", "messages.create", ("subject",)),
    ("PUT", r"/buckets/(?P<bucket_id>\d+)/card_tables/cards/(?P<card_id>\d+)/moves\.json", "cards.move", ("column_id",)),
    ("POST", r"/buckets/(?P<bucket_id>\d+)/questions/(?P<question_id>\d+)/answers\.json", "checkins.answer", ("content",)),
)


def _resolve_api_route(method: str, path: str) -> tuple[str, str, dict[str, Any], tuple[str, ...]]:
    if "//" in path or ".." in path or "?" in path or "#" in path:
        raise PermissionError("Basecamp API path must be a relative canonical path")
    for expected_method, pattern, capability, required_body in API_ROUTES:
        match = re.fullmatch(pattern, path)
        if match and method == expected_method:
            return (
                capability,
                str(match.groupdict().get("bucket_id") or match.groupdict().get("project_id") or ""),
                {
                    key: int(value) if value.isdigit() else value
                    for key, value in match.groupdict().items()
                    if value is not None and key != "bucket_id"
                },
                required_body,
            )
    raise PermissionError("Basecamp API method and path are not in the reviewed typed SDK route inventory")


async def _api(client: Any, method: str, values: Mapping[str, Any]) -> dict[str, Any]:
    capability, bucket_id, arguments, required_body = _resolve_api_route(method, str(values["path"]))
    body = values.get("body") or {}
    query = values.get("query") or {}
    if not isinstance(body, Mapping) or not isinstance(query, Mapping):
        raise TypeError("Basecamp API body and query must be objects")
    missing = [key for key in required_body if not body.get(key)]
    if missing:
        raise ValueError(f"Basecamp API body is missing required fields: {', '.join(missing)}")
    arguments.update(body if method != "GET" else query)
    profile_id = f"{client.expected.account_id}:{client.expected.person_id}"
    if not bucket_id and capability == "projects.list":
        bucket_id = client.project_ids[0]
    context = (
        ExecutionContext(TriggerClass.UNATTENDED, bucket_id, profile_id)
        if method == "GET"
        else derive_execution_context(bucket_id, profile_id)
    )
    capability_definition = default_registry().get(capability)
    approval = None
    if method != "GET" and requires_action_approval(capability_definition, arguments):
        try:
            from .approval import APPROVAL_BROKER
        except ImportError:  # pragma: no cover
            from approval import APPROVAL_BROKER
        approval = APPROVAL_BROKER.consume(capability, bucket_id, arguments, profile_id=profile_id)
    result = await BasecampOperations(client, profile_id=profile_id).execute(
        capability, arguments, context, approval=approval, idempotency_key=values.get("idempotency_key")
    )
    return {"ok": True, "data": result.value, "verified": result.verified, "replayed": result.replayed}


def register_tools(ctx: Any, client_factory: Any, profile_id: str | None = None) -> None:
    """Register the stable ten-tool Basecamp 1.0 interface."""
    for tool_name, schema in TOOL_SCHEMAS.items():
        async def handler(*, _tool_name: str = tool_name, **values: Any) -> dict[str, Any]:
            client = client_factory()
            try:
                active_profile = f"{client.expected.account_id}:{client.expected.person_id}"
                if profile_id is not None and profile_id != active_profile:
                    raise PermissionError("Basecamp tool client belongs to another Hermes profile")
                if _tool_name in WRITE_TOOLS:
                    return await _focused(client, _tool_name, values)
                if _tool_name == "basecamp_read_history":
                    return await _history(client, values)
                if _tool_name == "basecamp_api_read":
                    return await _api(client, "GET", values)
                return await _api(client, values["method"], values)
            finally:
                await client.close()

        ctx.register_tool(name=tool_name, toolset="basecamp", schema=schema, handler=handler, is_async=True,
                          description=DESCRIPTIONS[tool_name], emoji="⛺")
