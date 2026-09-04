"""Operation-specific postcondition verification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from policy import Capability
from resource_index import OwnershipError, folder_project_ids, project_id_from


class VerificationError(RuntimeError):
    pass


AUTHORED_CREATE_CAPABILITIES = frozenset(
    {
        "campfire.post",
        "card_columns.create",
        "cards.create",
        "cards.steps.create",
        "checkins.answer",
        "checkins.question.create",
        "cloud_files.create",
        "comments.create",
        "dock_tools.create",
        "documents.create",
        "gauges.create",
        "google_documents.create",
        "messages.create",
        "schedules.create",
        "timesheets.create",
        "todolists.create",
        "todos.create",
        "todos.create_loose",
        "uploads.create",
        "uploads.version",
        "vaults.create",
        "wormholes.create",
    }
)

STRUCTURAL_CREATE_EXEMPTIONS = frozenset(
    {
        "bookmarks.create",
        "folders.create",
        "message_types.create",
        "todolist_groups.create",
    }
)

ALTERNATE_AUTHOR_CREATE_FIELDS = {
    "boosts.create": "booster",
    "boosts.event.create": "booster",
}


class VerificationClient(Protocol):
    expected: Any

    async def call(self, service: str, method: str, arguments: Mapping[str, Any]) -> Any: ...


class PostconditionVerifier:
    def __init__(self, client: VerificationClient) -> None:
        self.client = client

    async def verify(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        result: Any,
        project_id: str,
        *,
        precondition: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if capability.service == "recordings":
            return await self._verify_recording_event(capability, arguments, project_id, precondition)
        if capability.name == "people.project_access":
            people = await self.client.call(
                "people", "list_for_project", {"project_id": int(project_id), "max_items": 500}
            )
            member_ids = {str(person.get("id")) for person in people if isinstance(person, Mapping)}
            grants = {str(value) for value in (arguments.get("grant") or [])}
            revokes = {str(value) for value in (arguments.get("revoke") or [])}
            if not grants.issubset(member_ids) or revokes.intersection(member_ids):
                raise VerificationError("Basecamp project access read-back does not match requested state")
            return {"verified": True, "member_ids": sorted(member_ids)}
        if isinstance(result, Mapping) and result.get("id") is not None:
            canonical = await self._read_back(capability, arguments, result)
            canonical_project = project_id_from(canonical)
            if canonical_project and canonical_project != project_id:
                raise OwnershipError("Basecamp read-back belongs to another project")
            if capability.name == "folders.create":
                requested_projects = {str(item) for item in arguments.get("project_ids") or []}
                if requested_projects != {project_id} or folder_project_ids(canonical) != requested_projects:
                    raise VerificationError(
                        "Basecamp folder read-back does not match exact requested project membership"
                    )
            if capability.name in AUTHORED_CREATE_CAPABILITIES:
                creator = canonical.get("creator")
                if not isinstance(creator, Mapping) or str(creator.get("id") or "") != str(
                    self.client.expected.person_id
                ):
                    raise VerificationError("Basecamp authored create read-back is missing expected agent authorship")
            alternate_author = ALTERNATE_AUTHOR_CREATE_FIELDS.get(capability.name)
            if alternate_author is not None:
                creator = canonical.get(alternate_author)
                if not isinstance(creator, Mapping) or str(creator.get("id") or "") != str(
                    self.client.expected.person_id
                ):
                    raise VerificationError("Basecamp create read-back is missing expected agent attribution")
            self._assert_requested_state(capability, arguments, canonical)
            return {"verified": True, "canonical": canonical}
        if result is None:
            return await self._verify_void(capability, arguments, project_id)
        raise VerificationError(f"Unsupported Basecamp mutation response shape for {capability.name}")

    async def _read_back(
        self, capability: Capability, arguments: Mapping[str, Any], result: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        item_id = int(str(result["id"]))
        service = capability.service
        method = "get"
        argument = f"{service.rstrip('s')}_id"
        overrides = {
            "campfires": ("campfires", "get_line", "line_id"),
            "boosts": ("boosts", "get_boost", "boost_id"),
            "gauges": ("gauges", "get_gauge_needle", "needle_id"),
            "timesheets": ("timesheets", "get", "entry_id"),
            "card_steps": ("card_steps", "get", "step_id"),
            "folders": ("folders", "get_folder", "folder_id"),
            "card_columns": ("card_columns", "get", "column_id"),
            "cloud_files": ("cloud_files", "get_cloud_file", "cloud_file_id"),
            "google_documents": ("google_documents", "get_google_document", "google_document_id"),
            "tools": ("tools", "get", "tool_id"),
            "todolists": ("todolists", "get", "id"),
            "vaults": ("vaults", "get", "vault_id"),
        }
        service, method, argument = overrides.get(service, (service, method, argument))
        if capability.service == "checkins":
            method, argument = (
                ("get_question", "question_id")
                if "question" in capability.name or capability.name in {"checkins.pause", "checkins.resume"}
                else ("get_answer", "answer_id")
            )
        if capability.service == "schedules" and capability.name == "schedules.create":
            method, argument = "get_entry", "entry_id"
        if capability.service == "calendars":
            method, argument = "get_calendar", "calendar_id"
        if capability.service == "message_types":
            service, method, argument = "message_types", "get", "type_id"
            message_type_args = {
                "bucket_id": int(str(arguments["bucket_id"])),
                "type_id": item_id,
            }
            value = await self.client.call(service, method, message_type_args)
            if not isinstance(value, Mapping):
                raise VerificationError("Basecamp mutation read-back is not an object")
            return value
        if capability.service == "todolist_groups":
            groups = await self.client.call(
                "todolist_groups",
                "list",
                {"todolist_id": int(str(arguments["todolist_id"])), "max_items": 500},
            )
            match = next(
                (entry for entry in groups if isinstance(entry, Mapping) and entry.get("id") == item_id),
                None,
            )
            if match is None:
                raise VerificationError("Basecamp to-do group read-back did not find the created group")
            return match
        if capability.service == "wormholes":
            table = await self.client.call(
                "card_tables", "get", {"card_table_id": int(str(arguments["card_table_id"]))}
            )
            wormholes = table.get("wormholes") if isinstance(table, Mapping) else None
            match = next(
                (entry for entry in (wormholes or []) if isinstance(entry, Mapping) and entry.get("id") == item_id),
                None,
            )
            if match is None:
                raise VerificationError("Basecamp wormhole read-back did not find the created wormhole")
            destination_id = arguments.get("destination_recording_id")
            if destination_id is not None and str(destination_id) not in str(match.get("destination_url") or ""):
                raise VerificationError("Basecamp wormhole destination does not match the request")
            return match
        if capability.service == "subscriptions":
            method, argument = "get", "recording_id"
            item_id = int(str(arguments["recording_id"]))
        if capability.service == "bookmarks":
            method, argument = "get_bookmark", "recording_id"
            item_id = int(str(arguments["recording_id"]))
        if capability.service == "hill_charts":
            method, argument = "get", "todoset_id"
            item_id = int(str(arguments["todoset_id"]))
        read_args: dict[str, Any] = {argument: item_id}
        if service == "campfires":
            read_args["campfire_id"] = int(str(arguments["campfire_id"]))
        value = await self.client.call(service, method, read_args)
        if not isinstance(value, Mapping):
            raise VerificationError("Basecamp mutation read-back is not an object")
        return value

    async def _verify_recording_event(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        project_id: str,
        precondition: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        prior_ids = precondition.get("pre_event_ids") if isinstance(precondition, Mapping) else None
        if not isinstance(prior_ids, list) or not all(isinstance(item, str) and item for item in prior_ids):
            raise VerificationError("Basecamp recording mutation lacks a canonical pre-mutation event snapshot")
        prior_id_set = set(prior_ids)
        events = await self.client.call(
            "events", "list", {"recording_id": int(str(arguments["recording_id"])), "max_items": 20}
        )
        expected_actions = {
            "recordings.archive": {"archived"},
            "recordings.restore": {"unarchived", "restored"},
            "recordings.trash": {"trashed"},
            "recordings.spotlight": {"spotlighted"},
            "recordings.unspotlight": {"unspotlighted"},
        }[capability.name]
        matching = [
            event
            for event in events
            if isinstance(event, Mapping)
            and str(event.get("recording_id") or "") == str(arguments["recording_id"])
            and str(event.get("action") or "").lower() in expected_actions
            and str(event.get("id") or "") not in prior_id_set
        ]
        if not matching:
            raise VerificationError("Basecamp event history does not confirm the recording transition")
        event = matching[0]
        if project_id_from(event) != project_id:
            raise OwnershipError("Basecamp recording transition belongs to another project")
        creator = event.get("creator")
        if not isinstance(creator, Mapping) or str(creator.get("id") or "") != self.client.expected.person_id:
            raise VerificationError("Basecamp recording transition has unexpected authorship")
        return {"verified": True, "event": event}

    def _assert_requested_state(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        canonical: Mapping[str, Any],
    ) -> None:
        handled: set[str] = set()
        nested_request_fields = {
            "calendars.update": ("calendar",),
            "gauges.create": ("gauge_needle",),
            "gauges.update": ("gauge_needle",),
        }
        for argument in nested_request_fields.get(capability.name, ()):
            requested = arguments.get(argument)
            if not isinstance(requested, Mapping):
                raise VerificationError(f"Basecamp {capability.name} requires an object-valued {argument}")
            for field, expected in requested.items():
                if field not in canonical or canonical[field] != expected:
                    raise VerificationError(
                        f"Basecamp {capability.name} read-back is missing or does not match {argument}.{field}"
                    )
            handled.add(argument)

        if capability.name == "projects.update" and arguments.get("schedule_attributes") is not None:
            schedule = arguments["schedule_attributes"]
            if not isinstance(schedule, Mapping):
                raise VerificationError("Basecamp projects.update schedule_attributes must be an object")
            for field in ("start_date", "end_date"):
                if field not in schedule or canonical.get(field) != schedule[field]:
                    raise VerificationError(
                        f"Basecamp projects.update read-back is missing or does not match schedule_attributes.{field}"
                    )
            handled.add("schedule_attributes")

        if capability.name == "subscriptions.update":
            subscribers = canonical.get("subscribers")
            if not isinstance(subscribers, list):
                raise VerificationError("Basecamp subscriptions.update read-back is missing subscribers")
            subscriber_ids = {
                int(str(item["id"])) for item in subscribers if isinstance(item, Mapping) and item.get("id") is not None
            }
            additions = {int(str(item)) for item in arguments.get("subscriptions") or []}
            removals = {int(str(item)) for item in arguments.get("unsubscriptions") or []}
            if not additions.issubset(subscriber_ids) or removals & subscriber_ids:
                raise VerificationError("Basecamp subscriptions.update read-back does not match requested membership")
            handled.update({"subscriptions", "unsubscriptions"})

        if capability.name == "hillcharts.update":
            dots = canonical.get("dots")
            if not isinstance(dots, list):
                raise VerificationError("Basecamp hillcharts.update read-back is missing tracked dots")
            tracked_ids = {
                int(str(item["id"])) for item in dots if isinstance(item, Mapping) and item.get("id") is not None
            }
            additions = {int(str(item)) for item in arguments.get("tracked") or []}
            removals = {int(str(item)) for item in arguments.get("untracked") or []}
            if not additions.issubset(tracked_ids) or removals & tracked_ids:
                raise VerificationError("Basecamp hillcharts.update read-back does not match requested tracking")
            handled.update({"tracked", "untracked"})

        if capability.name in {"wormholes.create", "wormholes.update"}:
            handled.add("destination_recording_id")
        if capability.name == "folders.create":
            handled.add("project_ids")
        exact_state: tuple[str, Any] | None = None
        if capability.name in {"messages.pin", "messages.unpin"}:
            exact_state = ("pinned", capability.name == "messages.pin")
        elif capability.name in {"checkins.pause", "checkins.resume"}:
            exact_state = ("paused", capability.name == "checkins.pause")
        elif capability.name in {"dock_tools.enable", "dock_tools.disable"}:
            exact_state = ("enabled", capability.name == "dock_tools.enable")
        elif capability.name == "dock_tools.reposition":
            exact_state = ("position", arguments.get("position"))
        elif capability.name == "schedules.settings":
            exact_state = (
                "include_due_assignments",
                arguments.get("include_due_assignments"),
            )
        if exact_state is not None:
            field, expected = exact_state
            if field not in canonical or canonical[field] != expected:
                raise VerificationError(f"Basecamp {capability.name} read-back is missing or does not match {field}")
        if capability.name == "card_columns.hold.enable" and not isinstance(canonical.get("on_hold"), Mapping):
            raise VerificationError("Basecamp card-column read-back does not confirm on-hold enabled")
        if capability.name == "card_columns.hold.disable" and canonical.get("on_hold") is not None:
            raise VerificationError("Basecamp card-column read-back does not confirm on-hold disabled")
        aliases = {
            "assignee_ids": "assignees",
            "base_name": "filename",
            "category_id": "category",
            "completion_subscriber_ids": "completion_subscribers",
            "column_id": "parent",
            "participant_ids": "participants",
            "parent_id": "parent",
            "person_id": "person",
            "project_ids": "bucket_ids",
            "subscriptions": "subscribers",
            "tool_type": "name",
        }
        for argument, expected in arguments.items():
            if argument in handled:
                continue
            routing = {
                capability.project_argument,
                capability.owner_argument,
                *capability.context_arguments,
            }
            if argument in routing or argument in {
                "attachable_sgid",
                "content_type",
                "notify",
            }:
                continue
            field = aliases.get(argument, argument)
            if field not in canonical:
                raise VerificationError(f"Basecamp {capability.name} read-back is missing requested {argument}")
            actual = canonical[field]
            if argument in {
                "assignee_ids",
                "completion_subscriber_ids",
                "participant_ids",
                "project_ids",
                "subscriptions",
            } and isinstance(actual, list):
                actual = [entry.get("id") for entry in actual if isinstance(entry, Mapping)]
            elif argument in {
                "category_id",
                "column_id",
                "parent_id",
                "person_id",
            } and isinstance(actual, Mapping):
                actual = actual.get("id")
            elif argument == "completion":
                expected = expected == "on"
            elif argument == "service" and isinstance(actual, Mapping):
                actual = actual.get("code")
            elif argument == "url" and capability.service == "schedules":
                actual = canonical.get("join_url")
            if actual != expected:
                raise VerificationError(f"Basecamp {capability.name} read-back does not match requested {argument}")

    async def _verify_void(
        self, capability: Capability, arguments: Mapping[str, Any], project_id: str
    ) -> Mapping[str, Any]:
        lookup: tuple[str, str, str] | None = None
        if capability.name == "todolists.reposition":
            value = await self.client.call("todolists", "get", {"id": int(str(arguments["todolist_id"]))})
            if not isinstance(value, Mapping):
                raise VerificationError("Basecamp to-do list read-back is not an object")
            self._assert_requested_state(capability, arguments, value)
            return {"verified": True, "canonical": value}
        if capability.name == "todolist_groups.reposition":
            groups = await self.client.call(
                "todolist_groups",
                "list",
                {"todolist_id": int(str(arguments["todolist_id"])), "max_items": 500},
            )
            group = next(
                (
                    item
                    for item in groups
                    if isinstance(item, Mapping) and str(item.get("id") or "") == str(arguments["group_id"])
                ),
                None,
            )
            if group is None or group.get("position") != arguments["position"]:
                raise VerificationError("Basecamp to-do group position does not match the request")
            return {"verified": True, "group": group}
        if capability.name == "cards.steps.reposition":
            value = await self.client.call("card_steps", "get", {"step_id": int(str(arguments["source_id"]))})
            if not isinstance(value, Mapping):
                raise VerificationError("Basecamp card-step read-back is not an object")
            self._assert_requested_state(capability, arguments, value)
            return {"verified": True, "canonical": value}
        if capability.name == "card_columns.move":
            table = await self.client.call(
                "card_tables", "get", {"card_table_id": int(str(arguments["card_table_id"]))}
            )
            columns = table.get("lists") if isinstance(table, Mapping) else None
            source = next(
                (
                    item
                    for item in (columns or [])
                    if isinstance(item, Mapping) and str(item.get("id") or "") == str(arguments["source_id"])
                ),
                None,
            )
            target = next(
                (
                    item
                    for item in (columns or [])
                    if isinstance(item, Mapping) and str(item.get("id") or "") == str(arguments["target_id"])
                ),
                None,
            )
            positioned = [
                item for item in (columns or []) if isinstance(item, Mapping) and isinstance(item.get("position"), int)
            ]
            positioned.sort(key=lambda item: int(item["position"]))
            expected_position = int(arguments.get("position") or 1)
            source_index = next(
                (
                    index
                    for index, item in enumerate(positioned)
                    if str(item.get("id") or "") == str(arguments["source_id"])
                ),
                -1,
            )
            if (
                source is None
                or target is None
                or source is target
                or not isinstance(target.get("position"), int)
                or source.get("position") != expected_position
                or source_index != expected_position - 1
            ):
                raise VerificationError("Basecamp card-column order does not match the requested move")
            return {
                "verified": True,
                "card_table": table,
                "column": source,
                "target": target,
            }
        if capability.name in {"card_columns.subscribe", "card_columns.unsubscribe"}:
            subscription = await self.client.call(
                "subscriptions", "get", {"recording_id": int(str(arguments["column_id"]))}
            )
            expected = capability.name == "card_columns.subscribe"
            if not isinstance(subscription, Mapping) or subscription.get("subscribed") is not expected:
                raise VerificationError("Basecamp card-column subscription does not match the request")
            return {"verified": True, "canonical": subscription}
        if capability.name == "wormholes.delete":
            table = await self.client.call(
                "card_tables", "get", {"card_table_id": int(str(arguments["card_table_id"]))}
            )
            wormholes = table.get("wormholes") if isinstance(table, Mapping) else None
            if any(
                isinstance(item, Mapping) and str(item.get("id") or "") == str(arguments["wormhole_id"])
                for item in (wormholes or [])
            ):
                raise VerificationError("Basecamp wormhole still exists after deletion")
            return {"verified": True, "card_table": table, "absent": True}
        if capability.name == "message_types.delete":
            try:
                await self.client.call(
                    "message_types",
                    "get",
                    {
                        "bucket_id": int(str(arguments["bucket_id"])),
                        "type_id": int(str(arguments["type_id"])),
                    },
                )
            except Exception as exc:
                status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                if status == 404:
                    return {"verified": True, "absent": True}
                raise
            raise VerificationError("Basecamp message type still exists after deletion")
        if capability.service == "projects":
            lookup = ("projects", "get", "project_id")
        elif capability.service == "todos":
            lookup = ("todos", "get", "todo_id")
        elif capability.service == "cards":
            lookup = ("cards", "get", "card_id")
        elif capability.service == "campfires" and arguments.get("line_id"):
            lookup = ("campfires", "get_line", "line_id")
        elif capability.service == "timesheets":
            lookup = ("timesheets", "get", "entry_id")
        elif capability.service == "bookmarks":
            lookup = ("bookmarks", "get_bookmark", "recording_id")
        elif capability.name == "folders.delete":
            lookup = ("folders", "get_folder", "folder_id")
        elif capability.service == "subscriptions":
            lookup = ("subscriptions", "get", "recording_id")
        elif capability.service == "gauges" and capability.name == "gauges.toggle":
            value = await self.client.call("gauges", "list_gauges", {"bucket_ids": project_id, "max_items": 200})
            gauge_request = arguments.get("gauge")
            expected_enabled = gauge_request.get("enabled") if isinstance(gauge_request, Mapping) else None
            matches = [
                item
                for item in value
                if isinstance(item, Mapping)
                and project_id_from(item) == project_id
                and "enabled" in item
                and item.get("enabled") is expected_enabled
            ]
            if not isinstance(expected_enabled, bool) or len(matches) != 1:
                raise VerificationError("Basecamp gauge read-back does not exactly match requested enabled state")
            return {"verified": True, "gauge": matches[0]}
        elif capability.service == "webhooks":
            lookup = ("webhooks", "get", "webhook_id")
        elif capability.service == "tools":
            lookup = ("tools", "get", "tool_id")
        elif capability.service == "card_columns":
            lookup = ("card_columns", "get", "column_id")
        elif capability.service == "my_assignments":
            assignments = await self.client.call("my_assignments", "get_my_assignments", {})
            priorities = assignments.get("priorities") if isinstance(assignments, Mapping) else None
            non_priorities = assignments.get("non_priorities") if isinstance(assignments, Mapping) else None
            priority_ids = [str(item.get("id") or "") for item in (priorities or []) if isinstance(item, Mapping)]
            non_priority_ids = {
                str(item.get("id") or "") for item in (non_priorities or []) if isinstance(item, Mapping)
            }
            if capability.name == "assignments.prioritize":
                valid = str(arguments["id"]) in priority_ids
            elif capability.name == "assignments.deprioritize":
                valid = str(arguments["recording_id"]) in non_priority_ids
            else:
                source_id_text = str(arguments["source_id"])
                position = int(arguments["position"])
                valid = 0 <= position < len(priority_ids) and priority_ids[position] == source_id_text
            if not valid:
                raise VerificationError("Basecamp assignment priority read-back does not match the request")
            return {"verified": True, "assignments": assignments}
        elif capability.owner_service and capability.owner_method and capability.owner_argument:
            lookup = (capability.owner_service, capability.owner_method, capability.owner_argument)
        if lookup is None:
            raise VerificationError(f"No read-back is defined for void mutation {capability.name}")
        service, method, argument = lookup
        read_args = {argument: arguments[argument]}
        if service == "campfires":
            read_args["campfire_id"] = arguments["campfire_id"]
        absent_expected = any(word in capability.name for word in ("delete", "trash"))
        try:
            value = await self.client.call(service, method, read_args)
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if absent_expected and status == 404:
                return {"verified": True, "absent": True}
            raise
        if absent_expected:
            raise VerificationError(f"Basecamp {capability.name} read-back still found the resource")
        if service == "events" and isinstance(value, list):
            owned = [entry for entry in value if isinstance(entry, Mapping)]
            if not owned or any(project_id_from(entry) != project_id for entry in owned):
                raise VerificationError("Basecamp recording history does not prove project ownership")
            return {"verified": True, "events": owned}
        if not isinstance(value, Mapping):
            raise VerificationError("Basecamp mutation read-back is not an object")
        expected_subscription = {
            "subscriptions.subscribe": True,
            "subscriptions.unsubscribe": False,
        }.get(capability.name)
        if expected_subscription is not None and value.get("subscribed") is not expected_subscription:
            raise VerificationError("Basecamp subscription read-back does not match requested state")
        expected_status = {
            "projects.archive": "archived",
            "projects.restore": "active",
            "recordings.archive": "archived",
            "recordings.restore": "active",
        }.get(capability.name)
        if expected_status is not None and value.get("status") != expected_status:
            raise VerificationError("Basecamp status read-back does not match the requested state")
        expected_completion = {
            "todos.complete": True,
            "todos.restore": False,
        }.get(capability.name)
        if expected_completion is not None and value.get("completed") is not expected_completion:
            raise VerificationError("Basecamp completion read-back does not match the requested state")
        self._assert_requested_state(capability, arguments, value)
        canonical_project = project_id_from(value)
        if capability.service == "projects":
            canonical_project = str(value.get("id") or "")
        if canonical_project and canonical_project != project_id:
            raise OwnershipError("Basecamp mutation read-back belongs to another project")
        return {"verified": True, "canonical": value}
