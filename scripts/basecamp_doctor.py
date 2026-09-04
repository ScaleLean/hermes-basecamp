#!/usr/bin/env python3
"""Verify Basecamp configuration, identity, and allowlisted project access."""

from __future__ import annotations

import asyncio
import json

from _plugin_loader import load_plugin


async def main() -> int:
    load_plugin()
    from basecamp_plugin.adapter import _make_client, _settings
    from basecamp_plugin.onboarding import doctor

    values = _settings()
    client = _make_client(values)
    try:
        report = await doctor(client)
        if not report.healthy:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "account_id": report.account_id,
                        "person_id": report.person_id,
                        "project_ids": list(report.project_ids),
                        "problems": list(report.problems),
                        "health": report.health,
                    }
                )
            )
            return 1
        campfires = await client.campfires(max_items=None)
        print(
            json.dumps(
                {
                    "ok": True,
                    "account_id": report.account_id,
                    "person_id": report.person_id,
                    "email": report.email,
                    "project_ids": list(report.project_ids),
                    "campfire_ids": [str(item.get("id")) for item in campfires],
                    "health": report.health,
                }
            )
        )
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
