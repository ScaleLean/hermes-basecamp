"""Frozen public-method dispositions for basecamp-sdk 0.16.0."""

from __future__ import annotations

import hashlib
import inspect
from typing import Any

SDK_016_PUBLIC_METHOD_DIGEST = "409d0ef955f2e721896f3af70b0db5faf84236e01304d1d8c2f291ecf8ad9e0e"

EXCLUSION_REASONS = {
    "account": "Adminland account mutation, logo, billing, security, or ownership boundary",
    "templates": "Template, library, and project-creation APIs are outside project-member scope",
    "http": "Raw HTTP bypasses the typed official SDK resource boundary",
}

EXCLUDED_SERVICES = frozenset(EXCLUSION_REASONS)

INTERNAL_METHODS = {
    "attachments.create": "native media upload helper",
    "my_notifications.get_my_notifications": "notification ingress helper",
    "webhooks.create": "approved webhook reconciliation helper",
    "webhooks.get": "webhook canonical read-back helper",
    "webhooks.list": "webhook reconciliation helper",
    "webhooks.update": "approved webhook reconciliation helper",
}

EXCLUDED_METHOD_REASONS = {
    "automation.list_lineup_markers": "Account-wide lineup markers do not expose a canonical project identifier for allowlist filtering",
    "bubble_ups.create_bubble_up": "No readable recording identifier is returned for project-bound read-back",
    "bubble_ups.delete_bubble_up": "No readable recording identifier is returned for project-bound deletion read-back",
    **{
        method: "Campfire chatbot credential lifecycle is a separate chatbot-key identity surface, not full-member activity"
        for method in (
            "campfires.create_chatbot",
            "campfires.delete_chatbot",
            "campfires.get_chatbot",
            "campfires.list_chatbots",
            "campfires.update_chatbot",
        )
    },
    "cards.update": "Alias of the registered cards.update_verbatim method",
    "checkins.reminders": "Account-wide check-in reminders do not expose canonical project IDs for allowlist filtering",
    "checkins.update_notification_settings": "SDK request fields do not match the readable notification-settings response, so exact state cannot be verified",
    "campfires.create_upload": "Raw-byte upload is excluded from generic tools; validated media delivery must use the bounded media pipeline",
    "client_visibility.set_visibility": "Visibility mutation has no resource-type getter for independent current-state read-back",
    **{
        method: "Legacy Basecamp client-inbox API does not expose a canonical project-bound resource contract"
        for method in (
            "client_approvals.get",
            "client_correspondences.get",
            "client_replies.get",
        )
    },
    "documents.replace": "Destructive full replacement; the merge-safe documents.update method is registered",
    **{
        f"everything.{method}": "Account-wide aggregate duplicates registered project-scoped reads and can expose denied-project aggregate structure"
        for method in (
            "get_everything_checkins",
            "get_everything_comments",
            "get_everything_completed_cards",
            "get_everything_completed_todos",
            "get_everything_files",
            "get_everything_forwards",
            "get_everything_messages",
            "get_everything_no_due_date_cards",
            "get_everything_no_due_date_todos",
            "get_everything_not_now_cards",
            "get_everything_open_cards",
            "get_everything_open_todos",
            "get_everything_overdue_cards",
            "get_everything_overdue_todos",
            "get_everything_unassigned_cards",
            "get_everything_unassigned_todos",
        )
    },
    **{
        f"lineup.{method}": "Account-wide lineup mutation cannot be constrained to one pre-existing allowlisted project"
        for method in ("create", "delete", "update")
    },
    "my_notes.get_my_note": "Personal My Notes content is identity-global and outside project-member scope",
    "my_notes.update_my_note": "Personal My Notes content is identity-global and outside project-member scope",
    "my_notifications.get_bubble_ups": "Notification bubble-ups expose opaque readable SGIDs without a canonical project-bound recording ID",
    "my_notifications.mark_as_read": "Notification read mutation accepts only an opaque readable SGID, so project ownership cannot be proved before mutation",
    **{
        method: "Out-of-office state is identity-global personal status, not an allowlisted project resource"
        for method in (
            "people.disable_out_of_office",
            "people.enable_out_of_office",
            "people.get_out_of_office",
        )
    },
    **{
        method: "Profile and preference state is identity-global personal configuration, not an allowlisted project resource"
        for method in (
            "people.get_my_preferences",
            "people.my_profile",
            "people.update_my_preferences",
            "people.update_my_profile",
        )
    },
    **{
        method: "Account-wide people list does not expose a project constraint; project member listing is registered instead"
        for method in (
            "people.list",
            "people.list_assignable",
            "people.list_pingable",
        )
    },
    "projects.create": "A new project has no pre-existing project ID that can satisfy the allowlist boundary",
    "projects.record_project_visit": "Visit telemetry has no durable project-member work result or canonical mutation read-back",
    "schedules.replace_entry": "Destructive full replacement; the partial schedules.update_entry method is registered",
    "todolists.replace": "Destructive full replacement; the merge-safe todolists.update method is registered",
    "todos.replace": "Destructive full replacement; the merge-safe todos.update method is registered",
    "uploads.download": "Binary download is excluded from generic JSON tools; validated media receipt must use the bounded media pipeline",
}

EXCLUDED_METHODS = frozenset(EXCLUDED_METHOD_REASONS)


def public_methods(account: Any) -> tuple[str, ...]:
    values = []
    for service_name in dir(account):
        if service_name.startswith("_"):
            continue
        service = getattr(account, service_name)
        for method_name in dir(service):
            if not method_name.startswith("_") and inspect.iscoroutinefunction(getattr(service, method_name, None)):
                values.append(f"{service_name}.{method_name}")
    return tuple(sorted(values))


def inventory_digest(methods: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(methods).encode()).hexdigest()


def disposition(method: str, registered: set[str]) -> tuple[str, str]:
    if method in registered:
        return "registered", "policy-controlled public operation"
    if method in INTERNAL_METHODS:
        return "internal", INTERNAL_METHODS[method]
    if method in EXCLUDED_METHOD_REASONS:
        return "excluded", EXCLUDED_METHOD_REASONS[method]
    service = method.split(".", 1)[0]
    if service in EXCLUSION_REASONS:
        return "excluded", EXCLUSION_REASONS[service]
    if service in EXCLUDED_SERVICES:
        return "excluded", EXCLUSION_REASONS[service]
    if method in EXCLUDED_METHODS:
        return "excluded", EXCLUDED_METHOD_REASONS[method]
    return "unclassified", ""
