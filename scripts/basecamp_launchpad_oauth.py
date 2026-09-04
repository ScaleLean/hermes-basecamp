#!/usr/bin/env python3
"""Authorize one Basecamp member through Launchpad without printing secrets."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launchpad_oauth import begin_authorization, finish_authorization, load_app_credentials


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-credentials", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--person-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = begin_authorization(load_app_credentials(args.app_credentials))
    print(json.dumps({"authorization_url": request.url}), flush=True)

    callback: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self.path.startswith("/callback?"):
                self.send_response(204)
                self.end_headers()
                return
            callback.append(f"http://127.0.0.1:{self.server.server_port}{self.path}")
            body = b"Basecamp authorization received. You may close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *unused) -> None:
            pass

    port = int(request.credentials.redirect_uri.rsplit(":", 1)[1].split("/", 1)[0])
    server = HTTPServer(("127.0.0.1", port), Handler)
    while not callback:
        server.handle_request()
    finish_authorization(
        request,
        callback[0],
        account_id=args.account_id,
        person_id=args.person_id,
        email=args.email,
        output_path=args.output,
    )
    print(json.dumps({"saved": str(args.output.expanduser().resolve()), "identity_verified": True}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
