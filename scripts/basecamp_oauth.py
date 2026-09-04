#!/usr/bin/env python3
"""Authorize a Basecamp member without exposing the resulting credential."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oauth_onboarding import authorize


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--person-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def display(auth) -> None:
        print(
            json.dumps(
                {
                    "verification_uri": auth.verification_uri,
                    "verification_uri_complete": auth.verification_uri_complete,
                    "user_code": auth.user_code,
                    "expires_in": auth.expires_in,
                }
            ),
            flush=True,
        )

    token = authorize(
        account_id=args.account_id,
        person_id=args.person_id,
        email=args.email,
        output_path=args.output,
        display=display,
    )
    print(
        json.dumps(
            {
                "saved": str(args.output.expanduser().resolve()),
                "resource": token.resource,
                "scope": token.scope,
                "identity_verified": True,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
