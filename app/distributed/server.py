"""Isolated analysis endpoint. Not mounted on the trading dashboard."""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .coordinator import ShadowCoordinator, utc_now
from .protocol import MAX_BYTES, canonical


def make_server(address: tuple[str, int], coordinator: ShadowCoordinator) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # Do not log authorization or request data.

        def authenticated(self) -> bool:
            return hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + coordinator.key.hex()
            )

        def respond(self, status: int, body: Any = None) -> None:
            content = b"" if body is None else canonical(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            if not self.authenticated():
                self.respond(401)
                return
            path = urlsplit(self.path)
            if path.path != "/v1/jobs":
                self.respond(404)
                return
            try:
                workers = parse_qs(path.query).get("worker", [])
                if len(workers) != 1:
                    raise ValueError("worker id required")
                job = coordinator.claim(workers[0], now=utc_now())
                self.respond(204 if job is None else 200, job)
            except (ValueError, TypeError):
                self.respond(400)

        def do_POST(self) -> None:
            if not self.authenticated():
                self.respond(401)
                return
            if self.path != "/v1/results":
                self.respond(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES:
                    raise ValueError("invalid body size")
                coordinator.accept(json.loads(self.rfile.read(length)), now=utc_now())
                self.respond(200, {"accepted": True, "shadow_only": True})
            except (ValueError, KeyError, TypeError, AttributeError):
                self.respond(400)

    # Deliberately local-only: publishing requires a separate authenticated TLS/VPN setup.
    if address[0] not in {"127.0.0.1", "localhost"}:
        raise ValueError("analysis server must remain loopback until secure relay is configured")
    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server
