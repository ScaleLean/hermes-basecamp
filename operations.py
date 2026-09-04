"""Typed, policy-controlled Basecamp member operation facade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .journal import OperationJournal
    from .policy import (
        ActionApproval,
        CapabilityRegistry,
        ExecutionContext,
        PolicyEngine,
        RiskClass,
        action_digest,
        default_registry,
    )
    from .resource_index import ResourceIndex
    from .verification import PostconditionVerifier
except ImportError:  # pragma: no cover - direct plugin-file loading
    from journal import OperationJournal
    from policy import (
        ActionApproval,
        CapabilityRegistry,
        ExecutionContext,
        PolicyEngine,
        RiskClass,
        action_digest,
        default_registry,
    )
    from resource_index import ResourceIndex
    from verification import PostconditionVerifier


class CapabilityDeniedError(PermissionError):
    """Basecamp returned 403. This is a permission decision, not a retry signal."""


class OperationInProgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationResult:
    capability: str
    project_id: str
    value: Any
    verified: Mapping[str, Any] | None
    replayed: bool = False


def default_journal_path(account_id: str, person_id: str) -> Path:
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home

    configured = str(get_secret("BASECAMP_STATE_DIR", "") or "").strip()
    root = Path(configured).expanduser() if configured else get_hermes_home() / "state" / "basecamp"
    return root / f"{account_id}-{person_id}-operations.sqlite3"


class BasecampOperations:
    """The only public route for broad Basecamp SDK capability execution."""

    def __init__(
        self,
        client: Any,
        *,
        profile_id: str,
        registry: CapabilityRegistry | None = None,
        journal: OperationJournal | None = None,
        max_output_items: int = 200,
    ) -> None:
        self.client = client
        self.profile_id = profile_id
        self.registry = registry or default_registry()
        self.policy = PolicyEngine(self.registry)
        self.resources = ResourceIndex(client)
        self.verifier = PostconditionVerifier(client)
        self.journal = journal or OperationJournal(
            default_journal_path(client.expected.account_id, client.expected.person_id)
        )
        self.max_output_items = max(1, min(max_output_items, 500))

    def unresolved_operations(self):
        return self.journal.unresolved(self.profile_id)

    def resolve_operation_for_retry(
        self,
        name: str,
        project_id: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
        *,
        confirmed: bool,
        confirmed_not_applied: bool = False,
    ) -> str:
        return self.journal.resolve_for_retry(
            profile_id=self.profile_id,
            idempotency_key=idempotency_key,
            capability=name,
            arguments_digest=action_digest(name, project_id, arguments),
            confirmed=confirmed,
            confirmed_not_applied=confirmed_not_applied,
        )

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ExecutionContext,
        *,
        approval: ActionApproval | None = None,
        idempotency_key: str | None = None,
    ) -> OperationResult:
        if context.profile_id != self.profile_id:
            raise PermissionError("Basecamp execution context belongs to another Hermes profile")
        capability = self.policy.authorize(name, arguments, context, approval)
        try:
            await self.client.attest_full_member()
            ownership = await self.resources.resolve(capability, arguments, context.project_id)
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if status == 403:
                raise CapabilityDeniedError(f"Basecamp member lacks permission for {name}") from exc
            raise

        digest = action_digest(name, context.project_id, arguments)
        if capability.risk is not RiskClass.READ:
            if not idempotency_key:
                raise ValueError("Basecamp mutations require an idempotency key")
            existing = self.journal.reserve(
                profile_id=self.profile_id,
                idempotency_key=idempotency_key,
                capability=name,
                arguments_digest=digest,
            )
            if existing is not None:
                if existing.state == "succeeded":
                    stored = existing.result or {}
                    return OperationResult(name, context.project_id, stored.get("value"), stored.get("verified"), True)
                if existing.state in {"reserved", "pending", "dispatched", "uncertain"}:
                    raise OperationInProgressError(
                        "Basecamp operation is pending or uncertain; reconcile it before retrying"
                    )
                raise RuntimeError(existing.error or "A previous Basecamp operation attempt failed")

        try:
            sdk_arguments = {key: value for key, value in arguments.items() if key not in capability.context_arguments}
            if capability.risk is not RiskClass.READ:
                self.journal.mark_dispatched(
                    profile_id=self.profile_id,
                    idempotency_key=str(idempotency_key),
                )
            value = await self.client.call(capability.service, capability.method, sdk_arguments)
            value = self.resources.filter_results(capability, value, context.project_id)
            verified = None
            if capability.risk is not RiskClass.READ:
                verified = await self.verifier.verify(
                    capability,
                    arguments,
                    value,
                    context.project_id,
                    precondition=ownership,
                )
                if verified.get("verified") is not True:
                    raise RuntimeError("Basecamp mutation did not produce verified canonical evidence")
                stored = {"value": value, "verified": verified}
                self.journal.finish(
                    profile_id=self.profile_id,
                    idempotency_key=str(idempotency_key),
                    state="succeeded",
                    result=stored,
                )
            return OperationResult(
                name,
                context.project_id,
                self._bounded(value),
                verified,
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            if capability.risk is not RiskClass.READ:
                # Once a mutation call begins, transport failures are uncertain. A canonical
                # reconciliation must decide whether Basecamp committed it.
                state = "failed" if status == 403 else "uncertain"
                self.journal.finish(
                    profile_id=self.profile_id,
                    idempotency_key=str(idempotency_key),
                    state=state,
                    error=str(exc)[:1000],
                )
            if status == 403:
                raise CapabilityDeniedError(f"Basecamp member lacks permission for {name}") from exc
            raise

    def _bounded(self, value: Any) -> Any:
        if isinstance(value, list):
            return value[: self.max_output_items]
        return value
