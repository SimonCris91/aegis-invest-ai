import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.brokers.models import BrokerIdentity, ExecutionState
from app.orchestration.active_runtime import (
    ETORO_DEMO_RUNTIME_STATUS_KIND,
    EtoroDemoContinuousRunner,
    _catalog_refresh_due,
    _reconcile_unresolved_demo_submissions,
    etoro_demo_runtime_status,
    read_etoro_demo_runtime_status,
    request_etoro_demo_runtime_stop,
)
from app.storage.sqlite import SqliteRecordStore


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=60)
        return value


def test_same_cycle_is_polled_without_duplicate_submission() -> None:
    calls = 0
    sleeps: list[float] = []

    def run_once() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {
            "status": "NO_CYCLE" if calls == 2 else "NO_TOP_OPPORTUNITY",
            "cycle_id": None if calls == 2 else "cycle-1",
            "cycle_as_of_resolved": "2026-08-31T16:00:00+00:00" if calls == 1 else None,
            "top_opportunity_count": 0,
            "eligible_count": 0,
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
        }

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        clock=FixedClock(),
        sleeper=sleeps.append,
        poll_interval_seconds=60,
    ).run(max_iterations=2)

    assert calls == 2
    assert report["runner_state"] == "STOPPED"
    assert report["poll_count"] == 2
    assert report["accepted_cycle_count"] == 1
    assert report["last_accepted_cycle_timestamp"] == "2026-08-31T16:00:00+00:00"
    assert report["demo_broker_write_calls"] == 0
    assert report["broker_write_calls_real"] == 0
    assert sleeps == [60]


def test_legacy_fill_reconciliation_saves_authoritative_position_id(tmp_path: Path) -> None:
    registry = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    try:
        key = "legacy-filled-order"
        assert registry.reserve_demo_submission(
            key,
            {
                "instrument_id": 1081,
                "symbol": "TRX",
                "broker_order_id": "order-123",
                "amount": "23.04",
            },
        )
        registry.update_demo_submission(key, "FILLED", {})

        class OrderLookupClient:
            def demo_order_lookup_details(self, order_id: str) -> dict[str, object]:
                assert order_id == "order-123"
                return {
                    "state": ExecutionState.FILLED,
                    "position_id": "778899",
                    "executed_exposure_account_currency": "23.04",
                }

        report = _reconcile_unresolved_demo_submissions(
            registry=registry,
            client=OrderLookupClient(),  # type: ignore[arg-type]
            identity=BrokerIdentity(stable_user_id="test-user", demo_account_id=2, real_account_id=1),
            observed_at=datetime(2026, 9, 24, 12, tzinfo=UTC),
        )

        record = registry.demo_submission(key)
        assert record is not None
        payload = record["payload"]
        assert isinstance(payload, dict)
        assert payload["broker_position_id"] == "778899"
        assert payload["executed_exposure_account_currency"] == "23.04"
        assert payload["broker_reconciliation_source"] == "ETORO_V2_ORDER_LOOKUP"
        assert payload["reconciliation"] == "verified"
        assert report["legacy_attempted"] == 1
        assert report["legacy_verified"] == 1
        assert report["legacy_remaining"] == 0
    finally:
        registry.close()


def test_no_cycle_never_produces_demo_post() -> None:
    writes = 0

    def run_once() -> Mapping[str, object]:
        return {
            "status": "NO_CYCLE",
            "cycle_id": None,
            "top_opportunity_count": 0,
            "eligible_count": 0,
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
        }

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        sleeper=lambda _: None,
    ).run(max_iterations=3)

    assert report["poll_count"] == 3
    assert report["accepted_cycle_count"] == 0
    assert report["demo_broker_write_calls"] == writes == 0
    assert report["top_opportunity_count"] == 0


def test_catalog_refresh_due_respects_snapshot_age() -> None:
    now = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)

    assert _catalog_refresh_due(None, as_of=now, interval_seconds=21600)
    assert not _catalog_refresh_due(
        {"retrieved_at": (now - timedelta(hours=5)).isoformat()},
        as_of=now,
        interval_seconds=21600,
    )
    assert _catalog_refresh_due(
        {"retrieved_at": (now - timedelta(hours=6)).isoformat()},
        as_of=now,
        interval_seconds=21600,
    )
    assert _catalog_refresh_due(
        {"retrieved_at": "not-a-timestamp"},
        as_of=now,
        interval_seconds=21600,
    )


def test_runner_invokes_universe_maintenance_on_configured_poll_cadence() -> None:
    runtime_calls = 0
    maintenance_calls = 0

    def run_once() -> Mapping[str, object]:
        nonlocal runtime_calls
        runtime_calls += 1
        return {
            "status": "NO_CYCLE",
            "cycle_id": None,
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
        }

    def maintain_once() -> Mapping[str, object]:
        nonlocal maintenance_calls
        maintenance_calls += 1
        return {
            "status": "ETORO_UNIVERSE_BOOTSTRAP_INCOMPLETE",
            "processed_this_run": 16,
            "broker_write_calls": 0,
        }

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        maintenance_once=maintain_once,
        maintenance_every_polls=2,
        sleeper=lambda _: None,
    ).run(max_iterations=3)

    assert runtime_calls == 3
    assert maintenance_calls == 1
    assert report["last_universe_maintenance"]["processed_this_run"] == 16
    assert report["last_universe_maintenance_error"] is None
    assert report["broker_write_calls_real"] == 0


def test_universe_maintenance_failure_does_not_stop_primary_runner() -> None:
    runtime_calls = 0

    def run_once() -> Mapping[str, object]:
        nonlocal runtime_calls
        runtime_calls += 1
        return {"status": "NO_CYCLE", "cycle_id": None}

    def maintain_once() -> Mapping[str, object]:
        raise RuntimeError("synthetic universe maintenance failure")

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        maintenance_once=maintain_once,
        sleeper=lambda _: None,
    ).run(max_iterations=2)

    assert runtime_calls == 2
    assert report["last_universe_maintenance_error"] == "RuntimeError"


def test_runtime_exception_uses_bounded_backoff_then_recovers() -> None:
    calls = 0
    sleeps: list[float] = []

    def run_once() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic transport failure")
        return {
            "status": "NO_CYCLE",
            "cycle_id": None,
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
        }

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        sleeper=sleeps.append,
        error_backoff_seconds=17,
        max_backoff_seconds=20,
    ).run(max_iterations=2)

    assert calls == 2
    assert sleeps == [17]
    assert report["last_error"] == "RuntimeError"
    assert report["broker_write_calls_real"] == 0


def test_transient_status_lock_does_not_stop_runner(tmp_path: Path) -> None:
    class LockedOnceStore(SqliteRecordStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.lock_once = True

        def append(self, kind: str, payload: Mapping[str, object]) -> int:
            if self.lock_once:
                self.lock_once = False
                raise sqlite3.OperationalError("database is locked")
            return super().append(kind, payload)

    store = LockedOnceStore(tmp_path / "runtime.sqlite3")
    calls = 0

    def run_once() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "NO_CYCLE", "cycle_id": None}

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        sleeper=lambda _: None,
        status_store=store,
    ).run(max_iterations=2)

    assert calls == 2
    assert report["runner_state"] == "STOPPED"
    assert store.list(ETORO_DEMO_RUNTIME_STATUS_KIND)


def test_keyboard_interrupt_stops_cleanly() -> None:
    def run_once() -> Mapping[str, object]:
        raise KeyboardInterrupt

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        sleeper=lambda _: pytest.fail("must not sleep after shutdown"),
    ).run()

    assert report["runner_state"] == "STOPPED"
    assert report["poll_count"] == 1
    assert report["demo_broker_write_calls"] == 0
    assert report["broker_write_calls_real"] == 0


def test_runner_rejects_non_positive_intervals() -> None:
    with pytest.raises(ValueError):
        EtoroDemoContinuousRunner(run_once=lambda: {}, poll_interval_seconds=0)


def test_runner_status_is_persisted_and_readable_without_runtime_work(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    runner = EtoroDemoContinuousRunner(
        run_once=lambda: {
            "status": "NO_CYCLE",
            "cycle_id": None,
            "pilot_enabled": True,
            "execution_enabled": True,
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
        },
        clock=FixedClock(),
        sleeper=lambda _: None,
        status_store=store,
    )

    runner.run(max_iterations=1)
    persisted = read_etoro_demo_runtime_status(tmp_path / "runtime.sqlite3")

    assert persisted is not None
    assert persisted["runner_state"] == "STOPPED"
    assert persisted["cycle_state"] == "NO_CYCLE"
    assert persisted["activity_code"] == "WAITING_FOR_ELIGIBLE_COMPLETED_BAR"
    assert store.list(ETORO_DEMO_RUNTIME_STATUS_KIND)


def test_persisted_stop_request_exits_at_safe_iteration_boundary(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    calls = 0

    def run_once() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        assert (
            store.request_runner_stop(requested_at=datetime(2026, 8, 31, 16, 1, tzinfo=UTC))
            == "RUNNING"
        )
        return {"status": "NO_CYCLE", "cycle_id": None}

    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        clock=FixedClock(),
        sleeper=lambda _: pytest.fail("stop request must avoid another poll"),
        status_store=store,
    ).run()

    assert calls == 1
    assert report["runner_state"] == "STOPPED"
    assert store.runner_control_state()["stop_requested"] is True


def test_stop_request_command_is_idempotent_and_status_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    first = request_etoro_demo_runtime_stop(
        path=path, requested_at=datetime(2026, 8, 31, 16, tzinfo=UTC)
    )
    second = request_etoro_demo_runtime_stop(
        path=path, requested_at=datetime(2026, 8, 31, 16, 1, tzinfo=UTC)
    )

    assert first["status"] == second["status"] == "STOP_REQUESTED"
    assert second["runner_presence"] == "ABSENT"
    assert etoro_demo_runtime_status(path=path)["status"] == "ABSENT"


def test_runner_lease_blocks_second_instance_and_stale_lease_is_recovered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    first_store = SqliteRecordStore(path)
    second_store = SqliteRecordStore(path)
    acquired = first_store.acquire_runner_lease(
        owner_token="owner-a",
        acquired_at=datetime(2026, 8, 31, 16, tzinfo=UTC),
        expires_at=datetime(2026, 8, 31, 16, 5, tzinfo=UTC),
    )
    assert acquired is not None

    blocked = EtoroDemoContinuousRunner(
        run_once=lambda: pytest.fail("held lease must block runtime"),
        clock=lambda: datetime(2026, 8, 31, 16, 1, tzinfo=UTC),
        status_store=second_store,
    ).run(max_iterations=1)
    assert blocked["runner_state"] == "ALREADY_RUNNING"

    recovered = second_store.acquire_runner_lease(
        owner_token="owner-b",
        acquired_at=datetime(2026, 8, 31, 16, 6, tzinfo=UTC),
        expires_at=datetime(2026, 8, 31, 16, 11, tzinfo=UTC),
    )
    assert recovered is not None
    assert recovered.generation == acquired.generation + 1


def test_runner_restart_releases_lease_and_preserves_status_store(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    calls = 0

    def run_once() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {
            "status": "NO_CYCLE",
            "cycle_id": None,
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
        }

    first = EtoroDemoContinuousRunner(
        run_once=run_once, clock=FixedClock(), sleeper=lambda _: None, status_store=store
    ).run(max_iterations=1)
    second = EtoroDemoContinuousRunner(
        run_once=run_once, clock=FixedClock(), sleeper=lambda _: None, status_store=store
    ).run(max_iterations=1)

    assert calls == 2
    assert first["runner_state"] == second["runner_state"] == "STOPPED"
    assert store.runner_control_state()["lease_status"] == "RELEASED"
    assert first["broker_write_calls_real"] == second["broker_write_calls_real"] == 0


def test_no_cycle_heartbeat_preserves_last_scan_and_write_totals(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    results = iter(
        (
            {
                "status": "BLOCKED",
                "cycle_id": "accepted-cycle-0001",
                "demo_broker_write_calls": 2,
                "broker_write_calls_real": 0,
                "news_events_received": 75,
                "news_events_fresh": 75,
                "news_events_material": 9,
                "news_event_digest": ({"headline": "Latest verified event"},),
                "package_material_diagnostics": {"TEST": "DATA_NOT_READY"},
                "top_opportunity_count": 3,
                "eligible_count": 2,
            },
            {
                "status": "NO_CYCLE",
                "pilot_enabled": True,
                "news_events_received": 0,
                "news_events_fresh": 0,
                "news_events_material": 0,
                "news_event_digest": (),
                "news_provider_diagnostics": {},
                "global_risk_context": {},
                "top_opportunity_count": 0,
                "eligible_count": 0,
                "demo_exit": {"observed_at": "heartbeat", "held": 2},
            },
        )
    )

    EtoroDemoContinuousRunner(
        run_once=lambda: next(results),
        clock=FixedClock(),
        sleeper=lambda _: None,
        status_store=store,
    ).run(max_iterations=2)

    persisted = read_etoro_demo_runtime_status(path)
    assert persisted is not None
    assert persisted["cycle_state"] == "NO_CYCLE"
    assert persisted["demo_broker_write_calls"] == 2
    assert persisted["demo_broker_write_calls_last_poll"] == 0
    assert persisted["last_submission_status"] == "BLOCKED"
    assert persisted["news_events_received"] == 75
    assert persisted["news_events_material"] == 9
    assert persisted["news_event_digest"] == [{"headline": "Latest verified event"}]
    assert persisted["package_material_diagnostics"] == {"TEST": "DATA_NOT_READY"}
    assert persisted["top_opportunity_count"] == 3
    assert persisted["eligible_demo_candidates"] == 2

    # The concise status endpoint must report real persisted Demo calls,
    # rather than hardcoding zero as if it were an account-read operation.
    status = etoro_demo_runtime_status(path=path)
    assert status["broker_write_calls"] == 2
    assert status["demo_broker_write_calls_last_poll"] == 0

    EtoroDemoContinuousRunner(
        run_once=lambda: {"status": "NO_CYCLE", "pilot_enabled": True},
        clock=FixedClock(),
        sleeper=lambda _: None,
        status_store=SqliteRecordStore(path),
    ).run(max_iterations=1)
    after_restart = read_etoro_demo_runtime_status(path)
    assert after_restart is not None
    assert after_restart["demo_broker_write_calls"] == 2
    assert after_restart["news_event_digest"] == [{"headline": "Latest verified event"}]


def test_new_cycle_does_not_retain_previous_coverage_block_reason(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    results = iter(
        (
            {
                "status": "BLOCKED",
                "cycle_id": "coverage-blocked-cycle",
                "a4c_reason": "INSUFFICIENT_COHERENT_MARKET_COVERAGE",
                "blockers": ("INSUFFICIENT_COHERENT_MARKET_COVERAGE",),
                "demo_broker_write_calls": 0,
                "broker_write_calls_real": 0,
            },
            {
                "status": "BLOCKED",
                "cycle_id": "risk-blocked-cycle",
                "blockers": ("RISK_MANAGER_REJECTED", "DEMO_PREFLIGHT_REJECTED"),
                "scanner_reached": True,
                "risk_manager_reached": True,
                "demo_broker_write_calls": 0,
                "broker_write_calls_real": 0,
            },
        )
    )

    EtoroDemoContinuousRunner(
        run_once=lambda: next(results),
        clock=FixedClock(),
        sleeper=lambda _: None,
        status_store=store,
    ).run(max_iterations=2)

    persisted = read_etoro_demo_runtime_status(path)
    assert persisted is not None
    assert persisted["scanner_reached"] is True
    assert persisted["risk_manager_reached"] is True
    assert persisted["blockers"] == ["RISK_MANAGER_REJECTED", "DEMO_PREFLIGHT_REJECTED"]
    assert persisted["a4c_reason"] == ["RISK_MANAGER_REJECTED", "DEMO_PREFLIGHT_REJECTED"]


def test_no_cycle_heartbeat_clears_legacy_coverage_reason_after_scanner_reached(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    store.append(
        ETORO_DEMO_RUNTIME_STATUS_KIND,
        {
            "runner_state": "RUNNING",
            "cycle_state": "BLOCKED",
            "a4c_reason": "INSUFFICIENT_COHERENT_MARKET_COVERAGE",
            "blockers": ["RISK_MANAGER_REJECTED", "DEMO_PREFLIGHT_REJECTED"],
            "scanner_reached": True,
            "risk_manager_reached": True,
            "observed_at": "2026-09-29T15:00:00+00:00",
        },
    )

    EtoroDemoContinuousRunner(
        run_once=lambda: {"status": "NO_CYCLE", "cycle_id": None},
        clock=FixedClock(),
        sleeper=lambda _: None,
        status_store=store,
    ).run(max_iterations=1)

    persisted = read_etoro_demo_runtime_status(path)
    assert persisted is not None
    assert persisted["scanner_reached"] is True
    assert persisted["blockers"] == ["RISK_MANAGER_REJECTED", "DEMO_PREFLIGHT_REJECTED"]
    assert persisted["a4c_reason"] == ["RISK_MANAGER_REJECTED", "DEMO_PREFLIGHT_REJECTED"]


def test_status_marks_persisted_running_with_expired_lease_as_expired(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    acquired_at = datetime(2026, 8, 31, 16, tzinfo=UTC)
    lease = store.acquire_runner_lease(
        owner_token="owner-before-reboot",
        acquired_at=acquired_at,
        expires_at=acquired_at + timedelta(minutes=5),
    )
    assert lease is not None
    store.append(
        ETORO_DEMO_RUNTIME_STATUS_KIND,
        {
            "runner_state": "RUNNING",
            "observed_at": acquired_at.isoformat(),
            "poll_count": 7,
            "broker_write_calls_real": 0,
        },
    )

    status = etoro_demo_runtime_status(path=path, now=acquired_at + timedelta(minutes=6))

    assert status["status"] == "OK"
    assert status["persisted_runner_state"] == "RUNNING"
    assert status["runner_state"] == "LEASE_EXPIRED"
    assert status["runner_lease"]["generation"] == lease.generation
    assert status["runner_lease"]["status"] == "EXPIRED"
    assert status["runner_lease"]["owner_present"] is False


def test_stop_on_expired_lease_reclaims_lease_and_persists_stopped_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    acquired_at = datetime(2026, 8, 31, 16, tzinfo=UTC)
    lease = store.acquire_runner_lease(
        owner_token="legacy-owner",
        acquired_at=acquired_at,
        expires_at=acquired_at + timedelta(minutes=5),
    )
    assert lease is not None
    store.append(
        ETORO_DEMO_RUNTIME_STATUS_KIND,
        {"runner_state": "RUNNING", "observed_at": acquired_at.isoformat(), "poll_count": 7},
    )

    result = request_etoro_demo_runtime_stop(
        path=path, requested_at=acquired_at + timedelta(minutes=6)
    )

    assert result["runner_presence"] == "STOPPED"
    assert result["previous_runner_state"] == "STOPPED"
    status = etoro_demo_runtime_status(path=path, now=acquired_at + timedelta(minutes=6))
    assert status["runner_state"] == "STOPPED"
    assert status["persisted_runner_state"] == "STOPPED"
    assert status["runner_lease"]["status"] == "EXPIRED"
    assert status["runner_lease"]["owner_present"] is False


def test_expired_owner_is_fenced_before_it_can_poll_again(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    acquired_at = datetime(2026, 8, 31, 16, tzinfo=UTC)
    lease = store.acquire_runner_lease(
        owner_token="legacy-owner",
        acquired_at=acquired_at,
        expires_at=acquired_at + timedelta(minutes=5),
    )
    assert lease is not None
    calls = 0

    def run_once() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "NO_CYCLE"}

    # A stop/reclaim from another process fences the old generation.
    assert (
        store.reclaim_expired_runner_lease(reclaimed_at=acquired_at + timedelta(minutes=6))
        == "EXPIRED"
    )
    report = EtoroDemoContinuousRunner(
        run_once=run_once,
        clock=lambda: acquired_at + timedelta(minutes=7),
        sleeper=lambda _: None,
        status_store=store,
    ).run(max_iterations=1)

    assert calls == 1
    assert report["runner_state"] == "STOPPED"
    assert report["broker_write_calls_real"] == 0


def test_reclaimed_lease_gets_new_generation_and_old_owner_cannot_heartbeat(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    start = datetime(2026, 8, 31, 16, tzinfo=UTC)
    old = store.acquire_runner_lease(
        owner_token="old-owner", acquired_at=start, expires_at=start + timedelta(minutes=5)
    )
    assert old is not None
    assert (
        store.reclaim_expired_runner_lease(reclaimed_at=start + timedelta(minutes=6)) == "EXPIRED"
    )
    new = store.acquire_runner_lease(
        owner_token="new-owner",
        acquired_at=start + timedelta(minutes=7),
        expires_at=start + timedelta(minutes=12),
    )
    assert new is not None
    assert new.generation == old.generation + 1
    assert not store.heartbeat_runner_lease(
        lease=old,
        heartbeat_at=start + timedelta(minutes=8),
        expires_at=start + timedelta(minutes=13),
    )


def test_status_preserves_running_only_while_lease_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    acquired_at = datetime(2026, 8, 31, 16, tzinfo=UTC)
    assert store.acquire_runner_lease(
        owner_token="healthy-owner",
        acquired_at=acquired_at,
        expires_at=acquired_at + timedelta(minutes=5),
    )
    store.append(
        ETORO_DEMO_RUNTIME_STATUS_KIND,
        {"runner_state": "RUNNING", "observed_at": acquired_at.isoformat()},
    )

    status = etoro_demo_runtime_status(path=path, now=acquired_at + timedelta(minutes=1))

    assert status["runner_state"] == "RUNNING"
    assert status["persisted_runner_state"] == "RUNNING"


def test_status_distinguishes_stop_requested_from_live_runner(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    start = datetime(2026, 8, 31, 16, tzinfo=UTC)
    assert store.acquire_runner_lease(
        owner_token="live-owner",
        acquired_at=start,
        expires_at=start + timedelta(minutes=5),
    )
    store.append(
        ETORO_DEMO_RUNTIME_STATUS_KIND,
        {"runner_state": "RUNNING", "observed_at": start.isoformat()},
    )
    assert store.request_runner_stop(requested_at=start + timedelta(minutes=1)) == "RUNNING"

    status = etoro_demo_runtime_status(path=path, now=start + timedelta(minutes=2))

    assert status["runner_state"] == "STOP_REQUESTED"
    assert status["runner_lease"]["status"] == "ACTIVE"
    assert status["runner_control"]["stop_requested"] is True
