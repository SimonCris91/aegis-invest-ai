"""Secret-rejecting SQLite operational record store."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from app.brokers.models import TrackRecordKind
from app.domain.enums import Currency

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


class ActiveClaimError(RuntimeError):
    pass


class RunnerLeaseError(RuntimeError):
    pass


def _optional_bool_int(value: object) -> int | None:
    if value is None:
        return None
    return int(bool(value))


@dataclass(frozen=True)
class ActiveOrchestrationClaim:
    claim_name: str
    owner_token: str
    generation: int
    acquired_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RunnerLease:
    owner_token: str
    generation: int
    acquired_at: datetime
    expires_at: datetime


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
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30.0)
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS operational_records "
            "(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
            "created_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        # Status/history reads select by kind and record order. Preserve all
        # audit rows while avoiding full table scans through unrelated events.
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_operational_records_kind_id "
            "ON operational_records(kind, id)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS demo_submissions "
            "(idempotency_key TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS active_cycle_watermarks "
            "(asset TEXT NOT NULL, timeframe TEXT NOT NULL, "
            "completed_bar_timestamp TEXT NOT NULL, PRIMARY KEY(asset, timeframe))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS active_accepted_cycles "
            "(cycle_id TEXT PRIMARY KEY, scan_cycle_timestamp TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS active_orchestration_claims "
            "(claim_name TEXT PRIMARY KEY, owner_token TEXT, generation INTEGER NOT NULL, "
            "acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL, "
            "released_at TEXT)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS runner_control "
            "(control_name TEXT PRIMARY KEY, stop_requested INTEGER NOT NULL, "
            "requested_at TEXT)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS runner_leases "
            "(lease_name TEXT PRIMARY KEY, owner_token TEXT, generation INTEGER NOT NULL, "
            "acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
            "status TEXT NOT NULL, released_at TEXT)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS market_acquisition_state ("
            "instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, symbol TEXT NOT NULL, "
            "last_attempt_at TEXT, last_success_at TEXT, latest_valid_completed_bar TEXT, "
            "classification TEXT NOT NULL, consecutive_failures INTEGER NOT NULL, "
            "retry_after TEXT, detail_code TEXT, "
            "PRIMARY KEY(instrument_id, timeframe))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS etoro_session_state ("
            "instrument_id TEXT PRIMARY KEY, exchange_id TEXT, "
            "is_exchange_open INTEGER, is_open INTEGER, "
            "is_currently_tradable INTEGER, is_buy_enabled INTEGER, "
            "session_state TEXT NOT NULL, observed_at TEXT NOT NULL, "
            "source TEXT NOT NULL, expires_at TEXT NOT NULL, error_class TEXT)"
        )
        self._connection.commit()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._connection.close()

    def etoro_session_states(self, *, as_of: datetime) -> dict[str, dict[str, object]]:
        """Return only non-expired broker session observations."""
        if as_of.tzinfo is None:
            raise ValueError("session state timestamp must be timezone-aware")
        rows = self._connection.execute(
            "SELECT instrument_id, exchange_id, is_exchange_open, is_open, "
            "is_currently_tradable, is_buy_enabled, session_state, observed_at, "
            "source, expires_at, error_class FROM etoro_session_state"
        ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            if datetime.fromisoformat(str(row[9])) < as_of:
                continue
            result[str(row[0])] = {
                "instrument_id": str(row[0]),
                "exchange_id": row[1],
                "is_exchange_open": None if row[2] is None else bool(row[2]),
                "is_open": None if row[3] is None else bool(row[3]),
                "is_currently_tradable": None if row[4] is None else bool(row[4]),
                "is_buy_enabled": None if row[5] is None else bool(row[5]),
                "session_state": str(row[6]),
                "observed_at": str(row[7]),
                "source": str(row[8]),
                "expires_at": str(row[9]),
                "error_class": row[10],
            }
        return result

    def upsert_etoro_session_state(self, *, state: Mapping[str, object]) -> None:
        """Persist one secret-free authoritative eToro session observation."""
        assert_secret_free(state)
        required = ("instrument_id", "session_state", "observed_at", "source", "expires_at")
        if any(key not in state for key in required):
            raise ValueError("incomplete eToro session state")
        self._connection.execute(
            "INSERT INTO etoro_session_state("
            "instrument_id, exchange_id, is_exchange_open, is_open, "
            "is_currently_tradable, is_buy_enabled, session_state, observed_at, "
            "source, expires_at, error_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(instrument_id) DO UPDATE SET exchange_id=excluded.exchange_id, "
            "is_exchange_open=excluded.is_exchange_open, is_open=excluded.is_open, "
            "is_currently_tradable=excluded.is_currently_tradable, "
            "is_buy_enabled=excluded.is_buy_enabled, session_state=excluded.session_state, "
            "observed_at=excluded.observed_at, source=excluded.source, "
            "expires_at=excluded.expires_at, error_class=excluded.error_class",
            (
                str(state["instrument_id"]),
                state.get("exchange_id"),
                _optional_bool_int(state.get("is_exchange_open")),
                _optional_bool_int(state.get("is_open")),
                _optional_bool_int(state.get("is_currently_tradable")),
                _optional_bool_int(state.get("is_buy_enabled")),
                str(state["session_state"]),
                str(state["observed_at"]),
                str(state["source"]),
                str(state["expires_at"]),
                state.get("error_class"),
            ),
        )
        self._connection.commit()

    def market_acquisition_states(self, timeframe: str) -> dict[str, dict[str, object]]:
        rows = self._connection.execute(
            "SELECT instrument_id, symbol, last_attempt_at, last_success_at, "
            "latest_valid_completed_bar, classification, consecutive_failures, retry_after, "
            "detail_code FROM market_acquisition_state WHERE timeframe=?",
            (timeframe,),
        ).fetchall()
        return {
            str(row[0]): {
                "instrument_id": str(row[0]),
                "symbol": str(row[1]),
                "last_attempt_at": row[2],
                "last_success_at": row[3],
                "latest_valid_completed_bar": row[4],
                "classification": str(row[5]),
                "consecutive_failures": int(row[6]),
                "retry_after": row[7],
                "detail_code": row[8],
            }
            for row in rows
        }

    def upsert_market_acquisition_state(
        self, *, timeframe: str, state: Mapping[str, object]
    ) -> None:
        assert_secret_free(state)
        self._connection.execute(
            "INSERT INTO market_acquisition_state("
            "instrument_id, timeframe, symbol, last_attempt_at, last_success_at, "
            "latest_valid_completed_bar, classification, consecutive_failures, retry_after, "
            "detail_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(instrument_id, timeframe) DO UPDATE SET "
            "symbol=excluded.symbol, last_attempt_at=excluded.last_attempt_at, "
            "last_success_at=excluded.last_success_at, "
            "latest_valid_completed_bar=excluded.latest_valid_completed_bar, "
            "classification=excluded.classification, "
            "consecutive_failures=excluded.consecutive_failures, "
            "retry_after=excluded.retry_after, detail_code=excluded.detail_code",
            (
                str(state["instrument_id"]),
                timeframe,
                str(state["symbol"]),
                state.get("last_attempt_at"),
                state.get("last_success_at"),
                state.get("latest_valid_completed_bar"),
                str(state["classification"]),
                int(str(state["consecutive_failures"])),
                state.get("retry_after"),
                state.get("detail_code"),
            ),
        )
        self._connection.commit()

    def request_runner_stop(
        self, *, requested_at: datetime, control_name: str = "demo-runner"
    ) -> str:
        """Persist an idempotent stop request for the named runner."""
        if requested_at.tzinfo is None:
            raise ValueError("runner stop timestamp must be timezone-aware")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "INSERT INTO runner_control(control_name, stop_requested, requested_at) "
                "VALUES (?, 1, ?) ON CONFLICT(control_name) DO UPDATE SET "
                "stop_requested=1, requested_at=excluded.requested_at",
                (control_name, requested_at.isoformat()),
            )
            row = self._runner_lease_row(control_name)
            if row is not None:
                current_expiry = datetime.fromisoformat(str(row[4]))
                if str(row[5]) == "ACTIVE" and current_expiry <= requested_at:
                    # Reclaim the expired owner while holding the write lock. The
                    # owner token remains fenced by the generation predicate used
                    # by heartbeat/release operations.
                    self._connection.execute(
                        "UPDATE runner_leases SET owner_token=NULL, status='EXPIRED', "
                        "released_at=? WHERE lease_name=? AND generation=? "
                        "AND status='ACTIVE' AND expires_at<=?",
                        (
                            requested_at.isoformat(),
                            control_name,
                            int(cast(str, row[2])),
                            requested_at.isoformat(),
                        ),
                    )
            self._connection.commit()
            lease = self._runner_lease_row(control_name)
            if lease is None:
                return "ABSENT"
            return "RUNNING" if lease[5] == "ACTIVE" else "STOPPED"
        except BaseException:
            self._connection.rollback()
            raise

    def runner_stop_requested(self, *, control_name: str = "demo-runner") -> bool:
        row = self._connection.execute(
            "SELECT stop_requested FROM runner_control WHERE control_name=?", (control_name,)
        ).fetchone()
        return row is not None and int(row[0]) == 1

    def runner_control_state(self, *, control_name: str = "demo-runner") -> dict[str, object]:
        row = self._connection.execute(
            "SELECT stop_requested, requested_at FROM runner_control WHERE control_name=?",
            (control_name,),
        ).fetchone()
        lease = self._runner_lease_row(control_name)
        return {
            "stop_requested": row is not None and int(cast(str, row[0])) == 1,
            "stop_requested_at": None if row is None else row[1],
            "lease_owner_present": lease is not None and lease[1] is not None,
            "lease_status": None if lease is None else str(lease[5]),
            "lease_generation": None if lease is None else int(cast(str, lease[2])),
            "lease_expires_at": None if lease is None else str(lease[4]),
        }

    def acquire_runner_lease(
        self,
        *,
        owner_token: str,
        acquired_at: datetime,
        expires_at: datetime,
        lease_name: str = "demo-runner",
    ) -> RunnerLease | None:
        if not owner_token:
            raise ValueError("runner owner token is required")
        if acquired_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("runner lease timestamps must be timezone-aware")
        if expires_at <= acquired_at:
            raise ValueError("runner lease must expire after acquisition")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._runner_lease_row(lease_name)
            if row is not None:
                current_expiry = datetime.fromisoformat(str(row[4]))
                if str(row[5]) == "ACTIVE" and current_expiry > acquired_at:
                    self._connection.rollback()
                    return None
                generation = int(cast(str, row[2])) + 1
                self._connection.execute(
                    "UPDATE runner_leases SET owner_token=?, generation=?, acquired_at=?, "
                    "heartbeat_at=?, expires_at=?, status='ACTIVE', released_at=NULL "
                    "WHERE lease_name=?",
                    (
                        owner_token,
                        generation,
                        acquired_at.isoformat(),
                        acquired_at.isoformat(),
                        expires_at.isoformat(),
                        lease_name,
                    ),
                )
            else:
                generation = 1
                self._connection.execute(
                    "INSERT INTO runner_leases(lease_name, owner_token, generation, acquired_at, "
                    "heartbeat_at, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')",
                    (
                        lease_name,
                        owner_token,
                        generation,
                        acquired_at.isoformat(),
                        acquired_at.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
            self._connection.execute(
                "INSERT INTO runner_control(control_name, stop_requested, requested_at) "
                "VALUES (?, 0, NULL) ON CONFLICT(control_name) DO UPDATE SET "
                "stop_requested=0, requested_at=NULL",
                (lease_name,),
            )
            self._connection.commit()
            return RunnerLease(owner_token, generation, acquired_at, expires_at)
        except BaseException:
            self._connection.rollback()
            raise

    def heartbeat_runner_lease(
        self,
        *,
        lease: RunnerLease,
        heartbeat_at: datetime,
        expires_at: datetime,
        lease_name: str = "demo-runner",
    ) -> bool:
        if heartbeat_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("runner heartbeat timestamps must be timezone-aware")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                "UPDATE runner_leases SET heartbeat_at=?, expires_at=? WHERE lease_name=? "
                "AND owner_token=? AND generation=? AND status='ACTIVE'",
                (
                    heartbeat_at.isoformat(),
                    expires_at.isoformat(),
                    lease_name,
                    lease.owner_token,
                    lease.generation,
                ),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                return False
            self._connection.commit()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    def reclaim_expired_runner_lease(
        self, *, reclaimed_at: datetime, lease_name: str = "demo-runner"
    ) -> str:
        """Fence and release an expired runner lease atomically."""
        if reclaimed_at.tzinfo is None:
            raise ValueError("runner reclaim timestamp must be timezone-aware")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._runner_lease_row(lease_name)
            if row is None:
                self._connection.rollback()
                return "ABSENT"
            expiry = datetime.fromisoformat(str(row[4]))
            if str(row[5]) != "ACTIVE":
                self._connection.rollback()
                return str(row[5])
            if expiry > reclaimed_at:
                self._connection.rollback()
                return "ACTIVE"
            self._connection.execute(
                "UPDATE runner_leases SET owner_token=NULL, status='EXPIRED', "
                "released_at=? WHERE lease_name=? AND generation=? "
                "AND status='ACTIVE' AND expires_at<=?",
                (
                    reclaimed_at.isoformat(),
                    lease_name,
                    int(cast(str, row[2])),
                    reclaimed_at.isoformat(),
                ),
            )
            self._connection.commit()
            return "EXPIRED"
        except BaseException:
            self._connection.rollback()
            raise

    def release_runner_lease(
        self,
        *,
        lease: RunnerLease,
        released_at: datetime,
        lease_name: str = "demo-runner",
    ) -> bool:
        if released_at.tzinfo is None:
            raise ValueError("runner release timestamp must be timezone-aware")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                "UPDATE runner_leases SET owner_token=NULL, status='RELEASED', released_at=? "
                "WHERE lease_name=? AND owner_token=? AND generation=? AND status='ACTIVE'",
                (released_at.isoformat(), lease_name, lease.owner_token, lease.generation),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                return False
            self._connection.commit()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    def _runner_lease_row(self, lease_name: str) -> tuple[object, ...] | None:
        return cast(
            tuple[object, ...] | None,
            self._connection.execute(
                "SELECT lease_name, owner_token, generation, acquired_at, expires_at, status, "
                "released_at FROM runner_leases WHERE lease_name=?",
                (lease_name,),
            ).fetchone(),
        )

    def append(self, kind: str, payload: Mapping[str, object]) -> int:
        assert_secret_free(payload)
        try:
            cursor = self._connection.execute(
                "INSERT INTO operational_records(kind, created_at, payload) VALUES (?, ?, ?)",
                (kind, datetime.now(UTC).isoformat(), json.dumps(payload, sort_keys=True)),
            )
            self._connection.commit()
        except sqlite3.OperationalError:
            self._connection.rollback()
            raise
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

    def latest(self, kind: str) -> dict[str, object] | None:
        """Read one record, without materializing an unbounded audit history."""
        row = self._connection.execute(
            "SELECT payload FROM operational_records WHERE kind=? ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    @staticmethod
    def read_latest_read_only(path: Path, kind: str) -> dict[str, object] | None:
        """Read an operational record without creating or mutating the database."""
        if not path.exists():
            return None
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA query_only=ON")
        try:
            row = connection.execute(
                "SELECT payload FROM operational_records WHERE kind=? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()
        return None if row is None else json.loads(row[0])

    @staticmethod
    def read_runner_lease_read_only(
        path: Path, lease_name: str = "demo-runner"
    ) -> dict[str, object] | None:
        """Read runner lease truth without creating or mutating the SQLite store."""
        if not path.exists():
            return None
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA query_only=ON")
        try:
            row = connection.execute(
                "SELECT owner_token, generation, acquired_at, heartbeat_at, expires_at, "
                "status, released_at FROM runner_leases WHERE lease_name=?",
                (lease_name,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "owner_present": row[0] is not None,
            "generation": int(row[1]),
            "acquired_at": str(row[2]),
            "heartbeat_at": str(row[3]),
            "expires_at": str(row[4]),
            "status": str(row[5]),
            "released_at": None if row[6] is None else str(row[6]),
        }

    @staticmethod
    def read_runner_control_read_only(
        path: Path, control_name: str = "demo-runner"
    ) -> dict[str, object]:
        """Read lifecycle control without creating or mutating the store."""
        if not path.exists():
            return {"stop_requested": False, "stop_requested_at": None}
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA query_only=ON")
        try:
            row = connection.execute(
                "SELECT stop_requested, requested_at FROM runner_control WHERE control_name=?",
                (control_name,),
            ).fetchone()
        except sqlite3.OperationalError:
            return {"stop_requested": False, "stop_requested_at": None}
        finally:
            connection.close()
        return {
            "stop_requested": row is not None and int(row[0]) == 1,
            "stop_requested_at": None if row is None else str(row[1]),
        }

    def active_cycle_watermarks(self) -> dict[tuple[str, str], datetime]:
        rows = self._connection.execute(
            "SELECT asset, timeframe, completed_bar_timestamp FROM active_cycle_watermarks"
        ).fetchall()
        return {(str(row[0]), str(row[1])): datetime.fromisoformat(str(row[2])) for row in rows}

    def active_claim(self, claim_name: str) -> ActiveOrchestrationClaim | None:
        row = self._connection.execute(
            "SELECT owner_token, generation, acquired_at, expires_at, status "
            "FROM active_orchestration_claims WHERE claim_name=?",
            (claim_name,),
        ).fetchone()
        if row is None or str(row[4]) != "ACTIVE" or row[0] is None:
            return None
        return ActiveOrchestrationClaim(
            claim_name=claim_name,
            owner_token=str(row[0]),
            generation=int(row[1]),
            acquired_at=datetime.fromisoformat(str(row[2])),
            expires_at=datetime.fromisoformat(str(row[3])),
        )

    def acquire_active_claim(
        self,
        *,
        claim_name: str,
        owner_token: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> ActiveOrchestrationClaim | None:
        if not claim_name or not owner_token:
            raise ValueError("claim_name and owner_token are required")
        if acquired_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("claim timestamps must be timezone-aware")
        if expires_at <= acquired_at:
            raise ValueError("claim must expire after acquisition")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT owner_token, generation, expires_at, status "
                "FROM active_orchestration_claims WHERE claim_name=?",
                (claim_name,),
            ).fetchone()
            if row is not None:
                current_expiry = datetime.fromisoformat(str(row[2]))
                if str(row[3]) == "ACTIVE" and current_expiry > acquired_at:
                    self._connection.rollback()
                    return None
                generation = int(row[1]) + 1
                self._connection.execute(
                    "UPDATE active_orchestration_claims SET owner_token=?, generation=?, "
                    "acquired_at=?, expires_at=?, status='ACTIVE', released_at=NULL "
                    "WHERE claim_name=?",
                    (
                        owner_token,
                        generation,
                        acquired_at.isoformat(),
                        expires_at.isoformat(),
                        claim_name,
                    ),
                )
            else:
                generation = 1
                self._connection.execute(
                    "INSERT INTO active_orchestration_claims "
                    "(claim_name, owner_token, generation, acquired_at, expires_at, status) "
                    "VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
                    (
                        claim_name,
                        owner_token,
                        generation,
                        acquired_at.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
            self._connection.commit()
            return ActiveOrchestrationClaim(
                claim_name=claim_name,
                owner_token=owner_token,
                generation=generation,
                acquired_at=acquired_at,
                expires_at=expires_at,
            )
        except BaseException:
            self._connection.rollback()
            raise

    def release_active_claim(
        self,
        *,
        claim_name: str,
        owner_token: str,
        generation: int,
        released_at: datetime,
    ) -> bool:
        """Release only the currently owned claim generation."""
        if released_at.tzinfo is None:
            raise ValueError("release timestamp must be timezone-aware")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                "UPDATE active_orchestration_claims SET owner_token=NULL, status='RELEASED', "
                "released_at=? WHERE claim_name=? AND owner_token=? AND generation=? "
                "AND status='ACTIVE'",
                (released_at.isoformat(), claim_name, owner_token, generation),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                return False
            self._commit_transaction()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    def finalize_active_cycle(
        self,
        *,
        claim: ActiveOrchestrationClaim,
        cycle_id: str,
        scan_cycle_timestamp: datetime,
        payload: Mapping[str, object],
        watermarks: Mapping[tuple[str, str], datetime],
        finalized_at: datetime,
    ) -> bool:
        """Finalize a fenced claim and all accepted-cycle state atomically."""
        assert_secret_free(payload)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            current = self._connection.execute(
                "SELECT owner_token, generation, expires_at, status "
                "FROM active_orchestration_claims WHERE claim_name=?",
                (claim.claim_name,),
            ).fetchone()
            if (
                current is None
                or str(current[0]) != claim.owner_token
                or int(current[1]) != claim.generation
                or str(current[3]) != "ACTIVE"
                or datetime.fromisoformat(str(current[2])) <= finalized_at
            ):
                self._connection.rollback()
                raise ActiveClaimError("claim is stale, expired, or fenced")
            duplicate = self._connection.execute(
                "SELECT 1 FROM active_accepted_cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            if duplicate is not None:
                self._connection.rollback()
                return False
            existing = {
                (str(asset), str(timeframe)): datetime.fromisoformat(str(timestamp))
                for asset, timeframe, timestamp in self._connection.execute(
                    "SELECT asset, timeframe, completed_bar_timestamp FROM active_cycle_watermarks"
                ).fetchall()
            }
            for key, timestamp in watermarks.items():
                previous = existing.get(key)
                if previous is not None and timestamp < previous:
                    raise ValueError("active cycle watermark cannot regress")
            self._connection.execute(
                "INSERT INTO active_accepted_cycles(cycle_id, scan_cycle_timestamp) VALUES (?, ?)",
                (cycle_id, scan_cycle_timestamp.isoformat()),
            )
            self._insert_operational_record("active-intelligence-cycle", payload)
            for (asset, timeframe), timestamp in watermarks.items():
                self._upsert_active_watermark(asset, timeframe, timestamp)
            self._connection.execute(
                "UPDATE active_orchestration_claims SET owner_token=NULL, status='RELEASED', "
                "released_at=? WHERE claim_name=? AND owner_token=? AND generation=?",
                (finalized_at.isoformat(), claim.claim_name, claim.owner_token, claim.generation),
            )
            self._commit_transaction()
            return True
        except BaseException:
            self._connection.rollback()
            raise

    def accepted_active_cycles(self) -> dict[str, datetime]:
        rows = self._connection.execute(
            "SELECT cycle_id, scan_cycle_timestamp FROM active_accepted_cycles"
        ).fetchall()
        return {str(row[0]): datetime.fromisoformat(str(row[1])) for row in rows}

    def _insert_operational_record(self, kind: str, payload: Mapping[str, object]) -> None:
        self._connection.execute(
            "INSERT INTO operational_records(kind, created_at, payload) VALUES (?, ?, ?)",
            (kind, datetime.now(UTC).isoformat(), json.dumps(payload, sort_keys=True)),
        )

    def _commit_transaction(self) -> None:
        self._connection.commit()

    def _upsert_active_watermark(self, asset: str, timeframe: str, timestamp: datetime) -> None:
        self._connection.execute(
            "INSERT INTO active_cycle_watermarks(asset, timeframe, completed_bar_timestamp) "
            "VALUES (?, ?, ?) ON CONFLICT(asset, timeframe) DO UPDATE SET "
            "completed_bar_timestamp=excluded.completed_bar_timestamp "
            "WHERE excluded.completed_bar_timestamp > "
            "active_cycle_watermarks.completed_bar_timestamp",
            (asset, timeframe, timestamp.isoformat()),
        )

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
        existing = self._connection.execute(
            "SELECT payload FROM demo_submissions WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing is None:
            raise KeyError("Demo submission reservation does not exist")
        merged_payload = json.loads(existing[0])
        merged_payload.update(payload)
        assert_secret_free(merged_payload)
        cursor = self._connection.execute(
            "UPDATE demo_submissions SET state=?, payload=? WHERE idempotency_key=?",
            (state, json.dumps(merged_payload, sort_keys=True), idempotency_key),
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

    def latest_demo_submission(self) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT idempotency_key, state, payload FROM demo_submissions "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "idempotency_key": str(row[0]),
            "state": str(row[1]),
            "payload": json.loads(row[2]),
        }

    def filled_demo_submissions(self) -> tuple[dict[str, object], ...]:
        """Return Aegis-owned Demo positions eligible for exit management."""
        rows = self._connection.execute(
            "SELECT idempotency_key, state, payload FROM demo_submissions "
            "WHERE state IN ('FILLED', 'PARTIALLY_FILLED') ORDER BY rowid"
        ).fetchall()
        return tuple(
            {
                "idempotency_key": str(row[0]),
                "state": str(row[1]),
                "payload": json.loads(row[2]),
            }
            for row in rows
        )

    def demo_write_attempt_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM demo_submissions WHERE state != 'RESERVED'"
        ).fetchone()
        return 0 if row is None else int(row[0])

    @staticmethod
    def _demo_payload_amount(
        payload: Mapping[str, object], currency: Currency, *, remaining: bool = False
    ) -> Decimal:
        if currency is Currency.USD:
            keys = (
                ("remaining_exposure_account_currency", "executed_exposure_account_currency", "amount_account_currency")
                if remaining
                else ("amount_account_currency", "executed_exposure_account_currency")
            )
        else:
            keys = (
                ("remaining_exposure_eur", "amount_account_currency", "amount_eur")
                if remaining
                else ("amount_account_currency", "amount_eur")
            )
        for key in keys:
            raw = payload.get(key)
            if raw is not None:
                amount = Decimal(str(raw))
                if amount < 0:
                    raise InvalidOperation
                return amount
        raise KeyError(keys[0])

    def managed_demo_open_exposure(self, currency: Currency) -> Decimal | None:
        """Return current net open exposure in the explicitly requested currency."""
        rows = self._connection.execute(
            "SELECT state, payload FROM demo_submissions "
            "WHERE state IN ('FILLED', 'PARTIALLY_FILLED')"
        ).fetchall()
        by_instrument: dict[str, Decimal] = {}
        for _state, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
                if payload.get("account_currency") != currency.value:
                    return None
                amount = self._demo_payload_amount(payload, currency)
                instrument = str(payload["instrument_id"])
                action = str(payload.get("action", "OPEN")).upper()
                if payload.get("exit_status") == "CLOSED":
                    continue
                if action in {"OPEN", "INCREASE"}:
                    amount = self._demo_payload_amount(payload, currency, remaining=True)
                    by_instrument[instrument] = by_instrument.get(instrument, Decimal("0")) + amount
                elif action in {"REDUCE", "CLOSE"}:
                    by_instrument[instrument] = by_instrument.get(instrument, Decimal("0")) - amount
                else:
                    return None
            except (KeyError, TypeError, ValueError, InvalidOperation):
                return None
        if any(value < 0 for value in by_instrument.values()):
            return None
        return sum(by_instrument.values(), Decimal("0"))

    def managed_demo_reserved_capital(self, currency: Currency) -> Decimal | None:
        """Return unresolved Aegis Demo reservations in the requested currency."""
        rows = self._connection.execute(
            "SELECT payload FROM demo_submissions "
            "WHERE state IN ('RESERVED', 'SUBMITTED', 'PENDING', 'UNKNOWN')"
        ).fetchall()
        total = Decimal("0")
        for (raw_payload,) in rows:
            try:
                payload = json.loads(raw_payload)
                if payload.get("account_currency") != currency.value:
                    return None
                amount = self._demo_payload_amount(payload, currency)
            except (KeyError, TypeError, ValueError, InvalidOperation):
                return None
            total += amount
        return total

    def managed_demo_exposure(self, currency: Currency) -> Decimal | None:
        """Return open exposure plus unresolved reservations in one currency."""
        open_exposure = self.managed_demo_open_exposure(currency)
        reserved = self.managed_demo_reserved_capital(currency)
        if open_exposure is None or reserved is None:
            return None
        return open_exposure + reserved

    def managed_demo_open_exposure_eur(self) -> Decimal | None:
        return self.managed_demo_open_exposure(Currency.EUR)

    def managed_demo_reserved_capital_eur(self) -> Decimal | None:
        return self.managed_demo_reserved_capital(Currency.EUR)

    def managed_demo_exposure_eur(self) -> Decimal | None:
        return self.managed_demo_exposure(Currency.EUR)
