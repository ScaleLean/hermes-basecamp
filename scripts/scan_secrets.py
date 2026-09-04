"""Fail CI on high-confidence credentials without printing matched values."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

PATTERNS = (
    re.compile("BEGIN " + "PRIVATE KEY"),
    re.compile("gh" + r"[pousr]_[A-Za-z0-9_]{30,}"),
    re.compile("xox" + r"[aboprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)(client_secret|access_token|refresh_token|webhook_token)\s*[=:]\s*['\"]?[A-Za-z0-9_./+-]{24,}"),
)

SAFE_NON_SECRETS = (
    "BASECAMP_WEBHOOK_TOKEN=replace-with-at-least-32-random-characters",
    "client_secret=request.credentials.client_secret,",
    "refresh_token=credentials.refresh_token,",
)


def scan(lines: list[str]) -> list[int]:
    return [
        number
        for number, line in enumerate(lines, 1)
        if line.strip().lstrip("+- ") not in SAFE_NON_SECRETS
        and any(pattern.search(line) for pattern in PATTERNS)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    if args.history:
        result = subprocess.run(
            ["git", "log", "-p", "--all", "--no-ext-diff"],
            check=True,
            capture_output=True,
            text=True,
        )
        findings = scan(result.stdout.splitlines())
        label = "Git history"
    else:
        result = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True)
        findings = []
        for name in result.stdout.splitlines():
            path = Path(name)
            try:
                findings.extend(scan(path.read_text(encoding="utf-8").splitlines()))
            except (OSError, UnicodeDecodeError):
                continue
        label = "tracked files"
    if findings:
        print(f"Potential credential material found in {label}; matched lines are intentionally redacted.")
        return 1
    print(f"No high-confidence credential material found in {label}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
