"""Approved Basecamp webhook registration and canonical reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:
    from .sdk_client import SAFE_WEBHOOK_RECORDING_GETTERS, UNSAFE_WEBHOOK_RECORDING_TYPES
except ImportError:  # pragma: no cover - installed flat-module wheel
    from sdk_client import SAFE_WEBHOOK_RECORDING_GETTERS, UNSAFE_WEBHOOK_RECORDING_TYPES


@dataclass(frozen=True)
class WebhookReconciliationResult:
    project_id: str
    webhook_id: str
    action: str
    verified: bool


class WebhookReconciler:
    def __init__(self, client: Any, *, payload_url: str, event_types: tuple[str, ...]) -> None:
        if not payload_url.startswith("https://"):
            raise ValueError("Basecamp webhook public URL must use HTTPS")
        if not event_types:
            raise ValueError("At least one Basecamp webhook event type is required")
        unsupported = sorted(set(event_types) - set(SAFE_WEBHOOK_RECORDING_GETTERS))
        if unsupported:
            reasons = [
                f"{item}: {UNSAFE_WEBHOOK_RECORDING_TYPES.get(item, 'no reviewed project-bound SDK getter')}"
                for item in unsupported
            ]
            raise ValueError("Unsafe Basecamp webhook recording types: " + "; ".join(reasons))
        self.client = client
        self.payload_url = payload_url
        self.event_types = tuple(sorted(set(event_types)))

    async def reconcile(self, *, approved: bool) -> tuple[WebhookReconciliationResult, ...]:
        await self.client.attest_full_member()
        results = []
        for project_id in self.client.project_ids:
            existing = await self.client.call("webhooks", "list", {"bucket_id": int(project_id), "max_items": 100})
            matches = [
                item
                for item in existing
                if isinstance(item, Mapping) and str(item.get("payload_url") or "") == self.payload_url
            ]
            if len(matches) > 1:
                raise RuntimeError(f"Multiple Basecamp webhooks match project {project_id}")
            if matches:
                current = matches[0]
                current_types = tuple(sorted(str(value) for value in (current.get("types") or [])))
                if current.get("active") is True and current_types == self.event_types:
                    webhook_id = str(current.get("id") or "")
                    action = "unchanged"
                else:
                    if not approved:
                        raise PermissionError("Basecamp webhook update requires action-time approval")
                    webhook_id = str(current.get("id") or "")
                    await self.client.call(
                        "webhooks",
                        "update",
                        {
                            "webhook_id": int(webhook_id),
                            "payload_url": self.payload_url,
                            "types": list(self.event_types),
                            "active": True,
                        },
                    )
                    action = "updated"
            else:
                if not approved:
                    raise PermissionError("Basecamp webhook creation requires action-time approval")
                created = await self.client.call(
                    "webhooks",
                    "create",
                    {
                        "bucket_id": int(project_id),
                        "payload_url": self.payload_url,
                        "types": list(self.event_types),
                        "active": True,
                    },
                )
                webhook_id = str(created.get("id") or "") if isinstance(created, Mapping) else ""
                action = "created"
            if not webhook_id:
                raise RuntimeError("Basecamp webhook mutation returned no ID")
            scoped = await self.client.call(
                "webhooks", "list", {"bucket_id": int(project_id), "max_items": 100}
            )
            scoped_matches = [
                item
                for item in scoped
                if isinstance(item, Mapping)
                and str(item.get("id") or "") == webhook_id
                and str(item.get("payload_url") or "") == self.payload_url
            ]
            verified = await self.client.call("webhooks", "get", {"webhook_id": int(webhook_id)})
            if not isinstance(verified, Mapping):
                raise TypeError("Basecamp webhook read-back is invalid")
            valid = (
                len(scoped_matches) == 1
                and str(verified.get("payload_url") or "") == self.payload_url
                and verified.get("active") is True
                and tuple(sorted(str(value) for value in (verified.get("types") or []))) == self.event_types
            )
            if not valid:
                raise RuntimeError("Basecamp webhook read-back did not match requested state")
            results.append(WebhookReconciliationResult(project_id, webhook_id, action, True))
        return tuple(results)
