"""Secret-rejecting SQLite operational record store."""

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from app.brokers.models import TrackRecordKind

FORBIDDEN_KEYS = {
    "api_key",
    "user_key",
    "authorization",
    "token",
    "secret",
    "password",
    "x-api-key",
    "x-user-key",
}


class SecretPersistenceError(ValueError):
    pass


def assert_secret_free(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                raise SecretPersistenceError("secret-shaped fields cannot be persisted")
            assert_secret_free(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            assert_secret_free(nested)


class SqliteRecordStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS operational_records "
            "(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
            "created_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS demo_submissions "
            "(idempotency_key TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self._connection.commit()

    def append(self, kind: str, payload: Mapping[str, object]) -> int:
        assert_secret_free(payload)
        cursor = self._connection.execute(
            "INSERT INTO operational_records(kind, created_at, payload) VALUES (?, ?, ?)",
            (kind, datetime.now(UTC).isoformat(), json.dumps(payload, sort_keys=True)),
        )
        self._connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a record identifier")
        return cursor.lastrowid

    def append_track_record(self, kind: TrackRecordKind, payload: Mapping[str, object]) -> int:
        normalized = dict(payload)
        normalized["track_record_kind"] = kind.value
        if kind is TrackRecordKind.ETORO_DEMO:
            normalized["funds_label"] = "SIMULATED FUNDS"
        if kind is TrackRecordKind.REAL_ACCOUNT_OBSERVATION:
            normalized["aegis_generated_return"] = False
        return self.append(f"track-record:{kind.value}", normalized)

    def list(self, kind: str) -> tuple[dict[str, object], ...]:
        rows = self._connection.execute(
            "SELECT payload FROM operational_records WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def reserve_demo_submission(self, idempotency_key: str, payload: Mapping[str, object]) -> bool:
        assert_secret_free(payload)
        try:
            self._connection.execute(
                "INSERT INTO demo_submissions(idempotency_key, state, payload) VALUES (?, ?, ?)",
                (idempotency_key, "RESERVED", json.dumps(payload, sort_keys=True)),
            )
            self._connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_demo_submission(
        self, idempotency_key: str, state: str, payload: Mapping[str, object]
    ) -> None:
        assert_secret_free(payload)
        cursor = self._connection.execute(
            "UPDATE demo_submissions SET state=?, payload=? WHERE idempotency_key=?",
            (state, json.dumps(payload, sort_keys=True), idempotency_key),
        )
        if cursor.rowcount != 1:
            raise KeyError("Demo submission reservation does not exist")
        self._connection.commit()

    def demo_submission(self, idempotency_key: str) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT state, payload FROM demo_submissions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return {"state": str(row[0]), "payload": json.loads(row[1])}

    def demo_submission_keys(self) -> frozenset[str]:
        rows = self._connection.execute("SELECT idempotency_key FROM demo_submissions").fetchall()
        return frozenset(str(row[0]) for row in rows)

    def unresolved_demo_submissions(self) -> tuple[dict[str, object], ...]:
        rows = self._connection.execute(
            "SELECT idempotency_key, state, payload FROM demo_submissions "
            "WHERE state IN ('RESERVED', 'SUBMITTED', 'PENDING', 'UNKNOWN')"
        ).fetchall()
        return tuple(
            {
                "idempotency_key": str(row[0]),
                "state": str(row[1]),
                "payload": json.loads(row[2]),
            }
            for row in rows
        )
