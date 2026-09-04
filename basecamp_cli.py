"""Async, identity-locked wrapper around the official Basecamp CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BasecampCLIError(RuntimeError):
    """The Basecamp CLI failed or returned an invalid response."""


class IdentityMismatchError(BasecampCLIError):
    """The authenticated Basecamp member is not the configured agent."""


@dataclass(frozen=True)
class ExpectedIdentity:
    account_id: str
    person_id: str
    email: str


class BasecampCLI:
    """Run the official CLI without a shell and verify its active identity."""

    def __init__(
        self,
        *,
        profile: str,
        expected: ExpectedIdentity,
        executable: str = "basecamp",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.profile = profile.strip()
        self.expected = expected
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        if not self.profile:
            raise ValueError("Basecamp CLI profile is required")
        if not all((expected.account_id, expected.person_id, expected.email)):
            raise ValueError("Basecamp account ID, person ID, and email are required")

    def command(self, args: Sequence[str]) -> list[str]:
        return [
            self.executable,
            "--profile",
            self.profile,
            "--account",
            self.expected.account_id,
            "--json",
            *args,
        ]

    async def run(self, args: Sequence[str]) -> Any:
        command = self.command(args)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        except FileNotFoundError as exc:
            raise BasecampCLIError(f"Basecamp CLI not found: {self.executable}") from exc
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise BasecampCLIError("Basecamp CLI command timed out") from exc

        text = stdout.decode("utf-8", errors="replace").strip()
        error_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = error_text or text or f"exit {process.returncode}"
            raise BasecampCLIError(f"Basecamp CLI failed: {detail[:500]}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BasecampCLIError("Basecamp CLI returned non-JSON output") from exc
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            raise BasecampCLIError(str(payload.get("error") or "Basecamp CLI request failed"))
        if isinstance(payload, Mapping) and "data" in payload:
            return payload["data"]
        return payload

    async def verify_identity(self) -> Mapping[str, Any]:
        data = await self.run(["me"])
        if not isinstance(data, Mapping):
            raise IdentityMismatchError("Basecamp identity response is not an object")

        actual_id = str(data.get("id") or "")
        actual_email = str(data.get("email_address") or data.get("email") or "").lower()
        expected_email = self.expected.email.lower()
        if actual_id != self.expected.person_id or actual_email != expected_email:
            raise IdentityMismatchError(
                "Basecamp identity mismatch: expected person "
                f"{self.expected.person_id} <{expected_email}>, got "
                f"{actual_id or 'missing'} <{actual_email or 'missing'}>"
            )
        return data

    async def timeline(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        data = await self.run(["timeline", "--limit", str(limit)])
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            entries = data.get("events") or data.get("items") or []
            if isinstance(entries, list):
                return [item for item in entries if isinstance(item, Mapping)]
        raise BasecampCLIError("Basecamp timeline response has an unsupported shape")

    async def show(self, locator: str) -> Mapping[str, Any]:
        data = await self.run(["show", locator])
        if not isinstance(data, Mapping):
            raise BasecampCLIError("Basecamp item response is not an object")
        return data

    async def verify_authorship(self, locator: str) -> Mapping[str, Any]:
        data = await self.show(locator)
        creator = data.get("creator") or {}
        if not isinstance(creator, Mapping):
            creator = {}
        creator_id = str(creator.get("id") or "")
        if creator_id != self.expected.person_id:
            raise IdentityMismatchError(
                "Basecamp write attribution mismatch: expected creator "
                f"{self.expected.person_id}, got {creator_id or 'missing'}"
            )
        return data

    async def post_chat(self, project_id: str, room_id: str, content: str) -> Mapping[str, Any]:
        data = await self.run(["chat", "post", content, "--project", project_id, "--room", room_id])
        if not isinstance(data, Mapping):
            raise BasecampCLIError("Basecamp chat response is not an object")
        return data

    async def post_comment(self, project_id: str, recording_id: str, content: str) -> Mapping[str, Any]:
        data = await self.run(["comments", "create", recording_id, content, "--project", project_id])
        if not isinstance(data, Mapping):
            raise BasecampCLIError("Basecamp comment response is not an object")
        return data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-basecamp")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prerequisites", help="Print human-owned Basecamp setup steps")
    commands.add_parser("doctor", help="Verify identity, full-member role, and project membership")
    revoke = commands.add_parser("revoke", help="Remove the configured local OAuth token file")
    revoke.add_argument("--token-file", type=Path)
    revoke.add_argument("--yes", action="store_true")
    webhooks = commands.add_parser("webhook-reconcile", help="Create or repair project webhooks")
    webhooks.add_argument("--public-url", required=True)
    webhooks.add_argument("--types", required=True, help="Comma-separated Basecamp recording types")
    webhooks.add_argument("--yes", action="store_true")
    journal_list = commands.add_parser("journal-list", help="List unresolved Basecamp mutations")
    journal_list.add_argument("--profile-id", required=True)
    journal_release = commands.add_parser(
        "journal-release", help="Release one exactly matched unresolved mutation for retry"
    )
    journal_release.add_argument("--profile-id", required=True)
    journal_release.add_argument("--idempotency-key", required=True)
    journal_release.add_argument("--operation", required=True)
    journal_release.add_argument("--project-id", required=True)
    journal_release.add_argument("--arguments-json", required=True)
    journal_release.add_argument("--confirmed-not-applied", action="store_true")
    journal_release.add_argument("--yes", action="store_true")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    if args.command == "prerequisites":
        from onboarding import ADMINLAND_PREREQUISITES

        for index, item in enumerate(ADMINLAND_PREREQUISITES, 1):
            print(f"{index}. {item}")
        return 0
    if args.command == "revoke":
        from onboarding import revoke_local_token

        configured = os.getenv("BASECAMP_OAUTH_TOKEN_FILE", "").strip()
        if args.token_file is None and not configured:
            raise SystemExit("BASECAMP_OAUTH_TOKEN_FILE or --token-file is required")
        path = args.token_file or Path(configured)
        if configured and path.expanduser().resolve() != Path(configured).expanduser().resolve():
            raise SystemExit("--token-file must match BASECAMP_OAUTH_TOKEN_FILE")
        removed = revoke_local_token(path, approved=args.yes)
        print(json.dumps({"ok": removed, "local_token_present": path.expanduser().exists()}))
        return 0 if removed else 1

    # Import through Hermes's plugin loader for commands using profile-scoped configuration.
    from scripts._plugin_loader import load_plugin

    load_plugin()
    from basecamp_plugin.adapter import _make_client, _settings
    from basecamp_plugin.onboarding import doctor

    client = _make_client(_settings())
    try:
        if args.command in {"journal-list", "journal-release"}:
            from basecamp_plugin.operations import BasecampOperations

            profile_id = f"{client.expected.account_id}:{client.expected.person_id}"
            if args.profile_id != profile_id:
                raise SystemExit("--profile-id must match the configured Basecamp identity")
            operations = BasecampOperations(client, profile_id=profile_id)
            if args.command == "journal-list":
                print(json.dumps([entry.__dict__ for entry in operations.unresolved_operations()]))
                return 0
            arguments = json.loads(args.arguments_json)
            if not isinstance(arguments, Mapping):
                raise SystemExit("--arguments-json must be a JSON object")
            disposition = operations.resolve_operation_for_retry(
                args.operation,
                args.project_id,
                arguments,
                args.idempotency_key,
                confirmed=args.yes,
                confirmed_not_applied=args.confirmed_not_applied,
            )
            print(json.dumps({"released": True, "disposition": disposition}))
            return 0
        if args.command == "webhook-reconcile":
            from basecamp_plugin.webhook_reconciliation import WebhookReconciler

            reconciler = WebhookReconciler(
                client,
                payload_url=args.public_url,
                event_types=tuple(value.strip() for value in args.types.split(",") if value.strip()),
            )
            results = await reconciler.reconcile(approved=args.yes)
            print(json.dumps([result.__dict__ for result in results]))
            return 0
        report = await doctor(client)
        print(json.dumps(report.__dict__))
        return 0 if report.healthy else 1
    finally:
        await client.close()


def main() -> int:
    return asyncio.run(_main_async(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
