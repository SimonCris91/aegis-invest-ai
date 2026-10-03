"""Bounded leased shadow queue. It cannot submit orders or change scanner decisions."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from .protocol import VERSION, canonical, sign, timestamp, validate_payload, verify


class ShadowCoordinator:
    def __init__(self, key: bytes, *, capacity: int = 256) -> None:
        if len(key) < 32 or capacity < 1:
            raise ValueError("analysis key and positive queue capacity required")
        self.key = key
        self.capacity = capacity
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    def submit(self, payload: dict[str, Any], *, now: datetime) -> str:
        validate_payload(payload)
        # Old/future snapshots cannot be smuggled into a new job with a fresh expiry.
        age = now - timestamp(payload["as_of"])
        if now.tzinfo is None or not timedelta(0) <= age <= timedelta(minutes=5):
            raise ValueError("snapshot is stale or in the future")
        job_id = hashlib.sha256(canonical(payload)).hexdigest()
        with self._lock:
            self._jobs = {key: job for key, job in self._jobs.items() if job["expires"] > now}
            if job_id not in self._jobs:
                if len(self._jobs) >= self.capacity:
                    raise ValueError("shadow queue capacity reached")
                self._jobs[job_id] = {
                    "payload": json.loads(canonical(payload)),
                    "expires": now + timedelta(seconds=120),
                    "lease": now,
                    "worker": None,
                    "attempt": None,
                    "result": None,
                }
        return job_id

    def claim(self, worker_id: str, *, now: datetime) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", worker_id):
            raise ValueError("invalid worker id")
        with self._lock:
            for job_id, job in self._jobs.items():
                if job["expires"] <= now or job["result"] is not None or job["lease"] > now:
                    continue
                job["worker"] = worker_id
                job["attempt"] = secrets.token_hex(16)
                job["lease"] = min(now + timedelta(seconds=30), job["expires"])
                return sign(
                    {
                        "version": VERSION,
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "attempt": job["attempt"],
                        "expires_at": job["lease"].isoformat(),
                        "payload": job["payload"],
                    },
                    self.key,
                )
        return None

    def accept(self, envelope: dict[str, Any], *, now: datetime) -> None:
        result = verify(envelope, self.key)
        if set(result) != {"version", "job_id", "worker_id", "attempt", "metrics"}:
            raise ValueError("unexpected result fields")
        with self._lock:
            job = self._jobs.get(result["job_id"])
            if job is None or result["version"] != VERSION:
                raise ValueError("unknown job or protocol")
            if job["lease"] <= now or job["expires"] <= now:
                raise ValueError("expired result")
            if result["worker_id"] != job["worker"] or result["attempt"] != job["attempt"]:
                raise ValueError("stale or foreign assignment")
            # Always advisory; HMAC proves sender, not numerical correctness.
            from .protocol import calculate

            if result["metrics"] != calculate(job["payload"]):
                raise ValueError("result differs from local reference")
            if job["result"] is not None and job["result"] != result["metrics"]:
                raise ValueError("conflicting duplicate result")
            job["result"] = json.loads(canonical(result["metrics"]))

    def result(self, job_id: str, *, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["expires"] <= now or job["result"] is None:
                return None
            return {"shadow_only": True, "version": VERSION, "metrics": dict(job["result"])}


def utc_now() -> datetime:
    return datetime.now(UTC)
