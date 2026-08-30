"""Dependency-free local HTTP server for the read-only Aegis Home."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from app.config import load_config, load_runtime_values
from app.data.runtime import build_readonly_active_scan_cycle_report
from app.web.home import home_snapshot_from_scan_cycle

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"


class AegisHomeHandler(BaseHTTPRequestHandler):
    """Only GET is implemented; all mutation methods are rejected."""

    server_version = "AegisReadOnly/1.0"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/home":
            self._send_home()
            return
        static_files = {
            "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/index.html": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
            "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
        }
        if path in static_files:
            file_path, content_type = static_files[path]
            self._send_file(file_path, content_type)
            return
        self._send_json(404, {"status": "ERROR", "error": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        self._send_json(405, {"status": "ERROR", "error": "READ_ONLY_METHOD_NOT_ALLOWED"})

    def do_PUT(self) -> None:  # noqa: N802
        self.do_POST()

    def do_PATCH(self) -> None:  # noqa: N802
        self.do_POST()

    def do_DELETE(self) -> None:  # noqa: N802
        self.do_POST()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_home(self) -> None:
        try:
            config = load_config(load_runtime_values())
            report = build_readonly_active_scan_cycle_report(config)
            snapshot = home_snapshot_from_scan_cycle(report)
            self._send_json(200, snapshot.model_dump(mode="json"))
        except Exception:
            self._send_json(
                503,
                {
                    "status": "ERROR",
                    "error": "HOME_SNAPSHOT_UNAVAILABLE",
                    "message": "Read-only scanner state is temporarily unavailable.",
                },
            )

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"status": "ERROR", "error": "STATIC_ASSET_NOT_FOUND"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(*, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), AegisHomeHandler)
    print(f"Aegis Home: http://{host}:{port}")
    print("Read-only mode: broker writes disabled")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
