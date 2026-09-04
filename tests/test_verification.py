import unittest
from types import SimpleNamespace

from policy import RiskClass, default_registry
from verification import (
    ALTERNATE_AUTHOR_CREATE_FIELDS,
    AUTHORED_CREATE_CAPABILITIES,
    STRUCTURAL_CREATE_EXEMPTIONS,
    PostconditionVerifier,
    VerificationError,
)


class _VerificationClient:
    def __init__(self, responses=None):
        self.expected = SimpleNamespace(person_id="2")
        self.responses = responses or {}
        self.calls = []

    async def call(self, service, method, arguments):
        self.calls.append((service, method, dict(arguments)))
        response = self.responses[(service, method)]
        if isinstance(response, Exception):
            raise response
        return response


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scalar_mutation_result_is_not_treated_as_verified(self):
        capability = default_registry().get("projects.update")
        with self.assertRaisesRegex(VerificationError, "Unsupported.*response shape"):
            await PostconditionVerifier(_VerificationClient({})).verify(
                capability, {"project_id": 10, "name": "New"}, ["unexpected"], "10"
            )

    def test_create_capabilities_have_explicit_authorship_contract(self):
        creates = {
            capability.name
            for capability in default_registry().list()
            if capability.risk is not RiskClass.ADMINLAND_DENIED and capability.method.startswith("create")
        }
        classified = AUTHORED_CREATE_CAPABILITIES | STRUCTURAL_CREATE_EXEMPTIONS | set(ALTERNATE_AUTHOR_CREATE_FIELDS)
        self.assertEqual(creates, classified)
        self.assertFalse(AUTHORED_CREATE_CAPABILITIES & STRUCTURAL_CREATE_EXEMPTIONS)

    async def test_authored_create_requires_present_matching_creator(self):
        client = _VerificationClient({("messages", "get"): {"id": 5, "bucket": {"id": 10}}})
        with self.assertRaisesRegex(VerificationError, "agent authorship"):
            await PostconditionVerifier(client).verify(
                default_registry().get("messages.create"),
                {"board_id": 3, "subject": "Hello", "content": "World"},
                {"id": 5},
                "10",
            )

    def test_operation_specific_state_requires_present_exact_value(self):
        verifier = PostconditionVerifier(_VerificationClient())
        cases = (
            ("messages.pin", {"message_id": 1}, {}, "pinned"),
            ("messages.unpin", {"message_id": 1}, {"pinned": True}, "pinned"),
            ("checkins.pause", {"question_id": 1}, {}, "paused"),
            ("checkins.resume", {"question_id": 1}, {"paused": True}, "paused"),
            ("dock_tools.enable", {"tool_id": 1}, {}, "enabled"),
            ("dock_tools.disable", {"tool_id": 1}, {"enabled": True}, "enabled"),
            ("dock_tools.reposition", {"tool_id": 1, "position": 2}, {}, "position"),
        )
        for name, arguments, canonical, expected in cases:
            with self.subTest(name=name), self.assertRaisesRegex(VerificationError, expected):
                verifier._assert_requested_state(default_registry().get(name), arguments, canonical)

    def test_card_column_hold_requires_exact_state(self):
        verifier = PostconditionVerifier(_VerificationClient())
        with self.assertRaises(VerificationError):
            verifier._assert_requested_state(
                default_registry().get("card_columns.hold.enable"),
                {"bucket_id": 10, "column_id": 2},
                {},
            )
        with self.assertRaises(VerificationError):
            verifier._assert_requested_state(
                default_registry().get("card_columns.hold.disable"),
                {"bucket_id": 10, "column_id": 2},
                {"on_hold": {"id": 3}},
            )

    def test_nested_mutation_bodies_require_exact_canonical_state(self):
        verifier = PostconditionVerifier(_VerificationClient())
        cases = (
            (
                "calendars.update",
                {"calendar_id": 1, "calendar": {"color": "blue"}},
                {"id": 1, "color": "red"},
            ),
            (
                "projects.update",
                {
                    "project_id": 10,
                    "name": "Project",
                    "schedule_attributes": {
                        "start_date": "2026-09-01",
                        "end_date": "2026-09-30",
                    },
                },
                {
                    "id": 10,
                    "name": "Project",
                    "start_date": "2026-09-01",
                    "end_date": "2026-10-01",
                },
            ),
            (
                "gauges.update",
                {"needle_id": 1, "gauge_needle": {"description": "New"}},
                {"id": 1, "description": "Old"},
            ),
            (
                "subscriptions.update",
                {
                    "recording_id": 1,
                    "subscriptions": [2],
                    "unsubscriptions": [3],
                },
                {"subscribers": [{"id": 3}]},
            ),
            (
                "hillcharts.update",
                {"todoset_id": 1, "tracked": [2], "untracked": [3]},
                {"dots": [{"id": 3}]},
            ),
        )
        for name, arguments, canonical in cases:
            with self.subTest(name=name), self.assertRaises(VerificationError):
                verifier._assert_requested_state(default_registry().get(name), arguments, canonical)

    async def test_card_column_move_rejects_stale_order(self):
        client = _VerificationClient(
            {
                ("card_tables", "get"): {
                    "id": 4,
                    "bucket": {"id": 10},
                    "lists": [
                        {"id": 41, "position": 1},
                        {"id": 42, "position": 2},
                    ],
                }
            }
        )
        with self.assertRaises(VerificationError):
            await PostconditionVerifier(client).verify(
                default_registry().get("card_columns.move"),
                {
                    "card_table_id": 4,
                    "source_id": 41,
                    "target_id": 42,
                    "position": 2,
                },
                None,
                "10",
            )

    async def test_todo_reposition_rejects_stale_parent(self):
        client = _VerificationClient(
            {
                ("todos", "get"): {
                    "id": 4,
                    "bucket": {"id": 10},
                    "position": 2,
                    "parent": {"id": 99},
                }
            }
        )
        with self.assertRaises(VerificationError):
            await PostconditionVerifier(client).verify(
                default_registry().get("todos.reposition"),
                {"todo_id": 4, "position": 2, "parent_id": 30},
                None,
                "10",
            )

    async def test_subscription_and_gauge_void_mutations_require_exact_state(self):
        subscription_client = _VerificationClient(
            {("subscriptions", "get"): {"subscribed": False, "bucket": {"id": 10}}}
        )
        with self.assertRaises(VerificationError):
            await PostconditionVerifier(subscription_client).verify(
                default_registry().get("subscriptions.subscribe"),
                {"recording_id": 1},
                None,
                "10",
            )

        gauge_client = _VerificationClient(
            {("gauges", "list_gauges"): [{"id": 3, "bucket": {"id": 10}, "enabled": False}]}
        )
        with self.assertRaises(VerificationError):
            await PostconditionVerifier(gauge_client).verify(
                default_registry().get("gauges.toggle"),
                {"project_id": 10, "gauge": {"enabled": True}},
                None,
                "10",
            )

    async def test_folder_delete_requires_absence_readback(self):
        missing = RuntimeError("not found")
        missing.status_code = 404
        client = _VerificationClient({("folders", "get_folder"): missing})
        verified = await PostconditionVerifier(client).verify(
            default_registry().get("folders.delete"),
            {"folder_id": 4},
            None,
            "10",
        )
        self.assertTrue(verified["verified"])
        self.assertTrue(verified["absent"])

    async def test_recording_mutations_reject_stale_events_and_accept_new_agent_event(self):
        actions = {
            "recordings.archive": "archived",
            "recordings.restore": "restored",
            "recordings.trash": "trashed",
            "recordings.spotlight": "spotlighted",
            "recordings.unspotlight": "unspotlighted",
        }
        for capability_name, action in actions.items():
            stale = {
                "id": 1,
                "recording_id": 40,
                "action": action,
                "bucket": {"id": 10},
                "creator": {"id": 2},
            }
            with (
                self.subTest(capability=capability_name, state="stale"),
                self.assertRaisesRegex(VerificationError, "does not confirm"),
            ):
                await PostconditionVerifier(_VerificationClient({("events", "list"): [stale]})).verify(
                    default_registry().get(capability_name),
                    {"recording_id": 40},
                    None,
                    "10",
                    precondition={"pre_event_ids": ["1"]},
                )
            fresh = dict(stale, id=2)
            with self.subTest(capability=capability_name, state="fresh"):
                verified = await PostconditionVerifier(
                    _VerificationClient({("events", "list"): [fresh, stale]})
                ).verify(
                    default_registry().get(capability_name),
                    {"recording_id": 40},
                    None,
                    "10",
                    precondition={"pre_event_ids": ["1"]},
                )
                self.assertTrue(verified["verified"])
                self.assertEqual(verified["event"]["id"], 2)

    async def test_folder_create_requires_exact_project_membership(self):
        arguments = {"name": "Project folder", "project_ids": [10]}
        allowed = _VerificationClient(
            {
                ("folders", "get_folder"): {
                    "id": 4,
                    "name": "Project folder",
                    "projects": [{"id": 10}],
                }
            }
        )
        verified = await PostconditionVerifier(allowed).verify(
            default_registry().get("folders.create"),
            arguments,
            {"id": 4},
            "10",
        )
        self.assertTrue(verified["verified"])

        cross_project = _VerificationClient(
            {
                ("folders", "get_folder"): {
                    "id": 4,
                    "name": "Project folder",
                    "projects": [{"id": 10}, {"id": 99}],
                }
            }
        )
        with self.assertRaisesRegex(VerificationError, "exact requested project"):
            await PostconditionVerifier(cross_project).verify(
                default_registry().get("folders.create"),
                arguments,
                {"id": 4},
                "10",
            )
