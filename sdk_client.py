"""Runtime client built on the official Basecamp Python SDK."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:
    from basecamp import AsyncClient, Config
    from basecamp.async_auth import AsyncOAuthTokenProvider, AsyncStaticTokenProvider

    SDK_AVAILABLE = True
except ImportError:
    AsyncClient = None  # type: ignore[assignment,misc]
    Config = None  # type: ignore[assignment,misc]
    AsyncOAuthTokenProvider = None  # type: ignore[assignment,misc]
    AsyncStaticTokenProvider = None  # type: ignore[assignment,misc]
    SDK_AVAILABLE = False


class BasecampRuntimeError(RuntimeError):
    pass


class IdentityMismatchError(BasecampRuntimeError):
    pass


class OwnershipMismatchError(BasecampRuntimeError):
    pass


# Official webhook recording types for which SDK 0.16 has a project-bound,
# read-only getter keyed by the webhook recording ID.
SAFE_WEBHOOK_RECORDING_GETTERS: dict[str, tuple[str, str, str]] = {
    "Comment": ("comments", "get", "comment_id"),
    "Client::Forward": ("client_correspondences", "get", "correspondence_id"),
    "CloudFile": ("cloud_files", "get_cloud_file", "cloud_file_id"),
    "Document": ("documents", "get", "document_id"),
    "GoogleDocument": ("google_documents", "get_google_document", "google_document_id"),
    "Inbox::Forward": ("forwards", "get", "forward_id"),
    "Kanban::Card": ("cards", "get", "card_id"),
    "Kanban::Step": ("card_steps", "get", "step_id"),
    "Message": ("messages", "get", "message_id"),
    "Question": ("checkins", "get_question", "question_id"),
    "Question::Answer": ("checkins", "get_answer", "answer_id"),
    "Schedule::Entry": ("schedules", "get_entry", "entry_id"),
    "Todo": ("todos", "get", "todo_id"),
    "Todolist": ("todolists", "get", "id"),
    "Upload": ("uploads", "get", "upload_id"),
    "Vault": ("vaults", "get", "vault_id"),
}

UNSAFE_WEBHOOK_RECORDING_TYPES: dict[str, str] = {
    "Client::Approval::Response": "the SDK has no response getter keyed by the webhook recording ID",
    "Client::Reply": "the SDK getter requires an untrusted parent recording ID",
}

WEBHOOK_CANONICAL_TYPE_ALIASES = {"Client::Forward": "Client::Correspondence"}

NOTIFICATION_APP_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/buckets/(?P<bucket>\d+)/todos/(?P<recording>\d+)(?:$|[/?#])"), "Todo"),
    (re.compile(r"/buckets/(?P<bucket>\d+)/card_tables/cards/(?P<recording>\d+)(?:$|[/?#])"), "Kanban::Card"),
    (re.compile(r"/buckets/(?P<bucket>\d+)/messages/(?P<recording>\d+)(?:$|[/?#])"), "Message"),
    (re.compile(r"/buckets/(?P<bucket>\d+)/documents/(?P<recording>\d+)(?:$|[/?#])"), "Document"),
    (re.compile(r"/buckets/(?P<bucket>\d+)/questions/(?P<recording>\d+)(?:$|[/?#])"), "Question"),
    (re.compile(r"/buckets/(?P<bucket>\d+)/uploads/(?P<recording>\d+)(?:$|[/?#])"), "Upload"),
)
CHAT_LINE_APP_ROUTE = re.compile(
    r"/buckets/(?P<bucket>\d+)/chats/(?P<parent>\d+)@(?P<recording>\d+)(?:$|[/?#])"
)


def _notification_recording(item: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """Resolve an app notification to a typed SDK recording pointer."""
    app_url = str(item.get("app_url") or "")
    chat = CHAT_LINE_APP_ROUTE.search(app_url)
    if chat:
        return chat.group("bucket"), chat.group("recording"), "Chat::Line", chat.group("parent")
    for pattern, record_type in NOTIFICATION_APP_ROUTES:
        match = pattern.search(app_url)
        if match:
            return match.group("bucket"), match.group("recording"), record_type, ""
    return None


@dataclass(frozen=True)
class ExpectedIdentity:
    account_id: str
    person_id: str
    email: str


@dataclass(frozen=True)
class OAuthCredentials:
    access_token: str
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    expires_at: float | None = None

    @property
    def refreshable(self) -> bool:
        return all((self.refresh_token, self.client_id, self.client_secret))


class BasecampSDKClient:
    """Policy-neutral domain boundary around the official async SDK."""

    def __init__(
        self,
        *,
        expected: ExpectedIdentity,
        credentials: OAuthCredentials | None,
        project_ids: tuple[str, ...],
        token_provider=None,
        on_refresh=None,
    ) -> None:
        if not SDK_AVAILABLE:
            raise BasecampRuntimeError("basecamp-sdk>=0.16.0,<0.17 is not installed")
        if token_provider is None and (credentials is None or not credentials.access_token):
            raise BasecampRuntimeError("A Basecamp access token is required")
        if not project_ids or any(not item.isdigit() for item in project_ids):
            raise BasecampRuntimeError("At least one numeric Basecamp project ID is required")
        self.expected = expected
        self.project_ids = project_ids
        self._owned_recordings: dict[str, str] = {}
        if token_provider is not None:
            provider = token_provider
        elif credentials is not None and credentials.refreshable:
            provider = AsyncOAuthTokenProvider(
                credentials.access_token,
                credentials.client_id,
                credentials.client_secret,
                refresh_token=credentials.refresh_token,
                expires_at=credentials.expires_at,
                on_refresh=on_refresh,
            )
        elif credentials is not None:
            provider = AsyncStaticTokenProvider(credentials.access_token)
        config = Config(max_retries=3, timeout=30.0, base_delay=1.0, max_jitter=0.25)
        self._client = AsyncClient(
            token_provider=provider,
            config=config,
            user_agent="Hermes-Basecamp (+https://github.com/ScaleLean/hermes-basecamp)",
        )
        self._account = self._client.for_account(expected.account_id)

    async def close(self) -> None:
        await self._client.close()

    async def verify_identity(self) -> Mapping[str, Any]:
        profile = await self._account.people.my_profile()
        actual_id = str(profile.get("id") or "")
        actual_email = str(profile.get("email_address") or "").lower()
        if actual_id != self.expected.person_id or actual_email != self.expected.email.lower():
            raise IdentityMismatchError(
                "Basecamp identity mismatch: expected person "
                f"{self.expected.person_id} <{self.expected.email.lower()}>, got "
                f"{actual_id or 'missing'} <{actual_email or 'missing'}>"
            )
        return profile

    async def attest_full_member(self) -> Mapping[str, Any]:
        """Verify the dedicated member role and membership in every configured project."""
        profile = await self.verify_identity()
        if profile.get("employee") is not True or profile.get("client") is not False:
            raise IdentityMismatchError(
                "Basecamp identity is not an employee full member (employee=true, client=false)"
            )
        for project_id in self.project_ids:
            people = await self._account.people.list_for_project(project_id=int(project_id), max_items=500)
            matches = [
                person
                for person in people
                if isinstance(person, Mapping) and str(person.get("id") or "") == self.expected.person_id
            ]
            if len(matches) != 1:
                raise IdentityMismatchError(f"Basecamp member is not present in configured project {project_id}")
            member = matches[0]
            if member.get("employee") is not True or member.get("client") is not False:
                raise IdentityMismatchError(f"Basecamp member has the wrong role in configured project {project_id}")
        return profile

    async def call(self, service: str, method: str, arguments: Mapping[str, Any]) -> Any:
        """Invoke one reviewed SDK method without constructing raw API endpoints."""
        if service.startswith("_") or method.startswith("_"):
            raise BasecampRuntimeError("Private Basecamp SDK members are not callable")
        resource = getattr(self._account, service, None)
        function = getattr(resource, method, None) if resource is not None else None
        if function is None or not callable(function):
            raise BasecampRuntimeError(f"Unsupported Basecamp SDK operation: {service}.{method}")
        try:
            inspect.signature(function).bind(**dict(arguments))
        except TypeError as exc:
            raise BasecampRuntimeError(f"Invalid arguments for {service}.{method}: {exc}") from exc
        return await function(**dict(arguments))

    async def download_url(self, url: str) -> Any:
        """Download through the SDK's authenticated URL safety boundary."""
        return await self._account.download_url(url)

    def _require_owned(self, item: Mapping[str, Any], expected_project_id: str) -> Mapping[str, Any]:
        bucket = item.get("bucket") or item.get("project") or {}
        project_id = (
            str(bucket.get("id") or "")
            if isinstance(bucket, Mapping)
            else str(item.get("bucket_id") or item.get("project_id") or "")
        )
        if project_id != expected_project_id or project_id not in self.project_ids:
            raise OwnershipMismatchError("Basecamp canonical resource belongs to a different or denied project")
        recording_id = str(item.get("id") or "")
        if recording_id:
            owned_recordings = getattr(self, "_owned_recordings", None)
            if owned_recordings is None:
                owned_recordings = {}
                self._owned_recordings = owned_recordings
            owned_recordings[recording_id] = expected_project_id
        return item

    async def timeline(self, *, limit_per_project: int | None = None) -> list[Mapping[str, Any]]:
        events: list[Mapping[str, Any]] = []
        for project_id in self.project_ids:
            result = await self._account.timeline.get_project_timeline(
                project_id=int(project_id), max_items=limit_per_project
            )
            events.extend(
                {**item, "_stream_id": f"activity:{project_id}"}
                for item in result
                if isinstance(item, Mapping)
            )
        events.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return events

    async def campfires(self, *, max_items: int | None = None) -> list[Mapping[str, Any]]:
        result = await self._account.campfires.list(max_items=max_items)
        allowed = set(self.project_ids)
        return [
            item
            for item in result
            if isinstance(item, Mapping)
            and str((item.get("bucket") or {}).get("id") or item.get("bucket_id") or "") in allowed
        ]

    async def campfire_lines(
        self,
        *,
        project_id: str,
        campfire_id: str,
        max_items: int | None = None,
    ) -> list[Mapping[str, Any]]:
        if project_id not in self.project_ids or not campfire_id.isdigit():
            raise BasecampRuntimeError("Basecamp Campfire is outside the project allowlist")
        result = await self._account.campfires.list_lines(
            campfire_id=int(campfire_id),
            sort="created_at",
            direction="desc",
            max_items=max_items,
        )
        events: list[Mapping[str, Any]] = []
        for line in result:
            if not isinstance(line, Mapping):
                continue
            events.append(
                {
                    "id": line.get("id"),
                    "kind": "chat_line_created",
                    "created_at": line.get("created_at"),
                    "creator": line.get("creator"),
                    "bucket": {"id": project_id},
                    "recording": {**line, "type": "Chat::Line"},
                    "parent": {"id": campfire_id},
                    "_stream_id": f"campfire:{project_id}:{campfire_id}",
                }
            )
        return events

    async def notifications(self) -> list[Mapping[str, Any]]:
        payload = await self._account.my_notifications.get_my_notifications(limit_bubble_ups=True)
        events: list[Mapping[str, Any]] = []
        for key in ("unreads", "reads", "bubble_ups", "scheduled_bubble_ups"):
            values = payload.get(key) or []
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, Mapping):
                        continue
                    location = re.search(
                        r"/buckets/(?P<bucket>\d+)/recordings/(?P<recording>\d+)/",
                        str(item.get("subscription_url") or ""),
                    )
                    section = str(item.get("section") or "").lower()
                    if section == "pings":
                        if location is None:
                            continue
                        bucket_id = location.group("bucket")
                        recording_id = location.group("recording")
                        lines = await self._account.campfires.list_lines(
                            campfire_id=int(recording_id),
                            sort="created_at",
                            direction="desc",
                            max_items=100,
                        )
                        for line in lines:
                            if not isinstance(line, Mapping):
                                continue
                            events.append(
                                {
                                    "id": line.get("id"),
                                    "kind": "ping_line_created",
                                    "created_at": line.get("created_at"),
                                    "creator": line.get("creator"),
                                    "bucket": {"id": bucket_id, "type": "Circle"},
                                    "recording": {**line, "type": "Chat::Line"},
                                    "parent": {"id": recording_id, "type": "Chat::Transcript"},
                                    "participants": item.get("participants") or [],
                                    "_stream_id": f"ping:{bucket_id}",
                                }
                            )
                        continue
                    resolved = _notification_recording(item)
                    if resolved is None:
                        continue
                    bucket_id, recording_id, recording_type, parent_id = resolved
                    if bucket_id not in self.project_ids:
                        continue
                    event = {
                        "id": item.get("id"),
                        "kind": f"notification_{item.get('section') or 'inbox'}",
                        "created_at": item.get("updated_at") or item.get("created_at"),
                        "creator": item.get("creator"),
                        "bucket": {"id": bucket_id, "type": "Project"},
                        "recording": {"id": recording_id, "type": recording_type},
                        "_stream_id": "notifications",
                        "_notification_section": item.get("section"),
                    }
                    if parent_id:
                        event["parent"] = {"id": parent_id, "type": "Chat::Transcript"}
                    events.append(event)
        return events

    async def assignments(self) -> list[Mapping[str, Any]]:
        """Return active assignments as normalized synthetic activity events."""
        payload = await self._account.my_assignments.get_my_assignments()
        values: list[Mapping[str, Any]] = []
        for key in ("priorities", "non_priorities"):
            group = payload.get(key) or []
            if isinstance(group, list):
                values.extend(item for item in group if isinstance(item, Mapping))
        events: list[Mapping[str, Any]] = []
        for item in values:
            bucket = item.get("bucket") or {}
            project_id = str(bucket.get("id") or "") if isinstance(bucket, Mapping) else ""
            if project_id not in self.project_ids:
                continue
            recording_id = str(item.get("id") or "")
            if not recording_id:
                continue
            updated_at = item.get("updated_at") or item.get("created_at") or item.get("due_on") or ""
            raw_type = str(item.get("type") or "")
            canonical_type = {
                "todo": "Todo",
                "card": "Kanban::Card",
                "kanban::card": "Kanban::Card",
                "step": "Kanban::Step",
                "kanban::step": "Kanban::Step",
            }.get(raw_type.lower(), raw_type)
            canonical = await self._get_webhook_recording(canonical_type, int(recording_id))
            if canonical is None:
                continue
            canonical = self._require_owned(canonical, project_id)
            updated_at = (
                canonical.get("updated_at")
                or canonical.get("created_at")
                or canonical.get("due_on")
                or updated_at
            )
            events.append(
                {
                    "id": f"assignment:{recording_id}:{updated_at}",
                    "kind": "assignment_created",
                    "created_at": updated_at,
                    "creator": canonical.get("creator") or item.get("creator") or {},
                    "bucket": dict(bucket),
                    "recording": {**canonical, "type": canonical_type},
                    "assignees": canonical.get("assignees") or item.get("assignees") or [],
                    "_stream_id": "assignments",
                }
            )
        return events

    async def fetch_recording(self, raw_event: Mapping[str, Any]) -> Mapping[str, Any] | None:
        recording = raw_event.get("recording") or raw_event.get("recordable") or {}
        parent = raw_event.get("parent") or {}
        if not isinstance(recording, Mapping):
            return None
        recording_id = int(recording.get("id") or 0)
        record_type = str(recording.get("type") or "")
        if not recording_id:
            return None
        bucket = raw_event.get("bucket") or raw_event.get("project") or {}
        expected_project_id = (
            str(bucket.get("id") or raw_event.get("bucket_id") or "") if isinstance(bucket, Mapping) else ""
        )
        bucket_type = str(bucket.get("type") or "") if isinstance(bucket, Mapping) else ""
        if bucket_type == "Circle" and record_type == "Chat::Line":
            parent_id = int(parent.get("id") or 0) if isinstance(parent, Mapping) else 0
            if not parent_id:
                return None
            ping_result = await self._account.campfires.get_line(campfire_id=parent_id, line_id=recording_id)
            result_bucket = ping_result.get("bucket") or {} if isinstance(ping_result, Mapping) else {}
            if (
                not isinstance(ping_result, Mapping)
                or not isinstance(result_bucket, Mapping)
                or str(result_bucket.get("id") or "") != expected_project_id
                or str(result_bucket.get("type") or "") != "Circle"
            ):
                raise OwnershipMismatchError("Basecamp Ping line does not belong to the expected Circle")
            return ping_result
        if expected_project_id not in self.project_ids:
            raise OwnershipMismatchError("Basecamp event project is not allowlisted")
        result: Mapping[str, Any] | None = await self._get_webhook_recording(record_type, recording_id)
        if record_type == "Chat::Line":
            parent_id = int(parent.get("id") or 0) if isinstance(parent, Mapping) else 0
            result = (
                await self._account.campfires.get_line(campfire_id=parent_id, line_id=recording_id)
                if parent_id
                else None
            )
        return self._require_owned(result, expected_project_id) if result is not None else None

    async def _get_webhook_recording(self, record_type: str, recording_id: int) -> Mapping[str, Any] | None:
        lookup_type = "Client::Forward" if record_type == "Client::Correspondence" else record_type
        getter = SAFE_WEBHOOK_RECORDING_GETTERS.get(lookup_type)
        if getter is None:
            return None
        service_name, method_name, argument_name = getter
        result = await getattr(getattr(self._account, service_name), method_name)(**{argument_name: recording_id})
        return result if isinstance(result, Mapping) else None

    async def fetch_webhook_event(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Authenticate a webhook pointer and rebuild all dispatch fields canonically."""
        raw_recording = payload.get("recording") or {}
        if not isinstance(raw_recording, Mapping):
            return None
        try:
            event_id = int(payload.get("id") or 0)
            recording_id = int(raw_recording.get("id") or 0)
        except (TypeError, ValueError):
            return None
        record_type = str(raw_recording.get("type") or "")
        getter = SAFE_WEBHOOK_RECORDING_GETTERS.get(record_type)
        if not event_id or not recording_id or getter is None:
            return None

        events = await self._account.events.list(recording_id=recording_id, max_items=None)
        matches = [
            event
            for event in events
            if isinstance(event, Mapping)
            and str(event.get("id") or "") == str(event_id)
            and str(event.get("recording_id") or "") == str(recording_id)
        ]
        if len(matches) != 1:
            return None
        canonical_event = matches[0]
        creator = canonical_event.get("creator") or {}
        created_at = canonical_event.get("created_at")
        action = str(canonical_event.get("action") or "")
        if not isinstance(creator, Mapping) or not creator.get("id") or not created_at or not action:
            return None

        canonical_recording = await self._get_webhook_recording(record_type, recording_id)
        if not isinstance(canonical_recording, Mapping):
            return None
        if str(canonical_recording.get("id") or "") != str(recording_id):
            return None
        canonical_type = str(canonical_recording.get("type") or "")
        expected_canonical_type = WEBHOOK_CANONICAL_TYPE_ALIASES.get(record_type, record_type)
        if canonical_type != expected_canonical_type:
            return None
        bucket = canonical_recording.get("bucket") or {}
        project_id = str(bucket.get("id") or "") if isinstance(bucket, Mapping) else ""
        if project_id not in self.project_ids:
            return None
        self._require_owned(canonical_recording, project_id)
        parent = canonical_recording.get("parent") or {}
        if not isinstance(parent, Mapping):
            parent = {}
        kind_prefix = re.sub(r"(?<!^)(?=[A-Z])", "_", canonical_type.replace("::", "")).lower()
        return {
            "id": event_id,
            "kind": str(canonical_event.get("kind") or f"{kind_prefix}_{action}"),
            "action": action,
            "created_at": created_at,
            "creator": dict(creator),
            "details": dict(canonical_event.get("details") or {})
            if isinstance(canonical_event.get("details"), Mapping)
            else {},
            "recording": dict(canonical_recording),
            "bucket": dict(bucket),
            "parent": dict(parent),
        }

    async def post_chat(self, project_id: str, room_id: str, content: str) -> Mapping[str, Any]:
        room = await self._account.campfires.get(campfire_id=int(room_id))
        bucket = room.get("bucket") or {}
        actual_id = str(bucket.get("id") or "") if isinstance(bucket, Mapping) else ""
        bucket_type = str(bucket.get("type") or "") if isinstance(bucket, Mapping) else ""
        if actual_id != project_id:
            raise OwnershipMismatchError("Basecamp chat belongs to a different bucket")
        if bucket_type != "Circle" and project_id not in self.project_ids:
            raise OwnershipMismatchError("Basecamp Campfire project is not allowlisted")
        return await self._account.campfires.create_line(
            campfire_id=int(room_id), content=content, content_type="text/html"
        )

    async def resolve_target(
        self, recording_id: str, expected_project_id: str = ""
    ) -> tuple[str, str]:
        """Resolve an official recording target to its typed write surface and project."""
        if not recording_id.isdigit():
            raise OwnershipMismatchError("Basecamp recording target must be numeric")
        cached_project = getattr(self, "_owned_recordings", {}).get(recording_id, "")
        if cached_project:
            if expected_project_id and cached_project != expected_project_id:
                raise OwnershipMismatchError("Basecamp recording target is outside the requested project")
            return "recording", cached_project
        try:
            room = await self._account.campfires.get(campfire_id=int(recording_id))
        except Exception as exc:
            status = (
                getattr(exc, "status_code", None)
                or getattr(exc, "status", None)
                or getattr(exc, "http_status", None)
            )
            if status != 404 and getattr(exc, "code", None) != "not_found":
                raise
        else:
            bucket = room.get("bucket") or {} if isinstance(room, Mapping) else {}
            project_id = str(bucket.get("id") or "") if isinstance(bucket, Mapping) else ""
            if project_id not in self.project_ids or (expected_project_id and project_id != expected_project_id):
                raise OwnershipMismatchError("Basecamp Campfire target is outside the requested project")
            return "chat", project_id

        attempted: set[tuple[str, str, str]] = set()
        for service_name, method_name, argument_name in SAFE_WEBHOOK_RECORDING_GETTERS.values():
            signature = (service_name, method_name, argument_name)
            if signature in attempted:
                continue
            attempted.add(signature)
            try:
                result = await getattr(getattr(self._account, service_name), method_name)(
                    **{argument_name: int(recording_id)}
                )
            except Exception as exc:
                status = (
                    getattr(exc, "status_code", None)
                    or getattr(exc, "status", None)
                    or getattr(exc, "http_status", None)
                )
                if status == 404 or getattr(exc, "code", None) == "not_found":
                    continue
                raise
            if not isinstance(result, Mapping) or str(result.get("id") or "") != recording_id:
                continue
            bucket = result.get("bucket") or {}
            project_id = str(bucket.get("id") or "") if isinstance(bucket, Mapping) else ""
            if project_id not in self.project_ids or (expected_project_id and project_id != expected_project_id):
                raise OwnershipMismatchError("Basecamp recording target is outside the requested project")
            self._require_owned(result, project_id)
            return "recording", project_id
        raise OwnershipMismatchError("Basecamp recording target could not be resolved through the SDK")

    async def post_comment(self, project_id: str, recording_id: str, content: str) -> Mapping[str, Any]:
        if project_id not in self.project_ids:
            raise OwnershipMismatchError("Basecamp recording project is not allowlisted")
        if getattr(self, "_owned_recordings", {}).get(recording_id) != project_id:
            events = await self._account.events.list(recording_id=int(recording_id), max_items=100)
            if not events or any(
                not isinstance(event, Mapping)
                or str((event.get("bucket") or {}).get("id") or event.get("bucket_id") or "") != project_id
                for event in events
            ):
                raise OwnershipMismatchError("Basecamp recording event history does not prove project ownership")
        return await self._account.comments.create(recording_id=int(recording_id), content=content)

    async def verify_chat_authorship(self, room_id: str, line_id: str) -> Mapping[str, Any]:
        item = await self._account.campfires.get_line(campfire_id=int(room_id), line_id=int(line_id))
        return self._verify_creator(item)

    async def verify_comment_authorship(self, comment_id: str) -> Mapping[str, Any]:
        return self._verify_creator(await self._account.comments.get(comment_id=int(comment_id)))

    def _verify_creator(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        creator = item.get("creator") or {}
        creator_id = str(creator.get("id") or "") if isinstance(creator, Mapping) else ""
        if creator_id != self.expected.person_id:
            raise IdentityMismatchError(
                f"Basecamp write attribution mismatch: expected creator {self.expected.person_id}, "
                f"got {creator_id or 'missing'}"
            )
        return item

    def verify_creator(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        """Verify that a canonical read-back was authored by this agent member."""
        return self._verify_creator(item)
