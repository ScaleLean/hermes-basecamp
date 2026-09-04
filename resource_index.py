"""Canonical Basecamp resource ownership resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from policy import Capability


class OwnershipError(PermissionError):
    """A resource cannot be proved to belong to the requested allowed project."""


class ResourceClient(Protocol):
    project_ids: tuple[str, ...]

    async def call(self, service: str, method: str, arguments: Mapping[str, Any]) -> Any: ...


def project_id_from(item: Mapping[str, Any]) -> str:
    for key in ("bucket", "project"):
        value = item.get(key)
        if isinstance(value, Mapping) and value.get("id") is not None:
            return str(value["id"])
    for key in ("bucket_id", "project_id"):
        if item.get(key) is not None:
            return str(item[key])
    return ""


def folder_project_ids(item: Mapping[str, Any]) -> set[str]:
    projects = item.get("projects") or item.get("buckets") or []
    if not isinstance(projects, list):
        return set()
    return {
        str(project.get("id") or "")
        for project in projects
        if isinstance(project, Mapping) and project.get("id") is not None
    }


def filter_nested_project(value: Any, project_id: str) -> tuple[Any, bool]:
    """Keep only nested objects that carry or contain evidence for one project."""
    if isinstance(value, list):
        filtered = []
        for item in value:
            kept, owned = filter_nested_project(item, project_id)
            if owned:
                filtered.append(kept)
        return filtered, bool(filtered)
    if not isinstance(value, Mapping):
        return value, False
    explicit = project_id_from(value)
    if explicit:
        return (dict(value), True) if explicit == project_id else ({}, False)
    nested: dict[str, Any] = {}
    saw_owned = False
    for key, item in value.items():
        kept, owned = filter_nested_project(item, project_id)
        if owned:
            nested[key] = kept
            saw_owned = True
    if not saw_owned:
        return {}, False
    return nested, True


REPORT_LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "assignments.list": ("priorities", "non_priorities"),
    "reports.assigned": ("todos",),
    "reports.overdue": (
        "under_a_week_late",
        "over_a_week_late",
        "over_a_month_late",
        "over_three_months_late",
    ),
    "reports.person_progress": ("events",),
    "reports.upcoming": (
        "schedule_entries",
        "recurring_schedule_entry_occurrences",
        "assignables",
    ),
}

REPORT_IGNORED_FIELDS: dict[str, tuple[str, ...]] = {
    "assignments.list": (),
    "reports.assigned": ("grouped_by", "person"),
    "reports.overdue": (),
    "reports.person_progress": ("person",),
    "reports.upcoming": (),
}


def project_report_projection(
    capability_name: str,
    value: Mapping[str, Any],
    project_id: str,
) -> dict[str, list[Any]]:
    """Project account-wide report mappings using their frozen SDK response shapes."""
    allowed_fields = REPORT_LIST_FIELDS[capability_name]
    known_fields = set(allowed_fields) | set(REPORT_IGNORED_FIELDS[capability_name])
    unknown_nonempty = [
        key for key, item in value.items() if key not in known_fields and item not in (None, "", [], {})
    ]
    if unknown_nonempty:
        raise OwnershipError(
            f"Basecamp {capability_name} returned unknown nonempty fields: " + ", ".join(sorted(unknown_nonempty))
        )
    projected: dict[str, list[Any]] = {}
    for field in allowed_fields:
        items = value.get(field, [])
        if not isinstance(items, list):
            if items not in (None, "", {}):
                raise OwnershipError(f"Basecamp {capability_name} field {field} is not a list")
            items = []
        projected[field] = [
            dict(item) for item in items if isinstance(item, Mapping) and project_id_from(item) == project_id
        ]
    return projected


class ResourceIndex:
    def __init__(self, client: ResourceClient) -> None:
        self.client = client

    async def resolve(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        requested_project_id: str,
    ) -> Mapping[str, Any]:
        allowed = set(self.client.project_ids)
        if requested_project_id not in allowed:
            raise OwnershipError("Basecamp execution project is not allowlisted")

        if (
            capability.name == "people.project_access"
            and str(arguments.get("project_id") or "") != requested_project_id
        ):
            raise OwnershipError("Basecamp project access project does not match its execution context")

        if capability.name == "projects.update" and arguments.get("admissions") is not None:
            raise OwnershipError("Basecamp project admissions cannot be independently read back after mutation")

        if capability.name.startswith("folders."):
            if capability.name == "folders.list":
                return {"bucket": {"id": requested_project_id}}
            if capability.name == "folders.create":
                projects = {str(value) for value in (arguments.get("project_ids") or [])}
                if projects != {requested_project_id}:
                    raise OwnershipError("Basecamp folder creation must target only the execution project")
                return {"bucket": {"id": requested_project_id}}
            folder_id = str(arguments.get("folder_id") or "")
            if not folder_id.isdigit():
                raise OwnershipError("Basecamp folder ID is missing")
            folder = await self.client.call("folders", "get_folder", {"folder_id": int(folder_id)})
            projects = folder_project_ids(folder) if isinstance(folder, Mapping) else set()
            if projects != {requested_project_id}:
                raise OwnershipError("Basecamp folder contains a different or denied project")
            return folder
        if capability.name == "cards.move":
            card_id = str(arguments.get("card_id") or "")
            column_id = str(arguments.get("column_id") or "")
            if not card_id.isdigit() or not column_id.isdigit():
                raise OwnershipError("Basecamp card and destination column IDs are required")
            card = await self.client.call("cards", "get", {"card_id": int(card_id)})
            column = await self.client.call("card_columns", "get", {"column_id": int(column_id)})
            if any(project_id_from(item) != requested_project_id for item in (card, column)):
                raise OwnershipError("Basecamp card move crosses the project boundary")
            return card
        if capability.name == "card_columns.move":
            table_id = str(arguments.get("card_table_id") or "")
            source_id = str(arguments.get("source_id") or "")
            target_id = str(arguments.get("target_id") or "")
            if not all(value.isdigit() for value in (table_id, source_id, target_id)):
                raise OwnershipError("Basecamp card table, source column, and target column IDs are required")
            table, source, target = (
                await self.client.call("card_tables", "get", {"card_table_id": int(table_id)}),
                await self.client.call("card_columns", "get", {"column_id": int(source_id)}),
                await self.client.call("card_columns", "get", {"column_id": int(target_id)}),
            )
            if any(project_id_from(item) != requested_project_id for item in (table, source, target)):
                raise OwnershipError("Basecamp card-column move crosses the project boundary")
            return table
        if capability.name == "cards.steps.reposition":
            card_id = str(arguments.get("card_id") or "")
            step_id = str(arguments.get("source_id") or "")
            if not card_id.isdigit() or not step_id.isdigit():
                raise OwnershipError("Basecamp card and source step IDs are required")
            card = await self.client.call("cards", "get", {"card_id": int(card_id)})
            step = await self.client.call("card_steps", "get", {"step_id": int(step_id)})
            parent = step.get("parent") if isinstance(step, Mapping) else None
            if (
                not isinstance(card, Mapping)
                or not isinstance(step, Mapping)
                or project_id_from(card) != requested_project_id
                or project_id_from(step) != requested_project_id
                or not isinstance(parent, Mapping)
                or str(parent.get("id") or "") != card_id
            ):
                raise OwnershipError("Basecamp source step does not belong to the requested card and project")
            return card
        if capability.name == "todos.reposition" and arguments.get("parent_id") is not None:
            todo_id = str(arguments.get("todo_id") or "")
            parent_id = str(arguments.get("parent_id") or "")
            if not todo_id.isdigit() or not parent_id.isdigit():
                raise OwnershipError("Basecamp to-do and destination list IDs are required")
            todo = await self.client.call("todos", "get", {"todo_id": int(todo_id)})
            parent = await self.client.call("todolists", "get", {"id": int(parent_id)})
            if any(
                not isinstance(item, Mapping) or project_id_from(item) != requested_project_id
                for item in (todo, parent)
            ):
                raise OwnershipError("Basecamp to-do destination parent crosses the project boundary")
            return todo
        if capability.name == "people.get":
            person_id = str(arguments.get("person_id") or "")
            if not person_id.isdigit():
                raise OwnershipError("Basecamp person ID is missing")
            people = await self.client.call(
                "people", "list_for_project", {"project_id": int(requested_project_id), "max_items": 500}
            )
            if not any(isinstance(person, Mapping) and str(person.get("id") or "") == person_id for person in people):
                raise OwnershipError("Basecamp person is not a member of the execution project")
            return {"bucket": {"id": requested_project_id}}
        if capability.name in {"reports.assigned", "reports.person_progress"}:
            person_id = str(arguments.get("person_id") or "")
            if not person_id.isdigit():
                raise OwnershipError("Basecamp person report requires a numeric person ID")
            people = await self.client.call(
                "people", "list_for_project", {"project_id": int(requested_project_id), "max_items": 500}
            )
            if not any(isinstance(person, Mapping) and str(person.get("id") or "") == person_id for person in people):
                raise OwnershipError("Basecamp person report target is not a project member")
            return {"bucket": {"id": requested_project_id}}
        if capability.name == "people.project_access":
            if arguments.get("create"):
                raise OwnershipError("Basecamp project access cannot create or invite people")
            people = await self.client.call(
                "people", "list_for_project", {"project_id": int(requested_project_id), "max_items": 500}
            )
            current = {str(person.get("id")) for person in people if isinstance(person, Mapping)}
            grants = {str(value) for value in (arguments.get("grant") or [])}
            revokes = {str(value) for value in (arguments.get("revoke") or [])}
            if not grants and not revokes:
                raise OwnershipError("Basecamp project access requires a grant or revoke")
            if not revokes.issubset(current):
                raise OwnershipError("Basecamp project access can revoke only current project members")
            for person_id in grants - current:
                if not person_id.isdigit():
                    raise OwnershipError("Basecamp project access grant IDs must be numeric")
                person = await self.client.call("people", "get", {"person_id": int(person_id)})
                if not isinstance(person, Mapping) or str(person.get("id") or "") != person_id:
                    raise OwnershipError("Basecamp project access grant target is not an existing account person")
            return {"bucket": {"id": requested_project_id}}
        if capability.name in {"message_types.update", "message_types.delete"}:
            if str(arguments.get("bucket_id") or "") != requested_project_id:
                raise OwnershipError("Basecamp message type project does not match its execution context")
            type_id = str(arguments.get("type_id") or "")
            if not type_id.isdigit():
                raise OwnershipError("Basecamp message type ID is required")
            project = await self.client.call("projects", "get", {"project_id": int(requested_project_id)})
            message_type = await self.client.call(
                "message_types",
                "get",
                {"bucket_id": int(requested_project_id), "type_id": int(type_id)},
            )
            if (
                not isinstance(project, Mapping)
                or str(project.get("id") or "") != requested_project_id
                or not isinstance(message_type, Mapping)
                or str(message_type.get("id") or "") != type_id
            ):
                raise OwnershipError("Basecamp message type could not be proved in the execution project")
            return message_type
        if capability.name == "search.query":
            has_bucket_ids = "bucket_ids" in arguments
            has_bucket_id = "bucket_id" in arguments
            if has_bucket_ids == has_bucket_id:
                raise OwnershipError("Basecamp search requires exactly one project scope form")
            bucket_ids = [str(value) for value in (arguments.get("bucket_ids") or [])]
            bucket_id = str(arguments.get("bucket_id") or "")
            if (has_bucket_ids and bucket_ids != [requested_project_id]) or (
                has_bucket_id and bucket_id != requested_project_id
            ):
                raise OwnershipError("Basecamp search must be constrained to the execution project")
            return {"bucket": {"id": requested_project_id}}
        if capability.name.startswith("wormholes."):
            if str(arguments.get("bucket_id") or "") != requested_project_id:
                raise OwnershipError("Basecamp wormhole source project does not match its execution context")
            table_id = str(arguments.get("card_table_id") or "")
            if not table_id.isdigit():
                raise OwnershipError("Basecamp wormhole card-table ID is required")
            table = await self.client.call("card_tables", "get", {"card_table_id": int(table_id)})
            if not isinstance(table, Mapping) or project_id_from(table) != requested_project_id:
                raise OwnershipError("Basecamp wormhole card table is outside the execution project")
            if capability.name != "wormholes.create":
                wormhole_id = str(arguments.get("wormhole_id") or "")
                wormholes = table.get("wormholes") or []
                if not wormhole_id.isdigit() or not any(
                    isinstance(item, Mapping) and str(item.get("id") or "") == wormhole_id for item in wormholes
                ):
                    raise OwnershipError("Basecamp wormhole is outside the execution card table")
            destination_id = arguments.get("destination_recording_id")
            if destination_id is not None:
                destination_events = await self.client.call(
                    "events", "list", {"recording_id": int(str(destination_id))}
                )
                destinations = [entry for entry in destination_events if isinstance(entry, Mapping)]
                destination_projects = {project_id_from(entry) for entry in destinations}
                if len(destination_projects) != 1 or not destination_projects.issubset(allowed):
                    raise OwnershipError("Basecamp wormhole destination is not in an allowlisted project")
            return table
        if capability.name in {"boosts.event.create", "boosts.event.list"}:
            recording_id = str(arguments.get("recording_id") or "")
            event_id = str(arguments.get("event_id") or "")
            if not recording_id.isdigit() or not event_id.isdigit():
                raise OwnershipError("Basecamp recording and event IDs are required")
            events = await self.client.call("events", "list", {"recording_id": int(recording_id)})
            matching = [
                event
                for event in events
                if isinstance(event, Mapping)
                and str(event.get("id") or "") == event_id
                and str(event.get("recording_id") or "") == recording_id
                and project_id_from(event) == requested_project_id
            ]
            if len(matching) != 1:
                raise OwnershipError("Basecamp event boost target is not the exact project-bound event")
            return matching[0]
        if capability.name == "todolist_groups.reposition":
            group_id = str(arguments.get("group_id") or "")
            todolist_id = str(arguments.get("todolist_id") or "")
            if not group_id.isdigit() or not todolist_id.isdigit():
                raise OwnershipError("Basecamp to-do group and list IDs are required")
            todolist = await self.client.call("todolists", "get", {"id": int(todolist_id)})
            groups = await self.client.call(
                "todolist_groups", "list", {"todolist_id": int(todolist_id), "max_items": 500}
            )
            if project_id_from(todolist) != requested_project_id or not any(
                isinstance(group, Mapping) and str(group.get("id") or "") == group_id for group in groups
            ):
                raise OwnershipError("Basecamp to-do group is outside the execution project")
            return todolist
        project_item: Mapping[str, Any] | None = None
        if capability.project_argument:
            supplied = str(arguments.get(capability.project_argument) or "")
            if supplied != requested_project_id:
                raise OwnershipError("Basecamp operation project does not match its execution context")
            project_item = await self.client.call("projects", "get", {"project_id": int(supplied)})
            if not isinstance(project_item, Mapping) or str(project_item.get("id") or "") != requested_project_id:
                raise OwnershipError("Basecamp project ownership could not be verified")
        if capability.owner_service and capability.owner_method and capability.owner_argument:
            value = arguments.get(capability.owner_argument)
            if value is None or not str(value).isdigit():
                raise OwnershipError(f"Basecamp ownership argument {capability.owner_argument} is missing")
            item = await self.client.call(
                capability.owner_service,
                capability.owner_method,
                {(capability.owner_parameter or capability.owner_argument): int(str(value))},
            )
        elif project_item is not None:
            item = project_item
        else:
            # Account-wide reads still run under an allowlisted execution context. Results are filtered later.
            return {"bucket": {"id": requested_project_id}}

        if capability.owner_service == "events" and isinstance(item, list):
            owned = [entry for entry in item if isinstance(entry, Mapping)]
            if not owned or any(project_id_from(entry) != requested_project_id for entry in owned):
                raise OwnershipError("Basecamp recording event history does not prove project ownership")
            if capability.service == "recordings":
                event_ids = {str(entry.get("id") or "") for entry in owned if entry.get("id") is not None}
                if len(event_ids) != len(owned) or "" in event_ids:
                    raise OwnershipError("Basecamp recording event history lacks canonical event IDs")
                return {
                    "bucket": {"id": requested_project_id},
                    "pre_event_ids": sorted(event_ids),
                }
            return owned[0]
        if not isinstance(item, Mapping):
            raise OwnershipError("Basecamp canonical ownership response is not an object")
        canonical_project = project_id_from(item)
        if capability.project_argument and item is project_item:
            canonical_project = str(item.get("id") or "")
        if canonical_project != requested_project_id or canonical_project not in allowed:
            raise OwnershipError("Basecamp canonical resource belongs to a different or denied project")
        return item

    def filter_results(self, capability: Capability, value: Any, requested_project_id: str) -> Any:
        """Drop account-wide results that cannot be tied to the allowed execution project."""
        if isinstance(value, list):
            if capability.name == "bookmarks.list":
                return [
                    item
                    for item in value
                    if isinstance(item, Mapping)
                    and isinstance(item.get("recording"), Mapping)
                    and project_id_from(item["recording"]) == requested_project_id
                ]
            if capability.name == "folders.list":
                return [
                    item
                    for item in value
                    if isinstance(item, Mapping) and folder_project_ids(item) == {requested_project_id}
                ]
            if capability.service == "projects":
                return [
                    item
                    for item in value
                    if isinstance(item, Mapping) and str(item.get("id") or "") == requested_project_id
                ]
            owned = [
                item for item in value if isinstance(item, Mapping) and project_id_from(item) == requested_project_id
            ]
            if owned or any(isinstance(item, Mapping) and project_id_from(item) for item in value):
                return owned
            if capability.project_argument or capability.owner_service or capability.name == "people.get":
                return value
            return []
        if isinstance(value, Mapping):
            if capability.name == "search.metadata":
                return value
            if capability.name in {
                "assignments.list",
                "reports.assigned",
                "reports.overdue",
                "reports.person_progress",
                "reports.upcoming",
            }:
                return project_report_projection(capability.name, value, requested_project_id)
            if capability.name.startswith("folders."):
                if folder_project_ids(value) != {requested_project_id}:
                    raise OwnershipError("Basecamp folder result contains a different or denied project")
                return value
            project_id = project_id_from(value)
            if capability.service == "projects" and str(value.get("id") or "") == requested_project_id:
                project_id = requested_project_id
            if project_id and project_id != requested_project_id:
                raise OwnershipError("Basecamp result crossed the execution project boundary")
            if not project_id and capability.name not in {"people.get", "search.query"}:
                raise OwnershipError("Basecamp result ownership cannot be proved")
        return value
