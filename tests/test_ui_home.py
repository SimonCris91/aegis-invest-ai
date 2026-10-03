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
from app.web.server import (
    AegisHomeHandler,
    _allowlisted_top_opportunity,
    _live_etoro_position_row,
    _read_aegis_demo_orders,
    _read_managed_demo_exposure_read_only,
    _read_runtime_activity,
    _read_unverified_demo_ledger_summary,
)


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
            "capital_currency": "USD",
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
    assert snapshot.capital.currency == "EUR"
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
    assert "news.events_fresh" in script
    assert "titoli dettagliati" in script
    assert "broker_write_calls: snapshot.live_system_status?.demo?.broker_write_calls" in script
    assert "brokerWriteCalls ?? 0" not in script
    assert "loss_count: complete ? losses.length : null" in script
    assert "gain_count: complete ? gains.length : null" in script
    assert "cash_reserve_fraction" in script
    assert "Candidati TOP per asset class" in script
    assert "Scanner valutati per asset class" in script
    assert "riserva minima del 10%" not in script
    assert "Dettaglio rifiuto broker" in script
    assert "Storico P/L" in script
    assert "realized_history" in script
    assert "Ordini effettuati" in script
    assert "order_history" in script


def test_live_etoro_position_projection_includes_broker_values_and_description() -> None:
    row = _live_etoro_position_row(
        {
            "positionID": "12345",
            "instrumentID": 1001,
            "symbol": "AAPL",
            "units": "2.5",
            "openRate": "180.10",
            "isBuy": True,
        },
        {"1001": {"symbol": "AAPL", "description": "Apple Inc."}},
        pnl_item={
            "unrealizedPnL": {"pnL": "4.75"},
            "amount": "450.00",
            "currentRate": "182.00",
            "currentValue": "455.00",
        },
    )

    assert row["symbol"] == "AAPL"
    assert row["description"] == "Apple Inc."
    assert row["units"] == "2.5"
    assert row["average_entry_price"] == "180.10"
    assert row["current_price"] == "182.00"
    assert row["current_value"] == "455.00"
    assert row["unrealized_pnl"] == "4.75"
    assert row["invested_amount"] == "450.00"
    assert row["direction"] == "LONG"


def test_dashboard_separates_broker_positions_from_aegis_monitoring() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "Posizioni aperte eToro Demo" in script
    assert "Asset in monitoraggio Aegis" in script
    assert "current_value" in script
    assert "unrealized_pnl" in script
    assert "LIVE_READ_UNAVAILABLE" in script
    assert "non sono considerate riconciliate" in script


def test_manual_order_selection_keeps_the_view_stable() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "let manualEditing = false" in script
    assert "manualEditing = true" in script
    assert "if (manualBusy || manualEditing) return;" in script
    assert 'if (selectedMode === "MANUAL" && manualEditing)' in script
    assert 'if (currentSnapshot && selectedMode !== "MANUAL" && !manualEditing)' in script
    assert "selectedRow.insertAdjacentElement('afterend', panel)" in script
    assert "document.createElement('section')" in script
    assert "Recreate the ticket when" in script
    assert "data-order-instrument-id" in script
    assert 'selectedMode === "MANUAL" && currentSnapshot && !initial' in script
    assert 'selectedMode !== "MANUAL" && !manualBusy' in script
    assert "panel.scrollIntoView" not in script


def test_dashboard_sections_expose_source_update_timestamps() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")
    styles = (web_server.WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "function sectionTimestampFor(snapshot, section)" in script
    assert "function setSectionUpdatedAt(section, timestamp" in script
    assert "decorateSectionTimestamps(snapshot)" in script
    assert "news.scan_completed_at" in script
    assert "live.heartbeat?.last_activity_at" in script
    assert "payload.observed_at || new Date().toISOString()" in script
    assert ".section-updated-at" in styles


def test_manual_sell_ticket_is_responsive_and_explains_missing_quote() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")
    styles = (web_server.WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "Bid live non disponibile nella lista" in script
    assert "Unità residue stimate" in script
    assert "units <= positionUnits" in script
    assert "Verifica prezzo e riepiloga vendita Demo" in script
    assert "non precompila tutta la posizione" in script
    assert "if (!event.target.checked) input.value = '';" in script
    assert "amount: side === 'SELL' && !saleAmount ? '0.01' : saleAmount" in script
    assert "if (side === 'SELL') updateSellDraft();" in script
    assert '.manual-order-ticket > * { min-width: 0;' in styles
    assert (
        '.manual-order-ticket input:not([type="checkbox"]), '
        '.manual-order-ticket select { display: block; width: 100%;' in styles
    )
    assert '.portfolio-table-body > .manual-order-ticket { width: auto;' in styles
    assert '.manual-order-ticket .partial-close { display: flex;' in styles


def test_home_js_labels_last_scan_opportunities_as_historical() -> None:
    script = (web_server.WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "HISTORICAL — LAST COMPLETED SCAN" in script
    assert "non opportunità live e non ordini approvati." in script
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
            "demo_broker_write_calls_last_poll": 0,
            "broker_write_calls_real": 0,
            "authorized_capital_eur": "200",
            "managed_exposure_limit_eur": "200",
            "managed_exposure_eur": "25",
            "sizing_mode": "RISK_MANAGER_AUTHORIZED_CAPITAL",
            "untrusted_derived_field": 99,
        }
    )

    assert status == {
        "runner": {"state": "RUNNING"},
        "cycle": {"state": "NO_CYCLE"},
        "demo": {"broker_write_calls": 3, "last_poll_broker_write_calls": 0},
        "real": {"broker_write_calls": 0, "execution_available": False},
        "capital": {
            "authorized_capital_eur": "200",
            "managed_exposure_limit_eur": "200",
            "managed_exposure_eur": "25",
            "sizing_mode": "RISK_MANAGER_AUTHORIZED_CAPITAL",
        },
    }


def test_home_keeps_news_counts_separate_from_rendered_event_digest() -> None:
    snapshot = home_snapshot_from_scan_cycle(
        _report(),
        live_system_status={
            "news": {
                "provider": "GDELT",
                "status": "PARTIAL",
                "events_received": 75,
                "events_fresh": 75,
                "events_material": 9,
                "event_digest": (),
            }
        },
    )

    assert snapshot.news["events_received"] == 75
    assert snapshot.news["events_fresh"] == 75
    assert snapshot.news["events_material"] == 9
    assert snapshot.news.get("events", ()) == ()


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


def test_live_system_status_exposes_demo_package_block_reason() -> None:
    status = live_system_status_from_runtime(
        {
            "package_material_diagnostics": {
                "APE": "DEMO_BUYING_POWER_UNAVAILABLE",
                "BAT": "DEMO_BUYING_POWER_UNAVAILABLE",
            },
            "candidate_execution_diagnostics": ({
                "symbol": "AAPL",
                "asset_class": "EQUITY",
                "package_status": "QUOTE_NOT_FRESH_FOR_EXECUTION",
                "quote": {"status": "STALE", "age_seconds": 420},
                "news": {"freshness": "NEWS_FRESH", "material_event_count": 1},
            },),
            "risk_manager_reached": False,
            "execution_admission_gate_reached": False,
        }
    )

    assert status is not None
    assert status["activity"]["package_material_diagnostics"] == {
        "APE": "DEMO_BUYING_POWER_UNAVAILABLE",
        "BAT": "DEMO_BUYING_POWER_UNAVAILABLE",
    }
    assert status["activity"]["candidate_execution_diagnostics"][0]["symbol"] == "AAPL"
    assert status["activity"]["candidate_execution_diagnostics"][0]["quote"]["age_seconds"] == 420
    assert status["demo"]["risk_manager_reached"] is False
    assert status["demo"]["execution_admission_gate_reached"] is False


def test_live_system_status_projects_multi_asset_scanner_counts() -> None:
    counts = {
        "evaluated": {"CRYPTO": 20, "EQUITY": 12, "ETF": 5},
        "buy_signals": {"CRYPTO": 3, "EQUITY": 1},
        "top": {"CRYPTO": 3},
        "watchlist": {"CRYPTO": 8, "EQUITY": 2},
        "no_trade": {"CRYPTO": 9, "EQUITY": 9, "ETF": 5},
        "rejected": {},
    }

    status = live_system_status_from_runtime(
        {"scanner_asset_class_counts": counts}
    )

    assert status is not None
    assert status["scanner"]["asset_class_counts"] == counts


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


def test_live_system_status_uses_active_lease_heartbeat_during_long_cycle() -> None:
    status = live_system_status_from_runtime(
        {
            "runner_state": "RUNNING",
            "observed_at": "2026-08-31T21:00:00+00:00",
            "runner_lease": {
                "status": "ACTIVE",
                "owner_present": True,
                "heartbeat_at": "2026-08-31T21:03:45+00:00",
            },
        },
        now=datetime(2026, 8, 31, 21, 4, tzinfo=UTC),
    )

    assert status is not None
    assert status["runner"]["state"] == "RUNNING"
    assert status["heartbeat"]["last_activity_at"] == "2026-08-31T21:03:45+00:00"
    assert status["heartbeat"]["age_seconds"] == 15.0
    assert status["heartbeat"]["is_stale"] is False


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
    assert snapshot.watchlist[0].reasons == ("confidence below entry threshold",)
    assert snapshot.positions.items[0].name == "Bitcoin"
    assert snapshot.positions.items[0].bar_timestamp == datetime(2026, 8, 31, 20, tzinfo=UTC)
    assert snapshot.positions.items[0].eligibility_reason_code == "POSITION_ALREADY_HELD"


def test_home_maps_saved_hold_reasons_to_watchlist_explanation() -> None:
    snapshot = home_snapshot_from_scan_cycle(
        _report(
            watchlist=(
                {
                    "symbol": "BTC",
                    "opportunity_score": "72.5",
                    "confidence": "0.54",
                    "reasons": ["trend confirmation missing"],
                    "rejection_reasons": [],
                },
            )
        )
    )

    assert snapshot.watchlist[0].reasons == ("trend confirmation missing",)


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
    assert activity["managed_exposure_eur"] == "0"
    assert activity["cycles_completed_since_startup"] is None
    assert activity["demo_submissions_attempted"] is None
    assert activity["latest_risk_decision"] is None
    assert len(activity["top_opportunities_per_cycle"]) == 1


def test_legacy_demo_ledger_is_shown_as_unverified_not_eur_exposure(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    assert store.reserve_demo_submission(
        "legacy-order:BTC",
        {"instrument_id": 100000, "symbol": "BTC", "amount_eur": "200", "action": "OPEN"},
    )
    store.update_demo_submission(
        "legacy-order:BTC", "FILLED", {"broker_order_id": "private-id"}
    )

    assert _read_managed_demo_exposure_read_only(path) is None
    assert _read_unverified_demo_ledger_summary(path) == {
        "count": 1,
        "nominal_total": "200",
        "currency": "UNKNOWN",
    }


def test_legacy_demo_order_projection_does_not_label_unknown_currency_as_eur(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = SqliteRecordStore(path)
    assert store.reserve_demo_submission(
        "legacy-order:ETH",
        {"instrument_id": 100001, "symbol": "ETH", "amount_eur": "75", "action": "OPEN"},
    )
    store.update_demo_submission(
        "legacy-order:ETH", "FILLED", {"broker_order_id": "opaque"}
    )

    order = _read_aegis_demo_orders(path)[0]
    assert order["amount"] == "75"
    assert order["currency"] == "UNKNOWN"
    assert order["status"] == "FILLED"
    assert order["created_at"] is None
    assert order["timestamp_source"] == "NOT_RECORDED_LEGACY"
    assert "Stato nel registro Aegis" in str(order["message"])


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


def test_home_catalog_metrics_follow_current_snapshot_and_bootstrap(tmp_path, monkeypatch) -> None:
    catalog_path = tmp_path / "catalog.json"
    active_path = tmp_path / "active.json"
    catalog_path.write_text(
        json.dumps({
            "snapshot_id": "fresh-snapshot",
            "retrieved_at": "2026-09-24T09:29:22+00:00",
            "unique_instrument_id_count": 16153,
            "raw_response": {"instrumentDisplayDatas": []},
            "instrument_display_datas": [],
        }),
        encoding="utf-8",
    )
    active_path.write_text(
        json.dumps({
            "source_snapshot_id": "fresh-snapshot",
            "created_at": "2026-09-24T09:30:23+00:00",
            "catalog_instrument_count": 16153,
            "verified_mapping_count": 11416,
            "market_data_ready_count": 11416,
            "catalog_not_ready_count": 4737,
            "catalog_pending_count": 0,
            "active_scanner_universe_count": 11416,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_server, "DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH", catalog_path)
    monkeypatch.setattr(web_server, "DEFAULT_ETORO_ACTIVE_UNIVERSE_PATH", active_path)

    runtime = web_server._overlay_local_etoro_universe_metrics({
        "catalog_instrument_count": 16152,
        "catalog_market_data_ready_count": 11261,
        "active_scanner_universe_count": 11261,
    })
    live = live_system_status_from_runtime(runtime)
    snapshot = home_snapshot_from_scan_cycle(_report(), live_system_status=live)

    assert snapshot.scanner.catalog_count == 16153
    assert snapshot.scanner.catalog_ready_count == 11416
    assert snapshot.scanner.universe_count == 11416
    assert snapshot.scanner.catalog_as_of == datetime(2026, 9, 24, 9, 29, 22, tzinfo=UTC)
    assert snapshot.scanner.bootstrap_as_of == datetime(2026, 9, 24, 9, 30, 23, tzinfo=UTC)


def test_home_does_not_show_old_readiness_when_snapshot_ids_disagree(tmp_path, monkeypatch) -> None:
    catalog_path = tmp_path / "catalog.json"
    active_path = tmp_path / "active.json"
    catalog_path.write_text(
        json.dumps({
            "snapshot_id": "new-snapshot",
            "retrieved_at": "2026-09-24T09:29:22+00:00",
            "unique_instrument_id_count": 16153,
            "raw_response": {"instrumentDisplayDatas": []},
            "instrument_display_datas": [],
        }),
        encoding="utf-8",
    )
    active_path.write_text(
        json.dumps({"source_snapshot_id": "old-snapshot", "created_at": "2026-09-20T00:00:00+00:00"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_server, "DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH", catalog_path)
    monkeypatch.setattr(web_server, "DEFAULT_ETORO_ACTIVE_UNIVERSE_PATH", active_path)

    runtime = web_server._overlay_local_etoro_universe_metrics({
        "catalog_instrument_count": 16152,
        "catalog_market_data_ready_count": 11261,
        "active_scanner_universe_count": 11261,
    })
    live = live_system_status_from_runtime(runtime)
    snapshot = home_snapshot_from_scan_cycle(_report(), live_system_status=live)

    assert snapshot.scanner.catalog_count == 16153
    assert snapshot.scanner.catalog_ready_count is None
    assert snapshot.scanner.universe_count is None
    assert snapshot.scanner.bootstrap_as_of is None


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


def test_live_status_never_claims_real_execution_is_available() -> None:
    status = live_system_status_from_runtime(
        {
            "runner_state": "RUNNING",
            "execution_available": True,
            "real_execution_available": True,
            "broker_write_calls_real": 0,
        },
        now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    assert status is not None
    assert status["real"]["execution_available"] is False
    assert status["real"]["broker_write_calls"] == 0


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


def test_proxied_dashboard_api_requires_bearer_and_never_authorizes_remote_writes(monkeypatch) -> None:
    token = "t" * 48
    monkeypatch.setattr(web_server, "load_runtime_values", lambda: {"AEGIS_DASHBOARD_ACCESS_TOKEN": token})
    monkeypatch.setattr(web_server, "_persisted_home_report", lambda: _report(top=1))
    server = ThreadingHTTPServer(("127.0.0.1", 0), AegisHomeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/home", headers={"CF-Connecting-IP": "203.0.113.7"})
        denied = connection.getresponse()
        denied.read()
        assert denied.status == 401

        connection.request(
            "GET",
            "/api/home",
            headers={"CF-Connecting-IP": "203.0.113.7", "Authorization": f"Bearer {token}"},
        )
        allowed = connection.getresponse()
        allowed.read()
        assert allowed.status == 200

        connection.request(
            "POST",
            "/api/home",
            headers={"CF-Connecting-IP": "203.0.113.7", "Authorization": f"Bearer {token}"},
        )
        rejected_write = connection.getresponse()
        rejected_write.read()
        assert rejected_write.status == 405
        connection.close()
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


def test_full_etoro_catalog_search_is_read_only_and_reports_bootstrap_status(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    catalog_rows = [
        {
            "instrumentID": 501,
            "symbolFull": "BTC",
            "instrumentDisplayName": "Bitcoin",
            "instrumentTypeID": 10,
        },
        {
            "instrumentID": 502,
            "symbolFull": "BETA",
            "instrumentDisplayName": "Beta Holdings",
            "instrumentTypeID": 5,
        },
    ]
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "snapshot_id": "fixture-snapshot",
                "retrieved_at": "2026-09-20T12:00:00+00:00",
                "unique_instrument_id_count": 2,
                "raw_response": {"instrumentDisplayDatas": catalog_rows},
                "instrument_display_datas": catalog_rows,
            }
        ),
        encoding="utf-8",
    )
    active_path = tmp_path / "universe.json"
    active_path.write_text(
        json.dumps(
            {
                "source_snapshot_id": "fixture-snapshot",
                "created_at": "2026-09-21T12:00:00+00:00",
                "records": [
                    {
                        "instrument_id": "501",
                        "status": "BOOTSTRAPPED",
                        "reason": None,
                    },
                    {
                        "instrument_id": "502",
                        "status": "NO_DATA",
                        "reason": "NO_VERIFIED_HISTORICAL_BARS",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_server, "DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH", catalog_path)
    monkeypatch.setattr(web_server, "DEFAULT_ETORO_ACTIVE_UNIVERSE_PATH", active_path)

    server = ThreadingHTTPServer(("127.0.0.1", 0), AegisHomeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/etoro/catalog/search?q=bitcoin")
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()

        assert response.status == 200
        assert payload["catalog_count"] == 2
        assert payload["matched_count"] == 1
        assert payload["results"][0] == {
            "instrument_id": "501",
            "symbol": "BTC",
            "name": "Bitcoin",
            "asset_class": "CRYPTO",
            "bootstrap_status": "BOOTSTRAPPED",
            "bootstrap_reason": None,
        }
        assert payload["readiness_as_of"] == "2026-09-21T12:00:00+00:00"

        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/etoro/catalog/search?q=b")
        short_response = connection.getresponse()
        short_payload = json.loads(short_response.read())
        connection.close()
        assert short_response.status == 400
        assert short_payload["status"] == "INVALID_QUERY"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
