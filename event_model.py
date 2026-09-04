"""Normalize Basecamp timeline events for the Hermes platform adapter."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_MENTION_CONTENT_TYPE = "application/vnd.basecamp.mention"
_PERSON_SGID = re.compile(r"^sgid://bc3/Person/(?P<person_id>\d+)(?:\?.*)?$")


class _MentionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.person_ids: set[str] = set()
        self._mention_attachments = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {name.lower(): value or "" for name, value in attrs}
        if tag == "img" and self._mention_attachments:
            person_id = values.get("data-avatar-for-person-id", "")
            if person_id.isdigit():
                self.person_ids.add(person_id)
            return
        if tag != "bc-attachment":
            return
        if values.get("content-type", "").lower() != _MENTION_CONTENT_TYPE:
            return
        self._mention_attachments += 1
        match = _PERSON_SGID.fullmatch(values.get("sgid", ""))
        if match:
            self.person_ids.add(match.group("person_id"))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "bc-attachment" and self._mention_attachments:
            self._mention_attachments -= 1


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    kind: str
    text: str
    person_id: str
    person_name: str
    project_id: str
    recording_id: str
    chat_id: str
    timestamp: datetime
    raw: Mapping[str, Any]


def _identifier(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("id") or "")
    return str(value or "")


def _plain_text(value: Any) -> str:
    raw = str(value or "")
    return html.unescape(_TAG.sub(" ", raw)).replace("\xa0", " ").strip()


def _mentioned_person_ids(value: Any) -> set[str]:
    parser = _MentionParser()
    parser.feed(str(value or ""))
    parser.close()
    return parser.person_ids


def normalize_event(raw: Mapping[str, Any]) -> NormalizedEvent | None:
    creator = raw.get("creator") or raw.get("person") or {}
    recording = raw.get("recording") or raw.get("recordable") or {}
    bucket = raw.get("bucket") or raw.get("project") or {}
    if not isinstance(creator, Mapping):
        creator = {}
    if not isinstance(recording, Mapping):
        recording = {}
    if not isinstance(bucket, Mapping):
        bucket = {}

    event_id = _identifier(raw.get("id"))
    person_id = _identifier(creator)
    project_id = _identifier(bucket) or _identifier(raw.get("bucket_id"))
    recording_id = _identifier(recording) or _identifier(raw.get("recording_id"))
    if not all((event_id, person_id, project_id, recording_id)):
        return None

    kind = str(raw.get("kind") or raw.get("action") or "event")
    person_name = str(creator.get("name") or "Basecamp member")
    title = recording.get("title") or recording.get("content") or raw.get("summary") or kind
    text = f"[Basecamp {kind}] {person_name}: {_plain_text(title)}".strip()

    parent = raw.get("parent") or {}
    parent_id = _identifier(parent) if isinstance(parent, Mapping) else ""
    record_type = str(recording.get("type") or raw.get("recordable_type") or "")
    bucket_type = str(bucket.get("type") or raw.get("bucket_type") or "")
    if bucket_type == "Circle":
        target_id = parent_id or recording_id
        chat_id = f"ping:{project_id}"
    elif record_type in {"Chat::Line", "Chat::Transcript"}:
        target_id = parent_id or recording_id
        chat_id = f"bucket:{project_id}/recording:{target_id}"
    else:
        target_id = parent_id or recording_id
        chat_id = f"bucket:{project_id}/recording:{target_id}"

    created = raw.get("created_at") or raw.get("createdAt")
    try:
        timestamp = datetime.fromisoformat(str(created))
    except (TypeError, ValueError):
        timestamp = datetime.now(UTC)

    return NormalizedEvent(
        event_id=event_id,
        kind=kind,
        text=text,
        person_id=person_id,
        person_name=person_name,
        project_id=project_id,
        recording_id=target_id,
        chat_id=chat_id,
        timestamp=timestamp,
        raw=raw,
    )


def is_addressed_to(raw: Mapping[str, Any], *, person_id: str, mention: str) -> bool:
    """Return true only for a structured Basecamp mention or assignment."""
    recording = raw.get("recording") or raw.get("recordable") or {}
    if not isinstance(recording, Mapping):
        recording = {}
    bucket = raw.get("bucket") or raw.get("project") or {}
    creator = raw.get("creator") or raw.get("person") or {}
    if (
        isinstance(bucket, Mapping)
        and str(bucket.get("type") or "") == "Circle"
        and isinstance(creator, Mapping)
    ):
        return creator.get("client") is False
    notification_section = str(raw.get("_notification_section") or "").lower()
    if notification_section == "mentions":
        return True
    if notification_section == "inbox" and str(recording.get("type") or "") == "Question":
        return True
    assignees = recording.get("assignees") or raw.get("assignees") or []
    if isinstance(assignees, list):
        for assignee in assignees:
            if _identifier(assignee) == person_id:
                return True

    del mention  # Display-only configuration. Authorization uses the person SGID.
    return any(
        person_id in _mentioned_person_ids(value)
        for value in (
            recording.get("content"),
            recording.get("title"),
            raw.get("summary"),
            raw.get("description"),
        )
    )


def parse_target(chat_id: str) -> tuple[str, str, str]:
    if re.fullmatch(r"recording:\d+", chat_id):
        return "recording", "", chat_id.split(":", 1)[1]
    if re.fullmatch(r"bucket:\d+", chat_id):
        return "bucket", chat_id.split(":", 1)[1], ""
    match = re.fullmatch(r"bucket:(\d+)/recording:(\d+)", chat_id)
    if match:
        return "recording", match.group(1), match.group(2)
    if re.fullmatch(r"ping:\d+", chat_id):
        return "ping", "", chat_id.split(":", 1)[1]
    raise ValueError(
        "Basecamp target must be recording:<id>, bucket:<id>, "
        "bucket:<id>/recording:<id>, or ping:<circle_id>"
    )
