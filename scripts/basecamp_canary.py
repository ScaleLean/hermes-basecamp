#!/usr/bin/env python3
"""Post and verify one explicitly approved Basecamp canary message."""

from __future__ import annotations

import argparse
import asyncio
import json

from _plugin_loader import load_plugin


async def run(target: str, message: str) -> int:
    load_plugin()
    from basecamp_plugin.adapter import _settings, _standalone_send

    result = await _standalone_send(_settings(), target, message)
    print(json.dumps(result))
    return 0 if result.get("success") else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="bucket:PROJECT_ID/recording:RECORDING_ID")
    parser.add_argument("--message", required=True)
    parser.add_argument("--yes", action="store_true", help="Confirm this exact live Basecamp write")
    args = parser.parse_args()
    if not args.yes:
        parser.error("a live Basecamp write requires --yes")
    return asyncio.run(run(args.target, args.message))


if __name__ == "__main__":
    raise SystemExit(main())
