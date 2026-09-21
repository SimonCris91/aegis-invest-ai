"""Deterministic tests for the local read-only Home contract."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

import app.web.server as web_server
from app.brokers.etoro.demo import EtoroDemoAdapter
from app.storage.sqlite import SqliteRecordStore
from app.web.home import home_snapshot_from_scan_cycle, live_system_status_from_runtime
from app.web.server import AegisHomeHandler, _allowlisted_top_opportunity, _read_runtime_activity


def _report(*, top: int = 0, watchlist: tuple[dict[str, object], ...] = ()) -> dict[str, object]:
    return {
        "status": "READ_ONLY_ACTIVE_SCAN_CYCLE_READY",
        "execution_mode": "READ_ONLY",
        "capital_currency": "EUR",
        "capital_mode": "SIMULATED",
        "data_readiness": "READY",
        "scanner_cycle_status": "COMPLETE",
        "broker_write_calls": 0,
        "scan_cycle_timestamp": datetime(2026, 8, 30, tzinfo=UTC).isoformat(),
        "assets_requested": 34,
        "assets_comparable": 34,
        "top_opportunities": tuple({"symbol": "AAA"} for _ in range(top)),
        "watchlist": watchlist,
        "no_trade": tuple({"symbol": "AAA"} for _ in range(26)),
        "positions_to_manage": (),
        "scanner_output": {"simulated_capital": "200"},
    }


def test_home_snapshot_maps_successful_zero_opportunity_state() -> None:
    snapshot = home_snapshot_from_scan_cycle(
        _report(
            watchlist=(
                {
                    "symbol": "BTC",
                    "full_asset_name": "Bitcoin",
                    "opportunity_score": "68.95",
                    "rank": 1,
                },
            )
        )
    )

    assert snapshot.capital.amount == Decimal("200")
    assert snapshot.scanner.top_opportunities == 0
    assert snapshot.scanner.assets_scanned == 34
    assert snapshot.scanner.assets_comparable == 34
    assert snapshot.scanner.watchlist_count == 1
    assert snapshot.positions.open_count == 0
    assert snapshot.safety.execution_mode == "READ_ONLY"
    assert snapshot.safety.broker_write_calls == 0
    assert snapshot.data_health.one_hour_status == "READY"


def test_home_snapshot_uses_live_runtime_for_operational_values() -> None:
    snapshot = home_snapshot_from_scan_cycle(
        {
            **_report(top=6),
            "watchlist": tuple({"symbol": "OLD"} for _ in range(471)),
            "no_trade": tuple({"symbol": "OLD"} for _ in range(9)),
        },
        live_system_status={
            "scanner": {"top_opportunity_count": 0},
            "universe": {"active_scanner_universe_count": 633},
            "capital": {"authorized_capital_eur": "2000"},
            "demo": {"broker_write_calls": 0},
            "cycle": {"state": "NO_CYCLE"},
        },
    )

    assert snapshot.capital.amount == Decimal("2000")
    assert snapshot.scanner.assets_scanned == 34
    assert snapshot.scanner.assets_comparable == 34
    assert snapshot.scanner.universe_count == 633
    assert snapshot.scanner.top_opportunities == 0
    assert snapshot.scanner.watchlist_count is None
    assert snapshot.scanner.no_trade_count is None
    assert len(snapshot.last_scan_top_opportunities) == 6
    assert snapshot.safety.broker_write_calls == 0


def test_waiting_is_not_degraded_and_demo_is_not_dashboard_readonly():
    live = {"runner": {"state": "RUNNING"}, "heartbeat": {"stale": False},
            "demo": {"execution_enabled": True}, "cycle": {"state": "NO_CYCLE"},
            "universe": {"active_scanner_universe_count": 639},
            "acquisition": {"status": "COMPLETE", "coverage_ratio": "1",
                            "minimum_coverage": "1", "eligible_count": 38}}
    snapshot = home_snapshot_from_scan_cycle({**_report(), "data_readiness": "DEGRADED"}, live_system_status=live)
    assert snapshot.safety.execution_mode == "DEMO"
    assert snapshot.scanner.assets_scanned == 34
    assert snapshot.scanner.universe_count == 639
    assert snapshot.scanner.coherent_now == 38
    assert snapshot.data_health.one_hour_status == "READY"
    assert snapshot.data_health.scanner_cycle_status == "NO_CYCLE"
    live["heartbeat"]["stale"] = True
    assert home_snapshot_from_scan_cycle(_report(), live_system_status=live).data_health.one_hour_status == "DEGRADED"


def test_home_live_values_do_not_fall_back_to_last_scan_counts() -> None:
    snapshot = home_snapshot_from_scan_cycle(
        _report(top=6, watchlist=tuple({"symbol": "OLD"} for _ in range(471))),
        live_system_status={
            "scanner": {"top_opportunity_count": 0},
            "universe": {"active_scanner_universe_count": 633},
            "capital": {"authorized_capital_eur": "2000"},
        },
    )

    assert snapshot.scanner.top_opportunities == 0
    assert snapshot.scanner.watchlist_count is None
    assert snapshot.scanner.no_trade_count is None
    assert len(snapshot.last_scan_top_opportunities) == 6


def test_home_js_uses_runtime_age_and_dash_for_unavailable_values() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert '"—"' in script
    assert "liveRuntimeTimestamp" in script
    assert "renderTopOpportunities(topItems, topItems.length" in script


def test_home_js_labels_last_scan_opportunities_as_historical() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "HISTORICAL — LAST COMPLETED SCAN" in script
    assert "Not current live opportunities" in script
    assert "LAST SCAN TOP OPPORTUNITIES" not in script


def test_home_snapshot_preserves_empty_watchlist_and_positions() -> None:
    snapshot = home_snapshot_from_scan_cycle(_report())

    assert snapshot.watchlist == ()
    assert snapshot.positions.items == ()
    assert snapshot.positions.open_count == 0


def test_home_snapshot_does_not_turn_missing_scanner_fields_into_zero() -> None:
    report = _report()
    report.pop("assets_comparable")
    report.pop("data_readiness")
    report["status"] = "BLOCKED"

    snapshot = home_snapshot_from_scan_cycle(report)

    assert snapshot.scanner.assets_comparable is None
    assert snapshot.data_health.one_hour_status == "UNKNOWN"
    assert snapshot.data_health.backend_status == "DEGRADED"


def test_home_snapshot_keeps_unprovided_operational_state_unknown() -> None:
    report = _report()
    for key in (
        "execution_mode",
        "capital_currency",
        "capital_mode",
        "data_readiness",
        "broker_write_calls",
    ):
        report.pop(key)

    snapshot = home_snapshot_from_scan_cycle(report)

    assert snapshot.safety.execution_mode == "UNKNOWN"
    assert snapshot.safety.broker_write_calls is None
    assert snapshot.capital.currency is None
    assert snapshot.capital.mode == "UNKNOWN"
    assert snapshot.data_health.one_hour_status == "UNKNOWN"
    assert snapshot.data_health.freshness_state is None
    assert snapshot.data_health.market_session_state is None


def test_live_system_status_uses_only_persisted_runtime_fields() -> None:
    status = live_system_status_from_runtime(
        {
            "runner_state": "RUNNING",
            "cycle_state": "NO_CYCLE",
            "demo_broker_write_calls": 3,
            "broker_write_calls_real": 0,
            "authorized_capital_eur": "200",
            "managed_exposure_eur": "25",
            "sizing_mode": "RISK_MANAGER_AUTHORIZED_CAPITAL",
            "untrusted_derived_field": 99,
        }
    )

    assert status == {
        "runner": {"state": "RUNNING"},
        "cycle": {"state": "NO_CYCLE"},
        "demo": {"broker_write_calls": 3},
        "real": {"broker_write_calls": 0},
        "capital": {
            "authorized_capital_eur": "200",
            "managed_exposure_eur": "25",
            "sizing_mode": "RISK_MANAGER_AUTHORIZED_CAPITAL",
        },
    }


def test_live_system_status_exposes_runtime_diagnostics_and_exit_management(tmp_path) -> None:
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    store.append(
        "etoro-demo-runtime-status",
        {
            "runner_state": "RUNNING",
            "cycle_state": "BLOCKED",
            "activity_code": "WAITING_FOR_ELIGIBLE_COMPLETED_BAR",
            "blockers": ["FRESH_NEWS_REQUIRED"],
            "last_error": None,
            "observed_at": "2026-08-31T21:00:00+00:00",
        },
    )
    store.append(
        "etoro-demo-exit-management",
        {
            "observed_at": "2026-08-31T21:00:01+00:00",
            "blocked": 0,
            "evaluated": 1,
            "held": 1,
            "close_triggered": 0,
        },
    )

    activity = _read_runtime_activity(tmp_path / "runtime.sqlite3")

    assert activity is not None
    assert activity["blockers"] == ["FRESH_NEWS_REQUIRED"]
    assert activity["latest_exit_management"]["held"] == 1
    status = live_system_status_from_runtime(
        activity,
        overnight_activity=activity,
        now=datetime(2026, 8, 31, 21, 0, 2, tzinfo=UTC),
    )
    assert status is not None
    assert status["activity"]["blockers"] == ["FRESH_NEWS_REQUIRED"]
    assert status["exit_management"]["close_triggered"] == 0


def test_live_system_status_exposes_market_acquisition_progress() -> None:
    status = live_system_status_from_runtime(
        {
            "acquisition_status": "IN_PROGRESS",
            "acquisition_instruments_requested": 633,
            "acquisition_instruments_attempted": 64,
            "acquisition_instruments_not_attempted": 377,
            "acquisition_instruments_in_backoff": 192,
            "acquisition_outcome_counts": {"PROVIDER_UNAVAILABLE": 64},
            "coherent_coverage_ratio": "0",
            "coherent_coverage_minimum": "1.0",
        }
    )

    assert status is not None
    assert status["acquisition"] == {
        "status": "IN_PROGRESS",
        "requested": 633,
        "attempted": 64,
        "not_attempted": 377,
        "in_backoff": 192,
        "outcome_counts": {"PROVIDER_UNAVAILABLE": 64},
        "coverage_ratio": "0",
        "minimum_coverage": "1.0",
    }


def test_live_system_status_marks_dead_runner_stale_from_persisted_heartbeat() -> None:
    status = live_system_status_from_runtime(
        {
            "runner_state": "RUNNING",
            "observed_at": "2026-08-31T21:00:00+00:00",
        },
        now=datetime(2026, 8, 31, 21, 4, tzinfo=UTC),
    )

    assert status is not None
    assert status["runner"]["state"] == "STALE"
    assert status["heartbeat"] == {
        "data_timestamp": "2026-08-31T21:00:00+00:00",
        "data_age_seconds": 240.0,
        "last_activity_at": "2026-08-31T21:00:00+00:00",
        "age_seconds": 240.0,
        "stale_after_seconds": 180,
        "stale_threshold_seconds": 180,
        "stale": True,
        "is_stale": True,
    }


def test_live_system_status_keeps_recent_runner_running() -> None:
    status = live_system_status_from_runtime(
        {
            "runner_state": "RUNNING",
            "observed_at": "2026-08-31T21:00:00+00:00",
        },
        now=datetime(2026, 8, 31, 21, 2, tzinfo=UTC),
    )

    assert status is not None
    assert status["runner"]["state"] == "RUNNING"
    assert status["heartbeat"]["stale"] is False


def test_live_system_status_preserves_session_semantics_for_ui() -> None:
    status = live_system_status_from_runtime(
        {
            "runner_state": "RUNNING",
            "cycle_state": "NO_CYCLE",
            "market_session_state": "CLOSED",
            "freshness_state": "FRESH_FOR_SESSION",
        }
    )

    assert status is not None
    assert status["cycle"] == {
        "state": "NO_CYCLE",
        "market_session_state": "CLOSED",
        "freshness_state": "FRESH_FOR_SESSION",
    }


def test_home_live_status_ignores_historical_top_count_when_no_cycle() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "const liveScanner = status.scanner || {};" in script
    assert 'liveTopCount > 0 && cycle.state !== "NO_CYCLE"' in script
    assert "Number(snapshot.scanner?.top_opportunities || 0)" not in script


def test_home_exposes_all_persisted_top_details() -> None:
    snapshot = home_snapshot_from_scan_cycle(
        {
            **_report(),
            "top_opportunity_items": (
                {
                    "symbol": "AAPL",
                    "full_asset_name": "Apple",
                    "opportunity_score": "77.5",
                    "rank": 1,
                    "rejection_reasons": [],
                },
                {
                    "symbol": "BTC",
                    "opportunity_score": "71.2",
                    "rank": 2,
                    "rejection_reasons": ["provider context unavailable"],
                },
            ),
        }
    )

    assert [item["symbol"] for item in snapshot.top_opportunity_items] == ["AAPL", "BTC"]
    assert snapshot.top_opportunity_items[0]["full_asset_name"] == "Apple"
    assert snapshot.top_opportunity_items[1]["rejection_reasons"] == [
        "provider context unavailable"
    ]
    assert [item["symbol"] for item in snapshot.last_scan_top_opportunities] == [
        "AAPL",
        "BTC",
    ]


def test_home_preserves_observed_asset_details_for_expandable_ui() -> None:
    report = {
        **_report(
            watchlist=(
                {
                    "symbol": "BTC",
                    "full_asset_name": "Bitcoin",
                    "asset_class": "CRYPTO",
                    "opportunity_score": "68.95",
                    "confidence": "0.74",
                    "rank": 1,
                    "action_state": "HOLD",
                    "timeframe": "1H",
                    "data_quality": "COMPLETE",
                    "freshness_state": "FRESH",
                    "provider_provenance": ["ETORO", "ALPACA"],
                    "risk_flags": ["VOLATILITY"],
                    "news_sentiment": "NEUTRAL",
                    "material_event_count": 2,
                    "rejection_reasons": ["confidence below entry threshold"],
                },
            )
        ),
        "positions_to_manage": (
            {
                "symbol": "BTC",
                "full_name": "Bitcoin",
                "asset_class": "CRYPTO",
                "current_price": "80000",
                "opportunity_score": "68.95",
                "confidence": "0.74",
                "action_state": "HOLD",
                "timeframe": "1H",
                "scan_cycle_timestamp": "2026-08-31T21:00:00+00:00",
                "bar_timestamp": "2026-08-31T20:00:00+00:00",
                "data_quality": "COMPLETE",
                "provider_provenance": ["ETORO"],
                "freshness_state": "FRESH",
                "market_session_state": "OPEN",
                "existing_position_state": "HELD",
                "eligible_for_entry_comparison": False,
                "eligibility_reason_code": "POSITION_ALREADY_HELD",
                "current_market_state": "OPEN",
                "risk_flags": ["VOLATILITY"],
            },
        ),
    }

    snapshot = home_snapshot_from_scan_cycle(report)

    assert snapshot.watchlist[0].name == "Bitcoin"
    assert snapshot.watchlist[0].provider_provenance == ("ETORO", "ALPACA")
    assert snapshot.watchlist[0].material_event_count == 2
    assert snapshot.positions.items[0].name == "Bitcoin"
    assert snapshot.positions.items[0].bar_timestamp == datetime(2026, 8, 31, 20, tzinfo=UTC)
    assert snapshot.positions.items[0].eligibility_reason_code == "POSITION_ALREADY_HELD"


def test_missing_live_runtime_status_remains_absent() -> None:
    assert live_system_status_from_runtime(None) is None


def test_overnight_activity_reads_persisted_runtime_state_without_inference(tmp_path) -> None:
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    payload = {
        "observed_at": "2026-08-31T22:00:00+00:00",
        "runner_state": "RUNNING",
        "cycle_state": "NO_CYCLE",
        "last_successful_scan_at": "2026-08-31T21:00:00+00:00",
        "top_opportunity_count": 0,
        "demo_broker_write_calls": 0,
        "broker_write_calls_real": 0,
        "authorized_capital_eur": "200",
        "managed_exposure_eur": None,
        "remaining_authorized_capital_eur": None,
        "poll_count": 2,
    }
    store.append("etoro-demo-runtime-status", payload)

    activity = _read_runtime_activity(tmp_path / "runtime.sqlite3")

    assert activity is not None
    assert activity["runner_state"] == "RUNNING"
    assert activity["last_completed_cycle_time"] == "2026-08-31T21:00:00+00:00"
    assert activity["authorized_capital_eur"] == "200"
    assert activity["managed_exposure_eur"] is None
    assert activity["cycles_completed_since_startup"] is None
    assert activity["demo_submissions_attempted"] is None
    assert activity["latest_risk_decision"] is None
    assert len(activity["top_opportunities_per_cycle"]) == 1


def test_activity_history_is_bounded_and_uses_latest_completed_poll(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    for poll in range(1, 101):
        store.append("etoro-demo-runtime-status", {"poll_count": poll, "cycle_state": None})
        store.append("etoro-demo-runtime-status", {"poll_count": poll, "cycle_state": "BLOCKED", "top_opportunity_count": 0})
    result = _read_runtime_activity(path)
    assert [row["poll_count"] for row in result["top_opportunities_per_cycle"]] == list(range(89, 101))
    store.append("etoro-demo-runtime-status", {"poll_count": 1, "cycle_state": "NO_CYCLE"})
    result = _read_runtime_activity(path)
    assert [row["poll_count"] for row in result["top_opportunities_per_cycle"]] == [1]


def test_home_snapshot_uses_backend_safety_and_temporal_fields() -> None:
    report = _report()
    report.update(
        {
            "execution_mode": "PAPER",
            "broker_write_calls": 0,
            "data_readiness": "DEGRADED",
            "data_freshness": "STALE",
            "market_session_state": "OPEN",
            "scanner_cycle_status": "PARTIAL",
        }
    )

    snapshot = home_snapshot_from_scan_cycle(report)

    assert snapshot.safety.execution_mode == "PAPER"
    assert snapshot.safety.broker_write_calls == 0
    assert snapshot.data_health.one_hour_status == "DEGRADED"
    assert snapshot.data_health.freshness_state == "STALE"
    assert snapshot.data_health.market_session_state == "OPEN"
    assert snapshot.data_health.scanner_cycle_status == "PARTIAL"


def test_live_status_exposes_news_circuit_breaker_and_write_counters() -> None:
    from app.web.home import live_system_status_from_runtime

    status = live_system_status_from_runtime(
        {
            "runner_state": "RUNNING",
            "observed_at": "2026-09-06T12:00:00+00:00",
            "news_provider": "ALPHA_VANTAGE",
            "news_provider_status": "RATE_LIMITED/SUPPRESSED",
            "news_rate_limit_triggered": True,
            "news_provider_request_count": 1,
            "news_requests_suppressed_after_rate_limit": 177,
            "news_cache_hits": 2,
            "news_cache_misses": 3,
            "demo_writes": 0,
            "real_writes": 0,
        },
        now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    assert status is not None
    assert status["news"]["status"] == "RATE_LIMITED/SUPPRESSED"
    assert status["news"]["requests_suppressed_after_rate_limit"] == 177
    assert status["demo"]["writes"] == 0
    assert status["real"]["writes"] == 0


def test_live_status_exposes_data_age_and_stale_threshold() -> None:
    from app.web.home import live_system_status_from_runtime

    status = live_system_status_from_runtime(
        {"runner_state": "RUNNING", "observed_at": "2026-09-06T11:55:00+00:00"},
        now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    assert status is not None
    assert status["heartbeat"]["data_timestamp"] == "2026-09-06T11:55:00+00:00"
    assert status["heartbeat"]["data_age_seconds"] == 300.0
    assert status["heartbeat"]["stale_threshold_seconds"] == 180
    assert status["heartbeat"]["is_stale"] is True


def test_top_opportunity_serialization_drops_unknown_fields_and_secrets() -> None:
    projected = _allowlisted_top_opportunity(
        {
            "symbol": "ADA",
            "instrument_id": "100017",
            "score": "72.1",
            "confidence": "0.81",
            "action": "WATCH",
            "api_key": "secret",
            "raw_payload": {"Authorization": "secret"},
            "filesystem_path": "C:\\private",
            "new_future_column": "should-drop",
        },
        rank=1,
        observed_at="2026-09-06T12:00:00+00:00",
    )

    assert projected == {
        "symbol": "ADA",
        "instrument_id": "100017",
        "score": "72.1",
        "confidence": "0.81",
        "action": "WATCH",
        "rank": 1,
        "observed_at": "2026-09-06T12:00:00+00:00",
    }


def test_local_server_serves_home_and_rejects_mutations() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), AegisHomeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/")
        get_response = connection.getresponse()
        get_body = get_response.read()
        assert get_response.status == 200
        assert b"Aegis Invest AI" in get_body

        connection.request("POST", "/api/home")
        post_response = connection.getresponse()
        post_body = post_response.read()
        assert post_response.status == 405
        assert b"READ_ONLY_METHOD_NOT_ALLOWED" in post_body
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_home_api_exposes_persisted_last_scan_top_rows(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        web_server,
        "_persisted_home_report",
        lambda: {
            **_report(),
            "top_opportunity_items": (
                {"symbol": "AGCO", "opportunity_score": "77.52", "rank": 1},
                {"symbol": "TSLA", "opportunity_score": "76.75", "rank": 2},
            ),
            "last_scan_top_opportunities": (
                {"symbol": "AGCO", "opportunity_score": "77.52", "rank": 1},
                {"symbol": "TSLA", "opportunity_score": "76.75", "rank": 2},
            ),
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), AegisHomeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/home")
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        assert response.status == 200
        assert [item["symbol"] for item in payload["last_scan_top_opportunities"]] == [
            "AGCO",
            "TSLA",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_home_api_repeated_reads_never_invoke_demo_consumer(monkeypatch) -> None:
    demo_post_attempts = 0

    def forbidden_submit_demo(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        nonlocal demo_post_attempts
        demo_post_attempts += 1
        raise AssertionError("GET /api/home must never submit Demo orders")

    monkeypatch.setattr(EtoroDemoAdapter, "submit_demo", forbidden_submit_demo)
    monkeypatch.setattr(
        web_server,
        "_persisted_home_report",
        lambda: _report(top=1),
    )
    monkeypatch.setattr(
        web_server,
        "read_etoro_demo_runtime_status",
        lambda: {
            "runner_state": "RUNNING",
            "cycle_state": "NO_CYCLE",
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
        },
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), AegisHomeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(3):
            connection = HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/api/home")
            response = connection.getresponse()
            body = response.read()
            connection.close()
            payload = json.loads(body)
            assert response.status == 200
            assert payload["scanner"]["top_opportunities"] == 1
            assert payload["safety"]["broker_write_calls"] == 0
            assert payload["live_system_status"]["runner"]["state"] == "RUNNING"
            assert payload["live_system_status"]["cycle"]["state"] == "NO_CYCLE"
            assert payload["live_system_status"]["demo"]["broker_write_calls"] == 0
            assert payload["live_system_status"]["real"]["broker_write_calls"] == 0
        assert demo_post_attempts == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
