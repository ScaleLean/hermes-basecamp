import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from journal import OperationJournal
from operations import BasecampOperations, CapabilityDeniedError, OperationInProgressError
from policy import (
    ActionApproval,
    ExecutionContext,
    TriggerClass,
    action_digest,
    default_registry,
)
from resource_index import OwnershipError, ResourceIndex, filter_nested_project
from sdk_client import ExpectedIdentity


class _Client:
    def __init__(self):
        self.expected = ExpectedIdentity("1", "2", "agent@example.com")
        self.project_ids = ("10",)
        self.calls = []
        self.attestations = 0

    async def attest_full_member(self):
        self.attestations += 1
        return {"id": 2, "employee": True, "client": False}

    async def call(self, service, method, arguments):
        self.calls.append((service, method, dict(arguments)))
        if service == "projects" and method == "get":
            return {"id": 10, "name": "New", "creator": {"id": 2}}
        if service == "projects" and method == "update":
            return {"id": 10, "bucket": {"id": 10}, "creator": {"id": 2}}
        if service == "timeline":
            return [{"id": 1, "bucket": {"id": 10}}, {"id": 2, "bucket": {"id": 99}}]
        if service == "events" and method == "list":
            return [{"id": 3, "bucket": {"id": 10}}]
        if service == "recordings" and method == "archive":
            return None
        raise AssertionError((service, method, arguments))


class _ForbiddenClient(_Client):
    async def call(self, service, method, arguments):
        if service == "projects" and method == "update":
            error = RuntimeError("forbidden")
            error.status_code = 403
            raise error
        return await super().call(service, method, arguments)


class _CoverageClient(_Client):
    def __init__(self):
        super().__init__()
        self.project_ids = ("10", "20")

    async def call(self, service, method, arguments):
        self.calls.append((service, method, dict(arguments)))
        if (service, method) == ("projects", "get"):
            return {"id": int(arguments["project_id"]), "bucket": {"id": int(arguments["project_id"])}}
        if (service, method) == ("people", "list_for_project"):
            return [{"id": 2, "bucket": {"id": 10}}]
        if (service, method) == ("people", "get"):
            return {"id": int(arguments["person_id"])}
        if (service, method) == ("todolists", "get"):
            return {"id": int(arguments["id"]), "bucket": {"id": 10}}
        if (service, method) == ("todolist_groups", "list"):
            return [{"id": 33, "position": 2, "bucket": {"id": 10}}]
        if (service, method) == ("todolist_groups", "reposition"):
            if set(arguments) != {"group_id", "position"}:
                raise AssertionError("context-only todolist_id leaked into SDK arguments")
            return None
        if (service, method) == ("card_tables", "get"):
            return {
                "id": int(arguments["card_table_id"]),
                "bucket": {"id": 10},
                "lists": [
                    {"id": 41, "position": 2, "bucket": {"id": 10}},
                    {"id": 42, "position": 1, "bucket": {"id": 10}},
                ],
                "wormholes": [{"id": 50, "destination_url": "/columns/60", "bucket": {"id": 10}}],
            }
        if (service, method) == ("card_columns", "get"):
            return {"id": int(arguments["column_id"]), "bucket": {"id": 10}}
        if (service, method) == ("cards", "get"):
            return {"id": int(arguments["card_id"]), "bucket": {"id": 10}}
        if (service, method) == ("card_steps", "get"):
            return {
                "id": int(arguments["step_id"]),
                "bucket": {"id": 10},
                "parent": {"id": 70},
            }
        if (service, method) == ("todos", "get"):
            return {"id": int(arguments["todo_id"]), "bucket": {"id": 10}}
        if (service, method) == ("message_types", "get"):
            return {"id": int(arguments["type_id"]), "name": "Notice"}
        if (service, method) == ("events", "list"):
            project = 20 if int(arguments["recording_id"]) == 60 else 99
            return [{"id": 1, "recording_id": arguments["recording_id"], "bucket": {"id": project}}]
        raise AssertionError((service, method, arguments))


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = _Client()
        self.operations = BasecampOperations(
            self.client,
            profile_id="profile",
            journal=OperationJournal(Path(self.temp.name) / "journal.sqlite3"),
        )

    def tearDown(self):
        self.temp.cleanup()

    async def test_read_refetches_project_and_filters_cross_project_results(self):
        context = ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile")
        result = await self.operations.execute("activity.timeline", {"project_id": "10"}, context)
        self.assertEqual([item["id"] for item in result.value], [1])

    async def test_write_has_readback_and_idempotent_replay(self):
        context = ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile")
        arguments = {"project_id": "10", "name": "New"}
        first = await self.operations.execute("projects.update", arguments, context, idempotency_key="update-123")
        second = await self.operations.execute("projects.update", arguments, context, idempotency_key="update-123")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(sum(call[:2] == ("projects", "update") for call in self.client.calls), 1)

    async def test_scope_mismatch_fails_before_mutation(self):
        context = ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile")
        with self.assertRaises(OwnershipError):
            await self.operations.execute(
                "projects.update",
                {"project_id": "99", "name": "Forged"},
                context,
                idempotency_key="update-999",
            )
        self.assertFalse(any(call[:2] == ("projects", "update") for call in self.client.calls))

    async def test_profile_mismatch_fails_before_attestation(self):
        context = ExecutionContext(TriggerClass.MANUAL, "10", "other")
        with self.assertRaises(PermissionError):
            await self.operations.execute("projects.get", {"project_id": "10"}, context)
        self.assertEqual(self.client.attestations, 0)

    async def test_pending_journal_does_not_repeat_a_mutation(self):
        arguments = {"project_id": "10", "name": "New"}
        from policy import action_digest

        self.operations.journal.reserve(
            profile_id="profile",
            idempotency_key="pending-1",
            capability="projects.update",
            arguments_digest=action_digest("projects.update", "10", arguments),
        )
        with self.assertRaises(OperationInProgressError):
            await self.operations.execute(
                "projects.update",
                arguments,
                ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile"),
                idempotency_key="pending-1",
            )

    async def test_search_requires_one_exact_scope_without_sdk_calls(self):
        client = _CoverageClient()
        capability = default_registry().get("search.query")
        for arguments in (
            {},
            {"bucket_id": 10, "bucket_ids": [10]},
            {"bucket_ids": [10, 20]},
            {"bucket_id": 20},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(OwnershipError):
                await ResourceIndex(client).resolve(capability, arguments, "10")
            self.assertEqual(client.calls, [])

    async def test_attestation_403_is_denied_without_journal_entry(self):
        error = RuntimeError("forbidden")
        error.status_code = 403
        self.client.attest_full_member = AsyncMock(side_effect=error)
        with self.assertRaises(CapabilityDeniedError):
            await self.operations.execute(
                "projects.update",
                {"project_id": "10", "name": "Denied"},
                ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile"),
                idempotency_key="attest-403",
            )
        self.assertIsNone(self.operations.journal.get("profile", "attest-403"))

    async def test_ownership_preflight_403_is_denied_without_journal_entry(self):
        error = RuntimeError("forbidden")
        error.status_code = 403
        self.operations.resources.resolve = AsyncMock(side_effect=error)
        with self.assertRaises(CapabilityDeniedError):
            await self.operations.execute(
                "projects.update",
                {"project_id": "10", "name": "Denied"},
                ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile"),
                idempotency_key="preflight-403",
            )
        self.assertIsNone(self.operations.journal.get("profile", "preflight-403"))

    async def test_403_is_a_capability_denial_not_a_retry(self):
        from operations import CapabilityDeniedError

        client = _ForbiddenClient()
        operations = BasecampOperations(
            client,
            profile_id="profile",
            journal=OperationJournal(Path(self.temp.name) / "forbidden.sqlite3"),
        )
        with self.assertRaises(CapabilityDeniedError):
            await operations.execute(
                "projects.update",
                {"project_id": "10", "name": "Denied"},
                ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile"),
                idempotency_key="forbidden-1",
            )
        entry = operations.journal.get("profile", "forbidden-1")
        self.assertEqual(entry.state, "failed")

    async def test_recording_ownership_uses_read_only_events_not_spotlight(self):
        self.client.calls.clear()
        capability = default_registry().get("comments.create")
        await ResourceIndex(self.client).resolve(capability, {"recording_id": 40}, "10")
        self.assertEqual(self.client.calls[0][:2], ("events", "list"))
        self.assertFalse(any(call[:2] == ("recordings", "spotlight") for call in self.client.calls))

    async def test_recording_ownership_captures_pre_mutation_event_ids(self):
        ownership = await ResourceIndex(self.client).resolve(
            default_registry().get("recordings.archive"),
            {"recording_id": 40},
            "10",
        )
        self.assertEqual(ownership["pre_event_ids"], ["3"])

    async def test_operations_passes_pre_mutation_event_snapshot_to_verifier(self):
        arguments = {"recording_id": 40}
        approval = ActionApproval(
            "recordings.archive",
            "10",
            action_digest("recordings.archive", "10", arguments),
            "Human User",
            time.time() + 60,
        )
        self.operations.verifier.verify = AsyncMock(return_value={"verified": True, "event": {"id": 4}})
        await self.operations.execute(
            "recordings.archive",
            arguments,
            ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile"),
            approval=approval,
            idempotency_key="archive-40",
        )
        self.assertEqual(
            self.operations.verifier.verify.await_args.kwargs["precondition"]["pre_event_ids"],
            ["3"],
        )

    async def test_project_access_allows_existing_account_person_but_not_create(self):
        client = _CoverageClient()
        capability = default_registry().get("people.project_access")
        await ResourceIndex(client).resolve(capability, {"project_id": 10, "grant": [3]}, "10")
        with self.assertRaises(OwnershipError):
            await ResourceIndex(client).resolve(
                capability,
                {"project_id": 10, "create": [{"email_address": "invite@example.com"}]},
                "10",
            )

    async def test_project_access_mismatch_is_rejected_before_any_client_call(self):
        client = _CoverageClient()
        with self.assertRaises(OwnershipError):
            await ResourceIndex(client).resolve(
                default_registry().get("people.project_access"),
                {"project_id": 20, "grant": [2]},
                "10",
            )
        self.assertEqual(client.calls, [])

    async def test_unverifiable_project_admissions_is_rejected_before_mutation(self):
        client = _CoverageClient()
        with self.assertRaisesRegex(OwnershipError, "admissions"):
            await ResourceIndex(client).resolve(
                default_registry().get("projects.update"),
                {"project_id": 10, "name": "Project", "admissions": "team"},
                "10",
            )
        self.assertEqual(client.calls, [])

    async def test_card_step_reposition_proves_exact_step_parent(self):
        client = _CoverageClient()
        await ResourceIndex(client).resolve(
            default_registry().get("cards.steps.reposition"),
            {"card_id": 70, "source_id": 71, "position": 1},
            "10",
        )
        self.assertIn(("card_steps", "get"), {(s, m) for s, m, _ in client.calls})

    async def test_todo_reposition_proves_optional_destination_list(self):
        client = _CoverageClient()
        await ResourceIndex(client).resolve(
            default_registry().get("todos.reposition"),
            {"todo_id": 80, "position": 1, "parent_id": 30},
            "10",
        )
        self.assertIn(("todolists", "get"), {(s, m) for s, m, _ in client.calls})

    async def test_message_type_mutation_prefetches_exact_type(self):
        client = _CoverageClient()
        await ResourceIndex(client).resolve(
            default_registry().get("message_types.update"),
            {"bucket_id": 10, "type_id": 90, "name": "Notice"},
            "10",
        )
        self.assertIn(("message_types", "get"), {(s, m) for s, m, _ in client.calls})

    async def test_event_boost_requires_exact_event_in_recording_history(self):
        client = _CoverageClient()
        await ResourceIndex(client).resolve(
            default_registry().get("boosts.event.create"),
            {"recording_id": 60, "event_id": 1, "content": "Thanks"},
            "20",
        )
        with self.assertRaises(OwnershipError):
            await ResourceIndex(client).resolve(
                default_registry().get("boosts.event.create"),
                {"recording_id": 60, "event_id": 999, "content": "Thanks"},
                "20",
            )

    async def test_wormhole_proves_source_table_and_allowlisted_destination(self):
        client = _CoverageClient()
        capability = default_registry().get("wormholes.create")
        await ResourceIndex(client).resolve(
            capability,
            {"bucket_id": 10, "card_table_id": 40, "destination_recording_id": 60},
            "10",
        )
        with self.assertRaises(OwnershipError):
            await ResourceIndex(client).resolve(
                capability,
                {"bucket_id": 10, "card_table_id": 40, "destination_recording_id": 61},
                "10",
            )

    async def test_card_column_move_proves_table_source_and_target(self):
        client = _CoverageClient()
        capability = default_registry().get("card_columns.move")
        await ResourceIndex(client).resolve(
            capability,
            {"card_table_id": 40, "source_id": 41, "target_id": 42, "position": 2},
            "10",
        )
        called = {(service, method) for service, method, _ in client.calls}
        self.assertTrue({("card_tables", "get"), ("card_columns", "get")}.issubset(called))

    async def test_context_only_owner_argument_is_not_sent_to_sdk(self):
        client = _CoverageClient()
        operations = BasecampOperations(
            client,
            profile_id="profile",
            journal=OperationJournal(Path(self.temp.name) / "coverage.sqlite3"),
        )
        result = await operations.execute(
            "todolist_groups.reposition",
            {"group_id": 33, "position": 2, "todolist_id": 30},
            ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile"),
            idempotency_key="group-position-33",
        )
        self.assertTrue(result.verified["verified"])

    def test_nested_account_report_filter_drops_other_projects(self):
        report = {
            "overdue": [
                {"id": 1, "title": "Allowed", "bucket": {"id": 10}},
                {"id": 2, "title": "Denied", "bucket": {"id": 99}},
            ],
            "count": 2,
        }
        filtered, owned = filter_nested_project(report, "10")
        self.assertTrue(owned)
        self.assertEqual([item["id"] for item in filtered["overdue"]], [1])
        self.assertNotIn("count", filtered)

    def test_account_wide_dict_reads_use_operation_specific_project_projections(self):
        fixtures = {
            "assignments.list": {
                "priorities": [
                    {"id": 1, "bucket": {"id": 10}},
                    {"id": 2, "bucket": {"id": 99}},
                ],
                "non_priorities": [{"id": 3, "bucket": {"id": 10}}],
            },
            "reports.assigned": {
                "grouped_by": "bucket",
                "person": {"id": 2, "name": "Project member"},
                "todos": [
                    {"id": 4, "bucket": {"id": 10}},
                    {"id": 5, "bucket": {"id": 99}},
                ],
            },
            "reports.overdue": {
                "under_a_week_late": [{"id": 6, "bucket": {"id": 10}}],
                "over_a_week_late": [{"id": 7, "bucket": {"id": 99}}],
                "over_a_month_late": [],
                "over_three_months_late": [],
            },
            "reports.person_progress": {
                "person": {"id": 2, "name": "Project member"},
                "events": [
                    {"id": 8, "bucket": {"id": 10}},
                    {"id": 9, "bucket": {"id": 99}},
                ],
            },
            "reports.upcoming": {
                "schedule_entries": [{"id": 10, "bucket": {"id": 10}}],
                "recurring_schedule_entry_occurrences": [{"id": 11, "bucket": {"id": 99}}],
                "assignables": [{"id": 12, "bucket": {"id": 10}}],
            },
        }
        index = ResourceIndex(_CoverageClient())
        for capability_name, fixture in fixtures.items():
            with self.subTest(capability=capability_name):
                projected = index.filter_results(default_registry().get(capability_name), fixture, "10")
                returned_ids = {item["id"] for items in projected.values() for item in items}
                self.assertTrue(returned_ids)
                self.assertNotIn(2, returned_ids)
                self.assertNotIn(5, returned_ids)
                self.assertNotIn(7, returned_ids)
                self.assertNotIn(9, returned_ids)
                self.assertNotIn(11, returned_ids)
                self.assertTrue(
                    all(str(item["bucket"]["id"]) == "10" for items in projected.values() for item in items)
                )

    def test_account_wide_dict_reads_fail_closed_on_unknown_nonempty_shape(self):
        with self.assertRaises(OwnershipError):
            ResourceIndex(_CoverageClient()).filter_results(
                default_registry().get("reports.overdue"),
                {
                    "under_a_week_late": [],
                    "new_server_group": [{"id": 1, "bucket": {"id": 10}}],
                },
                "10",
            )

    def test_account_wide_list_reads_retain_only_project_evidence(self):
        fixtures = {
            "bookmarks.list": [
                {"id": 1, "recording": {"id": 11, "bucket": {"id": 10}}},
                {"id": 2, "recording": {"id": 12, "bucket": {"id": 99}}},
            ],
            "drafts.list": [
                {"id": 3, "bucket": {"id": 10}},
                {"id": 4, "bucket": {"id": 99}},
            ],
            "assignments.completed": [
                {"id": 5, "bucket": {"id": 10}},
                {"id": 6, "bucket": {"id": 99}},
            ],
            "assignments.due": [
                {"id": 7, "bucket": {"id": 10}},
                {"id": 8, "bucket": {"id": 99}},
            ],
            "reports.progress": [
                {"id": 9, "bucket": {"id": 10}},
                {"id": 10, "bucket": {"id": 99}},
            ],
            "timesheets.report": [
                {"id": 11, "bucket": {"id": 10}},
                {"id": 12, "bucket": {"id": 99}},
            ],
            "campfire.list": [
                {"id": 13, "bucket": {"id": 10}},
                {"id": 14, "bucket": {"id": 99}},
            ],
        }
        index = ResourceIndex(_CoverageClient())
        for capability_name, fixture in fixtures.items():
            with self.subTest(capability=capability_name):
                filtered = index.filter_results(default_registry().get(capability_name), fixture, "10")
                self.assertEqual(len(filtered), 1)
                if capability_name == "bookmarks.list":
                    self.assertEqual(filtered[0]["recording"]["bucket"]["id"], 10)
                else:
                    self.assertEqual(filtered[0]["bucket"]["id"], 10)
