from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.orchestration.active_runtime import (
    ETORO_DEMO_RUNTIME_STATUS_KIND,
    EtoroDemoContinuousRunner,
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
