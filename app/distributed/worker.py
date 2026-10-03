"""Portable stdlib worker: outbound HTTPS only, no broker configuration or imports."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .coordinator import utc_now
from .protocol import MAX_BYTES, VERSION, calculate, canonical, sign, timestamp, verify


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        raise ValueError("redirect refused; analysis credentials must stay on one origin")


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("plain analysis server origin required")
    if parsed.path not in {"", "/"} or not parsed.hostname:
        raise ValueError("plain analysis server origin required")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("HTTPS required outside loopback")


def execute(envelope: dict[str, Any], key: bytes, worker_id: str) -> dict[str, Any]:
    job = verify(envelope, key)
    if set(job) != {"version", "job_id", "worker_id", "attempt", "expires_at", "payload"}:
        raise ValueError("unexpected job fields")
    if job["version"] != VERSION or job["worker_id"] != worker_id:
        raise ValueError("foreign assignment or unsupported operation")
    if timestamp(job["expires_at"]) <= utc_now():
        raise ValueError("expired assignment")
    return sign(
        {
            "version": VERSION,
            "job_id": job["job_id"],
            "worker_id": worker_id,
            "attempt": job["attempt"],
            "metrics": calculate(job["payload"]),
        },
        key,
    )


def run_once(origin: str, key: bytes, worker_id: str) -> bool:
    validate_url(origin)
    opener = build_opener(NoRedirect())
    headers = {"Authorization": "Bearer " + key.hex()}
    request = Request(
        origin.rstrip("/") + "/v1/jobs?" + urlencode({"worker": worker_id}), headers=headers
    )
    with opener.open(request, timeout=15) as response:
        if response.status == 204:
            return False
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("job body too large")
    result = execute(json.loads(data), key, worker_id)
    request = Request(
        origin.rstrip("/") + "/v1/results",
        data=canonical(result),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError("result not accepted")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="AEGIS shadow compute worker, never trades")
    parser.add_argument("--server", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    validate_url(args.server)
    key = bytes.fromhex(args.key_file.read_text().strip())
    if len(key) != 32:
        raise ValueError("dedicated 32-byte analysis key required")
    while True:
        try:
            worked = run_once(args.server, key, args.worker_id)
            print("ANALYSIS_COMPLETED" if worked else "WAITING_FOR_ANALYSIS", flush=True)
        except (HTTPError, OSError, ValueError, KeyError, TypeError):
            # No exception messages, credentials or URLs written to logs.
            print("ANALYSIS_CONNECTION_OR_VALIDATION_FAILED", flush=True)
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
