import time
import unittest

from policy import (
    ActionApproval,
    ExecutionContext,
    PolicyDeniedError,
    RiskClass,
    TriggerClass,
    UnknownCapabilityError,
    action_digest,
    default_registry,
)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.registry = default_registry()
        from policy import PolicyEngine

        self.policy = PolicyEngine(self.registry)
        self.context = ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile")

    def test_every_capability_has_a_typed_risk(self):
        self.assertGreater(len(self.registry.list()), 60)
        self.assertTrue(all(isinstance(item.risk, RiskClass) for item in self.registry.list()))

    def test_unknown_capability_fails_closed(self):
        with self.assertRaises(UnknownCapabilityError):
            self.policy.authorize("unknown.run", {}, self.context)

    def test_unattended_ordinary_write_is_denied(self):
        context = ExecutionContext(TriggerClass.UNATTENDED, "10", "profile")
        with self.assertRaises(PolicyDeniedError):
            self.policy.authorize("todos.complete", {"todo_id": 1}, context)

    def test_manual_self_declaration_is_not_an_approved_context(self):
        context = ExecutionContext(TriggerClass.MANUAL, "10", "profile")
        with self.assertRaises(PolicyDeniedError):
            self.policy.authorize("todos.complete", {"todo_id": 1}, context)

    def test_sensitive_sdk_fields_require_exact_approval_but_remain_available(self):
        context = ExecutionContext(TriggerClass.DIRECT_MENTION, "10", "profile")
        values = {
            "admissions": "invite",
            "assignee_ids": [2],
            "completion_subscriber_ids": [2],
            "create": [{"email_address": "person@example.com"}],
            "grant": [2],
            "participant_ids": [2],
            "person_id": 2,
            "revoke": [2],
            "schedule": {"frequency": "weekly"},
            "schedule_attributes": {"starts_on": "2026-09-04"},
            "paused": True,
            "subscriptions": [2],
            "unsubscriptions": [2],
            "visible_to_clients": False,
            "notify": True,
        }
        for key, value in values.items():
            arguments = {"todo_id": 1, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(PermissionError, "action-time approval"):
                self.policy.authorize("todos.update", arguments, context)
            approval = ActionApproval(
                "todos.update",
                "10",
                action_digest("todos.update", "10", arguments),
                "Human User",
                time.time() + 60,
            )
            self.assertEqual(
                self.policy.authorize("todos.update", arguments, context, approval).name,
                "todos.update",
            )

        for notify in ("true", 1, [], {}):
            arguments = {"todo_id": 1, "notify": notify}
            with self.subTest(notify=notify), self.assertRaisesRegex(PermissionError, "action-time approval"):
                self.policy.authorize("todos.update", arguments, context)
            approval = ActionApproval(
                "todos.update",
                "10",
                action_digest("todos.update", "10", arguments),
                "Human User",
                time.time() + 60,
            )
            self.assertEqual(
                self.policy.authorize("todos.update", arguments, context, approval).name,
                "todos.update",
            )

    def test_sensitive_approval_is_exact_and_expires(self):
        arguments = {"recording_id": 1}
        approval = ActionApproval(
            "recordings.trash",
            "10",
            action_digest("recordings.trash", "10", arguments),
            "Human User",
            time.time() + 60,
        )
        item = self.policy.authorize("recordings.trash", arguments, self.context, approval)
        self.assertEqual(item.risk, RiskClass.SENSITIVE_WRITE)
        with self.assertRaises(PolicyDeniedError):
            self.policy.authorize("recordings.trash", {"recording_id": 2}, self.context, approval)
        expired = ActionApproval(
            "recordings.trash",
            "10",
            action_digest("recordings.trash", "10", arguments),
            "Human User",
            time.time() - 1,
        )
        with self.assertRaises(PolicyDeniedError):
            self.policy.authorize("recordings.trash", arguments, self.context, expired)

    def test_adminland_is_never_automated(self):
        with self.assertRaisesRegex(PolicyDeniedError, "Adminland"):
            self.policy.authorize("admin.billing", {}, self.context)

    def test_merge_safe_update_methods_are_registered(self):
        self.assertEqual(self.registry.get("todos.update").method, "update")
        self.assertEqual(self.registry.get("documents.update").method, "update")
        self.assertEqual(self.registry.get("schedules.update").method, "update_entry")

    def test_irreversible_project_structure_changes_are_sensitive(self):
        for name in (
            "card_columns.create",
            "dock_tools.delete",
            "message_types.delete",
            "recordings.spotlight",
            "webhooks.delete",
            "wormholes.create",
            "checkins.pause",
            "checkins.resume",
            "schedules.settings",
            "gauges.toggle",
        ):
            self.assertEqual(self.registry.get(name).risk, RiskClass.SENSITIVE_WRITE)

    def test_unverifiable_bubble_up_mutations_are_not_registered(self):
        with self.assertRaises(UnknownCapabilityError):
            self.registry.get("bubbleups.create")
        with self.assertRaises(UnknownCapabilityError):
            self.registry.get("bubbleups.delete")

    def test_unverifiable_client_visibility_mutation_is_not_registered(self):
        with self.assertRaises(UnknownCapabilityError):
            self.registry.get("client_visibility.set")

    def test_binary_transfers_and_unreadable_checkin_settings_are_not_generic_tools(self):
        for name in (
            "campfire.upload",
            "uploads.download",
            "checkins.notifications.update",
        ):
            with self.subTest(name=name), self.assertRaises(UnknownCapabilityError):
                self.registry.get(name)
