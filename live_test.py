"""Approved safe-write end-to-end checks using a separate Basecamp conductor."""

from __future__ import annotations

import asyncio
import html
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _conductor_client(agent_client: Any) -> Any:
    try:
        from .adapter import _make_client
    except ImportError:  # pragma: no cover
        from adapter import _make_client

    token_file = os.getenv("BASECAMP_CONDUCTOR_TOKEN_FILE", "").strip()
    person_id = os.getenv("BASECAMP_CONDUCTOR_PERSON_ID", "").strip()
    email = os.getenv("BASECAMP_CONDUCTOR_EMAIL", "").strip()
    if not token_file or not person_id or not email:
        raise RuntimeError(
            "BASECAMP_CONDUCTOR_TOKEN_FILE, BASECAMP_CONDUCTOR_PERSON_ID, and "
            "BASECAMP_CONDUCTOR_EMAIL are required"
        )
    if not Path(token_file).expanduser().is_file():
        raise RuntimeError("The configured Basecamp conductor token file does not exist")
    if person_id == agent_client.expected.person_id:
        raise RuntimeError("The Basecamp conductor must be a different full member from the agent")
    return _make_client(
        {
            "account_id": agent_client.expected.account_id,
            "person_id": person_id,
            "person_email": email,
            "mention": "@conductor",
            "project_ids": tuple(agent_client.project_ids),
            "access_token": "",
            "token_file": token_file,
            "refresh_token": "",
            "client_id": "",
            "client_secret": "",
            "expires_at": None,
        }
    )


async def _wait_for_campfire_reply(
    conductor: Any, campfire_id: str, agent_person_id: str, after_id: int, timeout: float
) -> float | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        lines = await conductor._account.campfires.list_lines(
            campfire_id=int(campfire_id), sort="created_at", direction="desc", max_items=100
        )
        if any(
            isinstance(line, Mapping)
            and int(line.get("id") or 0) > after_id
            and str((line.get("creator") or {}).get("id") or "") == agent_person_id
            for line in lines
        ):
            return time.monotonic() - started
        await asyncio.sleep(2)
    return None


async def _wait_for_assignment(
    conductor: Any, todo_id: str, agent_person_id: str, timeout: float
) -> tuple[float | None, bool]:
    started = time.monotonic()
    first_comment: float | None = None
    while time.monotonic() - started < timeout:
        todo = await conductor.call("todos", "get", {"todo_id": int(todo_id)})
        comments = await conductor.call(
            "comments", "list", {"recording_id": int(todo_id), "max_items": 100}
        )
        if first_comment is None and any(
            isinstance(comment, Mapping)
            and str((comment.get("creator") or {}).get("id") or "") == agent_person_id
            for comment in comments
        ):
            first_comment = time.monotonic() - started
        if isinstance(todo, Mapping) and todo.get("completed") is True and first_comment is not None:
            return first_comment, True
        await asyncio.sleep(2)
    return first_comment, False


async def run_live_test(agent_client: Any, *, campfire_id: str, todolist_id: str) -> dict[str, Any]:
    """Create one mention and one assignment, then verify Maximus-style participation."""
    if not campfire_id.isdigit() or not todolist_id.isdigit():
        raise ValueError("Live test Campfire and to-do list IDs must be numeric")
    conductor = _conductor_client(agent_client)
    project_id = str(agent_client.project_ids[0])
    label = f"HB10-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    try:
        await agent_client.attest_full_member()
        await conductor.attest_full_member()
        people = await conductor.call(
            "people", "list_for_project", {"project_id": int(project_id), "max_items": 500}
        )
        agent = next(
            (
                person
                for person in people
                if isinstance(person, Mapping)
                and str(person.get("id") or "") == agent_client.expected.person_id
            ),
            None,
        )
        if not isinstance(agent, Mapping) or not agent.get("attachable_sgid"):
            raise RuntimeError("The conductor cannot obtain the agent member's structured mention identity")
        mention = (
            '<bc-attachment content-type="application/vnd.basecamp.mention" '
            f'sgid="{html.escape(str(agent["attachable_sgid"]), quote=True)}"></bc-attachment>'
        )
        sent = await conductor.post_chat(project_id, campfire_id, f"<div>{mention} {label} reply to this test.</div>")
        sent_id = str(sent.get("id") or "")
        if not sent_id:
            raise RuntimeError("The conductor Campfire write returned no ID")
        await conductor.verify_chat_authorship(campfire_id, sent_id)
        campfire_latency = await _wait_for_campfire_reply(
            conductor, campfire_id, agent_client.expected.person_id, int(sent_id), 30
        )

        todolist = await conductor.call("todolists", "get", {"id": int(todolist_id)})
        conductor._require_owned(todolist, project_id)
        created = await conductor.call(
            "todos",
            "create",
            {
                "todolist_id": int(todolist_id),
                "content": f"{label} assignment test",
                "description": "Post a result comment, then complete this synthetic test to-do.",
                "assignee_ids": [int(agent_client.expected.person_id)],
            },
        )
        todo_id = str(created.get("id") or "") if isinstance(created, Mapping) else ""
        if not todo_id:
            raise RuntimeError("The conductor to-do write returned no ID")
        todo = await conductor.call("todos", "get", {"todo_id": int(todo_id)})
        conductor._require_owned(todo, project_id)
        conductor.verify_creator(todo)
        assignment_latency, completed = await _wait_for_assignment(
            conductor, todo_id, agent_client.expected.person_id, 180
        )
        ok = campfire_latency is not None and assignment_latency is not None and assignment_latency <= 60 and completed
        return {
            "ok": ok,
            "label": label,
            "campfire_first_response_seconds": campfire_latency,
            "assignment_first_response_seconds": assignment_latency,
            "assignment_completed": completed,
            "agent_authorship_verified": ok,
        }
    finally:
        await conductor.close()
