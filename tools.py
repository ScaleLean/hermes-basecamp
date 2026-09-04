"""Compact Hermes tool groups backed by the capability facade."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Hermes uses a top-level ``tools`` package. This standalone plugin is also
# required to expose ``tools.py`` when loaded as ``basecamp_plugin.tools``.
# Preserve Hermes submodule discovery if Python imports this file as top-level
# ``tools`` from a source checkout.
_hermes_spec = importlib.util.find_spec("hermes_cli")
if __name__ == "tools" and _hermes_spec and _hermes_spec.origin:
    __path__ = [str(Path(_hermes_spec.origin).resolve().parent.parent / "tools")]

try:
    from .operations import BasecampOperations
    from .policy import (
        ActionApproval,
        ExecutionContext,
        RiskClass,
        TriggerClass,
        default_registry,
        requires_action_approval,
    )
    from .trusted_context import derive_execution_context
except ImportError:  # pragma: no cover
    from operations import BasecampOperations
    from policy import (
        ActionApproval,
        ExecutionContext,
        RiskClass,
        TriggerClass,
        default_registry,
        requires_action_approval,
    )
    from trusted_context import derive_execution_context


DOMAIN_TOOLS = {
    "projects_people": ("projects.", "people."),
    "todos": ("todos.", "todolists.", "todolist_groups."),
    "messages": ("messages.", "comments.", "campfire.", "message_boards.", "message_types."),
    "cards": ("cards.", "card_columns.", "card_tables.", "wormholes."),
    "schedules_checkins": ("schedules.", "checkins.", "calendars.", "events."),
    "files": (
        "documents.",
        "uploads.",
        "attachments.",
        "vaults.",
        "cloud_files.",
        "google_documents.",
        "client_visibility.",
    ),
    "discovery": ("search.", "reports.", "activity.", "timesheets."),
    "extras": (
        "subscriptions.",
        "bookmarks.",
        "boosts.",
        "bubbleups.",
        "gauges.",
        "hillcharts.",
        "recordings.",
        "automation.",
        "calendars.",
        "card_tables.",
        "client_approvals.",
        "client_correspondences.",
        "client_replies.",
        "client_visibility.",
        "cloud_files.",
        "drafts.",
        "events.",
        "everything.",
        "forwards.",
        "google_documents.",
        "lineup.",
        "message_boards.",
        "message_types.",
        "my_assignments.",
        "my_notes.",
        "my_notifications.",
        "todosets.",
        "webhooks.",
        "tools.",
        "assignments.",
        "dock_tools.",
    ),
}


class BasecampDomainTool:
    def __init__(self, domain: str, operations: BasecampOperations) -> None:
        if domain not in DOMAIN_TOOLS:
            raise ValueError(f"Unknown Basecamp tool domain: {domain}")
        self.domain = domain
        self.operations = operations

    def capabilities(self) -> tuple[dict[str, str], ...]:
        prefixes = DOMAIN_TOOLS[self.domain]
        return tuple(
            {
                "name": item.name,
                "risk": item.risk.value,
                "domain": item.domain,
            }
            for item in self.operations.registry.list()
            if item.name.startswith(prefixes)
        )

    async def run(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        context: ExecutionContext,
        *,
        approval: ActionApproval | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        prefixes = DOMAIN_TOOLS[self.domain]
        if not operation.startswith(prefixes):
            raise PermissionError(f"{operation} is outside the {self.domain} Basecamp tool")
        result = await self.operations.execute(
            operation,
            arguments,
            context,
            approval=approval,
            idempotency_key=idempotency_key,
        )
        return {
            "operation": result.capability,
            "project_id": result.project_id,
            "result": result.value,
            "verified": result.verified,
            "replayed": result.replayed,
        }


def build_domain_tools(operations: BasecampOperations) -> tuple[BasecampDomainTool, ...]:
    return tuple(BasecampDomainTool(name, operations) for name in DOMAIN_TOOLS)


def register_tools(ctx: Any, client_factory: Any, profile_id: str | None = None) -> None:
    """Register eight compact domain tools on Hermes's generic plugin surface."""
    registry = default_registry()
    for domain, prefixes in DOMAIN_TOOLS.items():
        names = [item.name for item in registry.list() if item.name.startswith(prefixes)]
        schema = {
            "name": f"basecamp_{domain}",
            "description": f"Run a policy-controlled Basecamp {domain.replace('_', ' ')} operation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": names},
                    "project_id": {"type": "string", "pattern": "^[0-9]+$"},
                    "arguments": {"type": "object"},
                    "idempotency_key": {"type": "string", "minLength": 8},
                },
                "required": ["operation", "project_id", "arguments"],
                "additionalProperties": False,
            },
        }

        async def handler(
            operation: str,
            project_id: str,
            arguments: Mapping[str, Any],
            idempotency_key: str | None = None,
            *,
            _domain: str = domain,
        ) -> dict[str, Any]:
            capability = registry.get(operation)
            if capability.risk is RiskClass.ADMINLAND_DENIED:
                raise PermissionError("Basecamp Adminland operations are not automated")
            client = client_factory()
            try:
                active_profile_id = f"{client.expected.account_id}:{client.expected.person_id}"
                if profile_id is not None and profile_id != active_profile_id:
                    raise PermissionError("Basecamp tool client belongs to another Hermes profile")
                approval = None
                execution_context = ExecutionContext(TriggerClass.UNATTENDED, project_id, active_profile_id)
                if capability.risk is RiskClass.ORDINARY_WRITE and not requires_action_approval(capability, arguments):
                    execution_context = derive_execution_context(project_id, active_profile_id)
                if requires_action_approval(capability, arguments):
                    try:
                        from .approval import APPROVAL_BROKER
                    except ImportError:  # pragma: no cover
                        from approval import APPROVAL_BROKER
                    approval = APPROVAL_BROKER.consume(operation, project_id, arguments, profile_id=active_profile_id)
                    if approval is None:
                        raise PermissionError("Basecamp action-time approval is missing or expired")
                operations = BasecampOperations(client, profile_id=active_profile_id, registry=registry)
                tool = BasecampDomainTool(_domain, operations)
                return await tool.run(
                    operation,
                    arguments,
                    execution_context,
                    approval=approval,
                    idempotency_key=idempotency_key,
                )
            finally:
                await client.close()

        ctx.register_tool(
            name=f"basecamp_{domain}",
            toolset="basecamp",
            schema=schema,
            handler=handler,
            is_async=True,
            description=schema["description"],
            emoji="⛺",
        )
