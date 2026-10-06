import json
from pathlib import Path
from typing import Any, cast

from app.brokers.etoro.client import EtoroReadClient
from app.config.loader import load_config
from app.data.runtime import persist_etoro_instrument_catalog_snapshot
from app.orchestration import active_runtime
from app.storage.sqlite import SqliteRecordStore


def test_calibration_read_only_does_not_require_production_coverage_threshold(
    monkeypatch: Any,
) -> None:
    config = load_config({"AEGIS_OPERATING_MODE": "ETORO_DEMO", "ETORO_API_ENABLED": "true"})
    monkeypatch.setattr(active_runtime, "runtime_credentials", lambda values: object())
    monkeypatch.setattr(
        "app.data.runtime.load_etoro_dynamic_active_scanner_instruments",
        lambda: (),
    )

    result = active_runtime.build_etoro_calibration_read_only_report(config, values={})

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "NO_ACTIVE_INSTRUMENTS"
    assert "COVERAGE_RATIO" not in str(result["blocker"])
    assert result["broker_write_calls"] == 0


def test_partial_nine_of_nine_is_not_valid_calibration() -> None:
    valid, reasons = active_runtime._calibration_validity(
        acquisition_status="IN_PROGRESS",
        unknown_count=170,
        active_session_denominator=9,
        acquisition_outcome_counts={
            "NOT_ATTEMPTED": 161,
            "RATE_LIMITED": 4,
            "UPDATED": 9,
        },
    )

    assert valid is False
    assert reasons == (
        "ACQUISITION_STATUS_IN_PROGRESS",
        "UNKNOWN_SESSION_STATE_PRESENT",
        "ACQUISITION_NOT_ATTEMPTED:161",
        "ACQUISITION_RATE_LIMITED:4",
    )


def _collector_report(
    *, valid: bool, denominator: int, status: str = "COMPLETE"
) -> dict[str, object]:
    return {
        "as_of": "2026-09-03T10:00:00+00:00",
        "catalog_total": 633,
        "calibratable_total": 632,
        "active_session_denominator": denominator,
        "causally_eligible": denominator if valid else 0,
        "measured_coverage_ratio": "1" if valid else "0",
        "session_state_reconciliation": {
            "TOTAL": 633,
            "OPEN_TRADABLE": denominator,
            "CLOSED": 633 - denominator - 1,
            "UNKNOWN": 0 if valid else 1,
            "UNSUPPORTED_INTERNAL": 1,
        },
        "scanner_input_count": denominator,
        "nonzero_score_count": denominator if valid else 0,
        "nonzero_confidence_count": denominator if valid else 0,
        "top_count": 0,
        "calibration_valid": valid,
        "calibration_invalid_reasons": () if valid else ("UNKNOWN_SESSION_STATE_PRESENT",),
        "acquisition": {"acquisition_status": status},
    }


def test_calibration_collector_persists_classification_and_stays_read_only(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    reports = iter(
        (
            _collector_report(valid=False, denominator=1, status="IN_PROGRESS"),
            _collector_report(valid=True, denominator=8),
        )
    )

    result = active_runtime.collect_etoro_calibration_evidence(
        load_config({"AEGIS_OPERATING_MODE": "ETORO_DEMO", "ETORO_API_ENABLED": "true"}),
        values={},
        max_attempts=2,
        interval_seconds=1,
        run_report=lambda: next(reports),
        sleeper=lambda _: None,
    )

    assert result["attempts"] == 2
    assert result["useful_evidence_runs"] == 1
    assert result["production_threshold_changed"] is False
    assert result["demo_writes"] == 0
    store = active_runtime.SqliteRecordStore(
        tmp_path / "work" / "etoro-coverage-calibration.sqlite3"
    )
    assert len(store.list("etoro-calibration-collector-observation")) == 2


def test_calibration_collector_stops_cleanly_on_ctrl_c(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    result = active_runtime.collect_etoro_calibration_evidence(
        load_config({"AEGIS_OPERATING_MODE": "ETORO_DEMO", "ETORO_API_ENABLED": "true"}),
        values={},
        interval_seconds=1,
        run_report=lambda: _collector_report(valid=False, denominator=0, status="IN_PROGRESS"),
        sleeper=interrupt,
    )

    assert result["stop_reason"] == "STOP_REQUESTED"
    assert result["demo_writes"] == 0


def test_full_catalog_session_audit_is_bounded_and_excludes_internal_records(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {"instrumentID": 1, "symbolFull": "ONE", "instrumentTypeID": 5},
                {"instrumentID": 2, "symbolFull": "TWO", "instrumentTypeID": 5},
                {
                    "instrumentID": 610,
                    "symbolFull": "ETORIAN610",
                    "instrumentTypeID": 5,
                    "isInternalInstrument": True,
                },
            ]
        },
        retrieved_at=active_runtime.datetime(2026, 9, 3, tzinfo=active_runtime.UTC),
    )

    def fake_enrich(
        *, store: SqliteRecordStore, instruments: tuple[Any, ...], as_of: Any, **_: Any
    ) -> tuple[Any, ...]:
        for instrument in instruments:
            store.upsert_etoro_session_state(
                state={
                    "instrument_id": instrument.broker_instrument_id,
                    "session_state": "OPEN_TRADABLE",
                    "observed_at": as_of.isoformat(),
                    "source": "test",
                    "expires_at": as_of.replace(hour=23).isoformat(),
                    "is_currently_tradable": True,
                    "is_buy_enabled": True,
                    "is_exchange_open": True,
                    "is_open": True,
                }
            )
        return instruments

    monkeypatch.setattr(active_runtime, "runtime_credentials", lambda _: object())
    monkeypatch.setattr(
        "app.orchestration.session_state.enrich_instrument_session_state", fake_enrich
    )
    result = active_runtime.build_etoro_full_catalog_session_audit_report(
        load_config({"AEGIS_OPERATING_MODE": "ETORO_DEMO", "ETORO_API_ENABLED": "true"}),
        values={},
        client=cast(EtoroReadClient, object()),
        batch_size=1,
        clock=lambda: active_runtime.datetime(2026, 9, 3, 12, tzinfo=active_runtime.UTC),
    )

    assert result["full_catalog_total"] == 3
    assert result["selected_this_run"] == 1
    assert result["queried_this_run"] == 1
    assert result["persisted_this_run"] == 1
    assert result["audited_cumulative"] == 1
    assert result["static_internal"] == 1
    assert result["not_yet_audited"] == 1
    assert result["open_tradable"] == 1
    assert result["active_universe_reference_count"] == 0
    assert result["current_active_open_tradable"] == 0
    assert result["new_open_tradable_outside_active_universe"] == 1
    assert result["broker_write_calls"] == 0
    assert (tmp_path / "work" / "etoro-full-catalog-session-audit.json").exists()

    second = active_runtime.build_etoro_full_catalog_session_audit_report(
        load_config({"AEGIS_OPERATING_MODE": "ETORO_DEMO", "ETORO_API_ENABLED": "true"}),
        values={},
        client=cast(EtoroReadClient, object()),
        batch_size=1,
        clock=lambda: active_runtime.datetime(2026, 9, 3, 12, 1, tzinfo=active_runtime.UTC),
    )
    assert second["queried_this_run"] == 1
    assert second["audited_cumulative"] == 2
    assert second["not_yet_audited"] == 0


def test_full_catalog_candidate_calibration_uses_only_new_open_ids(
    tmp_path: Path, monkeypatch: Any
) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "records": {
                    "10": {"outcome": "OPEN_TRADABLE"},
                    "11": {"outcome": "OPEN_TRADABLE"},
                    "12": {"outcome": "CLOSED"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.data.runtime.read_etoro_dynamic_universe_artifact",
        lambda: {"active_records": [{"etoro_instrument_id": "10"}]},
    )
    captured: dict[str, object] = {}

    def fake_calibration(*args: Any, **kwargs: Any) -> dict[str, object]:
        captured["candidate_ids"] = kwargs["candidate_ids"]
        captured["acquisition_batch_size"] = kwargs["acquisition_batch_size"]
        return {"status": "CALIBRATION_READ_ONLY_COMPLETE"}

    monkeypatch.setattr(
        active_runtime,
        "build_etoro_calibration_read_only_report",
        fake_calibration,
    )
    result = active_runtime.build_etoro_full_catalog_candidate_calibration_read_only_report(
        load_config({"AEGIS_OPERATING_MODE": "ETORO_DEMO", "ETORO_API_ENABLED": "true"}),
        values={},
        audit_path=audit_path,
    )

    assert captured["candidate_ids"] == ["11"]
    assert captured["acquisition_batch_size"] == 64
    assert result["candidate_count"] == 1
