"""Safe, read-only CLI for Aegis Invest AI."""

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from app.brokers.etoro.demo_execution import (
    run_operational_demo_once,
    run_user_confirmed_demo_validation,
    verify_demo_validation_read_only,
)
from app.brokers.etoro.demo_preflight import (
    build_first_demo_preflight_report,
    default_demo_preflight_store,
)
from app.brokers.etoro.readiness import (
    DEFAULT_READINESS_STORE_PATH,
    build_etoro_readiness_report,
    default_etoro_readiness_store,
)
from app.brokers.etoro.runtime import etoro_status, etoro_transport_check
from app.config import load_config, load_runtime_values
from app.data.runtime import (
    build_active_market_scanner_foundation_report,
    build_active_scanner_1h_foundation_report,
    build_active_scanner_1h_full_universe_sweep_report,
    build_active_scanner_1h_iex_equity_pilot_report,
    build_active_scanner_1h_pilot_report,
    build_active_scanner_observation_temporal_alignment_report,
    build_alpaca_core_4h_backfill_report,
    build_alpaca_full_backfill_report,
    build_alpaca_provider_pilot_report,
    build_etoro_broker_universe_discovery_report,
    build_etoro_crypto_validation_report,
    build_etoro_dynamic_active_universe,
    build_etoro_instrument_catalog_probe_report,
    build_etoro_instrument_schema_report,
    build_etoro_universe_bootstrap_report,
    build_exit_evidence_acquisition_report,
    build_exit_policy_v2_exposed_holdout_diagnostic_report,
    build_exit_policy_v2_train_validation_report,
    build_lifecycle_walkforward_readiness_report,
    build_lifecycle_zero_trade_forensics_report,
    build_live_intelligence_report,
    build_polygon_provider_pilot_report,
    build_prospective_shadow_validation_readiness_report,
    build_readonly_active_scan_cycle_report,
    build_real_strategy_validation_report,
    build_runtime_forensics_report,
)
from app.identity import AGENT_NAME, APPLICATION_NAME
from app.intelligence.runtime import build_strategy_intelligence_report
from app.news.runtime import build_alpha_vantage_news_adapter_report
from app.orchestration.active_runtime import (
    build_active_intelligence_orchestrator_report,
    build_etoro_calibration_read_only_report,
    build_etoro_demo_runtime_once_report,
    build_etoro_demo_runtime_report,
    build_etoro_full_catalog_candidate_calibration_read_only_report,
    build_etoro_full_catalog_session_audit_report,
    collect_etoro_calibration_evidence,
    etoro_demo_runtime_status,
    request_etoro_demo_runtime_stop,
)
from app.scanner.runtime import (
    DEFAULT_MARKET_SCAN_STORE_PATH,
    build_market_scan_report,
    default_market_scan_store,
)
from app.storage.sqlite import SqliteRecordStore
from app.validation.runtime import build_strategy_validation_report
from app.validation.storage import (
    DEFAULT_STRATEGY_VALIDATION_STORE_PATH,
    default_strategy_validation_store,
)

DEMO_VALIDATION_STORE_PATH = Path("work") / "etoro-demo-execution-validation.sqlite3"


def main(argv: Sequence[str] | None = None, *, values: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.main", description=APPLICATION_NAME)
    parser.add_argument(
        "command",
        choices=(
            "health",
            "portfolio",
            "analyze",
            "paper-status",
            "broker-status",
            "etoro-status",
            "shadow-run",
            "demo-status",
            "reconcile",
            "performance",
            "demo-smoke-test",
            "etoro-readiness",
            "etoro-demo-preflight",
            "etoro-transport-check",
            "etoro-instrument-schema",
            "scan-markets",
            "strategy-intelligence",
            "intelligence-live",
            "etoro-universe-discovery",
            "etoro-instrument-catalog-probe",
            "etoro-dynamic-universe-build",
            "etoro-universe-bootstrap",
            "etoro-crypto-validation-probe",
            "acquire-exit-evidence",
            "alpaca-core-4h-backfill",
            "alpaca-full-backfill",
            "alpaca-provider-pilot",
            "lifecycle-walkforward-readiness",
            "lifecycle-zero-trade-forensics",
            "exitpolicy-v2-train-validation",
            "exitpolicy-v2-exposed-holdout-diagnostic",
            "prospective-shadow-ready",
            "polygon-provider-pilot",
            "validate-strategies",
            "runtime-forensics",
            "active-market-scanner",
            "active-scanner-observation-alignment",
            "active-scan-cycle",
            "active-scanner-1h-foundation",
            "active-scanner-1h-full-universe-sweep",
            "active-scanner-1h-pilot",
            "active-scanner-1h-iex-pilot",
            "alpha-vantage-news-adapter",
            "active-intelligence-orchestrator",
            "etoro-demo-runtime-once",
            "etoro-demo-runtime",
            "etoro-demo-runtime-stop",
            "etoro-demo-runtime-status",
            "etoro-calibration-read-only",
            "etoro-full-catalog-candidate-calibration-read-only",
            "etoro-calibration-collector",
            "etoro-full-catalog-session-audit",
            "etoro-demo-execution-validate",
            "etoro-demo-execution-once",
            "etoro-demo-execution-verify",
        ),
    )
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--asset-class", default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--max-instruments", type=int, default=9)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--offline-fixture", action="store_true")
    parser.add_argument("--real-data", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--collection-interval-seconds", type=float, default=900.0)
    parser.add_argument("--minimum-useful-runs", type=int, default=3)
    parser.add_argument("--minimum-active-denominator", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--confirm-demo-write", action="store_true")
    parser.add_argument("--diagnose-live-quote", action="store_true")
    parser.add_argument("--diagnose-demo-eligibility", action="store_true")
    parser.add_argument("--diagnostic-read-only", action="store_true")
    args = parser.parse_args(argv)
    runtime_values = load_runtime_values(values)
    config = load_config(runtime_values)
    if args.command == "health":
        payload: dict[str, object] = {
            "agent": AGENT_NAME,
            "application": APPLICATION_NAME,
            "environment": config.environment.value,
            "kill_switch": config.kill_switch,
            "paper_trading_available": config.paper_trading.enabled,
            "production_trading_enabled": config.production_trading_enabled,
            "real_execution_available": False,
            "status": "ok",
        }
    elif args.command == "portfolio":
        payload = {
            "application": APPLICATION_NAME,
            "broker_provider": config.providers.broker.value,
            "paper_initial_capital_eur": str(config.initial_capital_eur),
            "portfolio_state": "not_initialized",
            "real_execution_available": False,
        }
    elif args.command == "analyze":
        payload = {
            "agent": AGENT_NAME,
            "application": APPLICATION_NAME,
            "analysis_state": "not_configured",
            "reason": "CLI fixture symbols are not configured",
            "real_execution_available": False,
        }
    elif args.command == "paper-status":
        payload = {
            "application": APPLICATION_NAME,
            "ledger_state": "not_initialized",
            "paper_trading_available": config.paper_trading.enabled,
            "real_execution_available": False,
        }
    elif args.command in {"broker-status", "etoro-status"}:
        status = etoro_status(config, values=runtime_values)
        payload = {
            "application": APPLICATION_NAME,
            **status.model_dump(mode="json"),
        }
    elif args.command == "demo-smoke-test":
        payload = {
            "application": APPLICATION_NAME,
            "status": "BLOCKED",
            "reason": (
                "complete live identity, eligibility, quote, FX, risk, admission, "
                "and explicit opt-in are required"
            ),
            "order_submitted": False,
            "real_execution_available": False,
        }
    elif args.command == "etoro-readiness":
        report = build_etoro_readiness_report(
            config,
            values=runtime_values,
            store=default_etoro_readiness_store(),
        )
        payload = {
            "application": APPLICATION_NAME,
            "record_store_path": str(DEFAULT_READINESS_STORE_PATH),
            **report.model_dump(mode="json"),
        }
    elif args.command == "etoro-demo-preflight":
        preflight_report = build_first_demo_preflight_report(
            config,
            values=runtime_values,
            store=default_demo_preflight_store(Path("work") / "first-demo-preflight.sqlite3"),
        )
        payload = {
            "application": APPLICATION_NAME,
            **preflight_report.model_dump(mode="json"),
        }
    elif args.command == "etoro-transport-check":
        payload = {
            "application": APPLICATION_NAME,
            **etoro_transport_check(config, values=runtime_values),
        }
    elif args.command == "etoro-instrument-schema":
        symbols = (
            tuple(item.strip().upper() for item in args.instrument.split(",") if item.strip())
            if args.instrument
            else ("AAPL", "SPY", "BTC")
        )
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_instrument_schema_report(
                config,
                values=runtime_values,
                symbols=symbols,
            ),
        }
    elif args.command == "scan-markets":
        payload = {
            "application": APPLICATION_NAME,
            **build_market_scan_report(
                config,
                values=runtime_values,
                store=default_market_scan_store(DEFAULT_MARKET_SCAN_STORE_PATH),
            ),
        }
    elif args.command == "strategy-intelligence":
        payload = {
            "application": APPLICATION_NAME,
            **build_strategy_intelligence_report(config),
        }
    elif args.command == "intelligence-live":
        payload = {
            "application": APPLICATION_NAME,
            **build_live_intelligence_report(config, values=runtime_values),
        }
    elif args.command == "etoro-universe-discovery":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_broker_universe_discovery_report(config, values=runtime_values),
        }
    elif args.command == "etoro-instrument-catalog-probe":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_instrument_catalog_probe_report(
                config, values=runtime_values, persist=args.persist
            ),
        }
    elif args.command == "etoro-dynamic-universe-build":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_dynamic_active_universe(),
            "broker_write_calls": 0,
            "real_execution_available": False,
        }
    elif args.command == "etoro-universe-bootstrap":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_universe_bootstrap_report(config, values=runtime_values),
        }
    elif args.command == "etoro-crypto-validation-probe":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_crypto_validation_report(config, values=runtime_values),
        }
    elif args.command == "acquire-exit-evidence":
        payload = {
            "application": APPLICATION_NAME,
            **build_exit_evidence_acquisition_report(config, values=runtime_values),
        }
    elif args.command == "alpaca-core-4h-backfill":
        payload = {
            "application": APPLICATION_NAME,
            **build_alpaca_core_4h_backfill_report(config, values=runtime_values),
        }
    elif args.command == "alpaca-full-backfill":
        payload = {
            "application": APPLICATION_NAME,
            **build_alpaca_full_backfill_report(config, values=runtime_values),
        }
    elif args.command == "alpaca-provider-pilot":
        payload = {
            "application": APPLICATION_NAME,
            **build_alpaca_provider_pilot_report(config, values=runtime_values),
        }
    elif args.command == "lifecycle-walkforward-readiness":
        payload = {
            "application": APPLICATION_NAME,
            **build_lifecycle_walkforward_readiness_report(config),
        }
    elif args.command == "lifecycle-zero-trade-forensics":
        payload = {
            "application": APPLICATION_NAME,
            **build_lifecycle_zero_trade_forensics_report(config),
        }
    elif args.command == "exitpolicy-v2-train-validation":
        payload = {
            "application": APPLICATION_NAME,
            **build_exit_policy_v2_train_validation_report(config),
        }
    elif args.command == "exitpolicy-v2-exposed-holdout-diagnostic":
        payload = {
            "application": APPLICATION_NAME,
            **build_exit_policy_v2_exposed_holdout_diagnostic_report(config),
        }
    elif args.command == "prospective-shadow-ready":
        payload = {
            "application": APPLICATION_NAME,
            **build_prospective_shadow_validation_readiness_report(config),
        }
    elif args.command == "polygon-provider-pilot":
        payload = {
            "application": APPLICATION_NAME,
            **build_polygon_provider_pilot_report(config, values=runtime_values),
        }
    elif args.command == "validate-strategies":
        requested_filters = {
            "instrument": args.instrument,
            "asset_class": args.asset_class,
            "start": args.start,
            "end": args.end,
            "strategy": args.strategy,
            "timeframe": args.timeframe,
            "walk_forward": args.walk_forward,
            "max_instruments": args.max_instruments,
        }
        if args.real_data:
            payload = {
                "application": APPLICATION_NAME,
                **build_real_strategy_validation_report(
                    config,
                    values=runtime_values,
                    asset_class=args.asset_class,
                    instrument=args.instrument,
                    timeframe=args.timeframe,
                    start=args.start,
                    end=args.end,
                    max_instruments=args.max_instruments,
                    store=default_strategy_validation_store(DEFAULT_STRATEGY_VALIDATION_STORE_PATH),
                ),
                "requested_filters": requested_filters,
            }
        else:
            payload = {
                "application": APPLICATION_NAME,
                **build_strategy_validation_report(
                    config,
                    offline_fixture=True,
                    store=default_strategy_validation_store(DEFAULT_STRATEGY_VALIDATION_STORE_PATH),
                ),
                "requested_filters": requested_filters,
            }
    elif args.command == "runtime-forensics":
        payload = {
            "application": APPLICATION_NAME,
            **build_runtime_forensics_report(),
        }
    elif args.command == "active-market-scanner":
        payload = {
            "application": APPLICATION_NAME,
            **build_active_market_scanner_foundation_report(config),
        }
    elif args.command == "active-scanner-observation-alignment":
        payload = {
            "application": APPLICATION_NAME,
            **build_active_scanner_observation_temporal_alignment_report(config),
        }
        report_path = Path("reports") / "step9_0a3_scanner_observation_temporal_alignment.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _render_active_scanner_observation_alignment_report(payload),
            encoding="utf-8",
        )
        payload["report_path"] = str(report_path)
    elif args.command == "active-scan-cycle":
        payload = {
            "application": APPLICATION_NAME,
            **build_readonly_active_scan_cycle_report(config),
        }
        report_path = Path("reports") / "step9_0a4_readonly_active_scan_cycle.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _render_readonly_active_scan_cycle_report(payload),
            encoding="utf-8",
        )
        payload["report_path"] = str(report_path)
    elif args.command == "active-scanner-1h-foundation":
        payload = {
            "application": APPLICATION_NAME,
            **build_active_scanner_1h_foundation_report(config, values=runtime_values),
        }
    elif args.command == "active-scanner-1h-full-universe-sweep":
        payload = {
            "application": APPLICATION_NAME,
            **build_active_scanner_1h_full_universe_sweep_report(
                config,
                values=runtime_values,
            ),
        }
        report_path = Path("reports") / "step9_0a2d_1h_full_universe_readiness_sweep.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _render_active_scanner_1h_pilot_report(payload),
            encoding="utf-8",
        )
        payload["report_path"] = str(report_path)
    elif args.command == "active-scanner-1h-pilot":
        payload = {
            "application": APPLICATION_NAME,
            **build_active_scanner_1h_pilot_report(config, values=runtime_values),
        }
        report_path = Path("reports") / "step9_0a2_1h_readonly_pilot.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _render_active_scanner_1h_pilot_report(payload),
            encoding="utf-8",
        )
        payload["report_path"] = str(report_path)
    elif args.command == "active-scanner-1h-iex-pilot":
        payload = {
            "application": APPLICATION_NAME,
            **build_active_scanner_1h_iex_equity_pilot_report(
                config,
                values=runtime_values,
            ),
        }
        report_path = Path("reports") / "step9_0a2b_iex_equity_1h_pilot.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _render_active_scanner_1h_pilot_report(payload),
            encoding="utf-8",
        )
        payload["report_path"] = str(report_path)
    elif args.command == "alpha-vantage-news-adapter":
        payload = {
            "application": APPLICATION_NAME,
            **build_alpha_vantage_news_adapter_report(config),
        }
    elif args.command == "active-intelligence-orchestrator":
        payload = {
            "application": APPLICATION_NAME,
            **build_active_intelligence_orchestrator_report(config),
        }
    elif args.command == "etoro-demo-runtime-once":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_demo_runtime_once_report(config, values=runtime_values),
        }
    elif args.command == "etoro-calibration-read-only":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_calibration_read_only_report(
                config,
                values=runtime_values,
                max_iterations=args.max_iterations or 1,
            ),
        }
    elif args.command == "etoro-full-catalog-candidate-calibration-read-only":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_full_catalog_candidate_calibration_read_only_report(
                config,
                values=runtime_values,
                max_iterations=args.max_iterations or 1,
                acquisition_batch_size=args.batch_size,
            ),
        }
    elif args.command == "etoro-calibration-collector":
        payload = {
            "application": APPLICATION_NAME,
            **collect_etoro_calibration_evidence(
                config,
                values=runtime_values,
                max_attempts=args.max_attempts,
                interval_seconds=args.collection_interval_seconds,
                minimum_useful_runs=args.minimum_useful_runs,
                minimum_active_denominator=args.minimum_active_denominator,
            ),
        }
    elif args.command == "etoro-full-catalog-session-audit":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_full_catalog_session_audit_report(
                config,
                values=runtime_values,
                batch_size=args.batch_size,
                concurrency=config.scanner.live_acquisition_concurrency,
            ),
        }
    elif args.command == "etoro-demo-runtime":
        payload = {
            "application": APPLICATION_NAME,
            **build_etoro_demo_runtime_report(
                config,
                values=runtime_values,
                max_iterations=args.max_iterations,
                diagnostic_read_only=args.diagnostic_read_only,
            ),
        }
    elif args.command == "etoro-demo-runtime-stop":
        payload = {
            "application": APPLICATION_NAME,
            **request_etoro_demo_runtime_stop(),
        }
    elif args.command == "etoro-demo-runtime-status":
        payload = {
            "application": APPLICATION_NAME,
            **etoro_demo_runtime_status(),
        }
    elif args.command == "etoro-demo-execution-validate":
        payload = {
            "application": APPLICATION_NAME,
            **run_user_confirmed_demo_validation(
                config,
                values=runtime_values,
                confirm_demo_write=args.confirm_demo_write,
                store=SqliteRecordStore(DEMO_VALIDATION_STORE_PATH),
            ),
        }
    elif args.command == "etoro-demo-execution-once" and args.diagnose_demo_eligibility:
        from app.brokers.etoro.eligibility_diagnostic import diagnose_demo_eligibility

        payload = {
            "application": APPLICATION_NAME,
            **diagnose_demo_eligibility(config, runtime_values),
        }
    elif args.command == "etoro-demo-execution-once":
        payload = {
            "application": APPLICATION_NAME,
            **run_operational_demo_once(
                config,
                values=runtime_values,
                confirm_demo_write=args.confirm_demo_write,
                store=SqliteRecordStore(DEMO_VALIDATION_STORE_PATH),
                diagnose_only=args.diagnose_live_quote,
            ),
        }
    elif args.command == "etoro-demo-execution-verify":
        payload = {
            "application": APPLICATION_NAME,
            **verify_demo_validation_read_only(
                config,
                values=runtime_values,
                store=SqliteRecordStore(DEMO_VALIDATION_STORE_PATH),
            ),
        }
    else:
        payload = {
            "application": APPLICATION_NAME,
            "command": args.command,
            "status": "NOT_CONFIGURED",
            "real_execution_available": False,
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _render_active_scanner_1h_pilot_report(payload: Mapping[str, object]) -> str:
    lines: list[str] = []
    lines.append(f"# {payload.get('report_title', 'STEP 9.0A2 1H Read-Only Data Pilot')}")
    lines.append("")
    lines.append(f"- status: `{payload.get('status', 'UNKNOWN')}`")
    lines.append(f"- phase: `{payload.get('phase', 'UNKNOWN')}`")
    lines.append(f"- broker_write_calls: `{payload.get('broker_write_calls', 0)}`")
    lines.append(f"- demo_execution_enabled: `{payload.get('demo_execution_enabled', False)}`")
    lines.append(f"- real_execution_available: `{payload.get('real_execution_available', False)}`")
    lines.append(f"- windows_cmd: `{payload.get('windows_cmd', '')}`")
    lines.append("")
    lines.append("## Requested Universe")
    lines.append("")
    lines.append("```json")
    lines.append(
        json.dumps(
            payload.get("pilot_symbols", payload.get("requested_symbols", {})),
            sort_keys=True,
        )
    )
    lines.append("```")
    lines.append("")
    if "asset_class_readiness" in payload or "global_1h" in payload:
        lines.append("## Readiness Summary")
        lines.append("")
        if "asset_class_readiness" in payload:
            lines.append("```json")
            lines.append(json.dumps(payload.get("asset_class_readiness"), sort_keys=True))
            lines.append("```")
            lines.append("")
        if "global_1h" in payload:
            lines.append(f"- global_1h: `{payload.get('global_1h')}`")
            lines.append("")
    lines.append("## Acquisition")
    lines.append("")
    lines.append(
        "| symbol | asset_class | provider | provider_symbol | requested_feed | "
        "feed_provenance | final_status | fetched_bars | cached_context_bars | "
        "earliest | latest | freshness | market_session_state | expected_latest_bar | "
        "duplicate_timestamps | ohlcv_valid | pagination_verified | cache_round_trip | "
        "broker_write_calls |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- | "
        "--- | --- | ---: | --- | --- | --- | --- | ---: |"
    )
    acquisition = cast(tuple[Mapping[str, object], ...], payload.get("acquisition", ()))
    for row in acquisition:
        if not isinstance(row, Mapping):
            continue
        cache_round_trip = (
            f"inserted={row.get('cache_inserted', 0)}, "
            f"updated={row.get('cache_updated', 0)}, "
            f"unchanged={row.get('cache_unchanged', 0)}"
        )
        pagination_verified = (
            bool(row.get("pagination_requested"))
            and bool(row.get("pagination_token_observed"))
            and bool(row.get("second_page_fetched"))
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("symbol", "")),
                    str(row.get("asset_class", "")),
                    str(row.get("provider", "")),
                    str(row.get("provider_symbol", "")),
                    str(row.get("requested_feed", "")),
                    str(row.get("provider_feed_provenance", "")),
                    str(row.get("final_status", "")),
                    str(row.get("fetched_bars", "")),
                    str(row.get("cached_context_bars", "")),
                    str(row.get("earliest_cached_context", "")),
                    str(row.get("latest_cached_context", "")),
                    str(row.get("freshness_status", "")),
                    str(row.get("market_session_state", "")),
                    str(row.get("expected_latest_bar", "")),
                    str(row.get("duplicate_timestamps", "")),
                    str(row.get("ohlcv_valid", "")),
                    str(row.get("pagination_verified", pagination_verified)),
                    cache_round_trip,
                    str(row.get("broker_write_calls", "")),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Scanner Output")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload.get("example_scanner_output"), sort_keys=True, default=str))
    lines.append("```")
    lines.append("")
    lines.append("## Final Classification")
    lines.append("")
    lines.append(
        f"- repeated_intraday_shadow_scan_ready: "
        f"`{payload.get('repeated_intraday_shadow_scan_ready', False)}`"
    )
    lines.append(f"- next_blocker: `{payload.get('next_blocker')}`")
    lines.append("")
    return "\n".join(lines)


def _render_active_scanner_observation_alignment_report(
    payload: Mapping[str, object],
) -> str:
    lines: list[str] = []
    lines.append("# STEP 9.0A3 Scanner Observation Temporal Alignment")
    lines.append("")
    lines.append(f"- status: `{payload.get('status', 'UNKNOWN')}`")
    lines.append(f"- phase: `{payload.get('phase', 'UNKNOWN')}`")
    lines.append(f"- scan_cycle_timestamp: `{payload.get('scan_cycle_timestamp', 'UNKNOWN')}`")
    lines.append(f"- observation_model: `{payload.get('observation_model', 'UNKNOWN')}`")
    lines.append(f"- temporal_alignment: `{payload.get('temporal_alignment', 'UNKNOWN')}`")
    lines.append(f"- cross_asset_snapshot: `{payload.get('cross_asset_snapshot', 'UNKNOWN')}`")
    lines.append(f"- future_bar_exclusion: `{payload.get('future_bar_exclusion', False)}`")
    lines.append(f"- stale_exclusion: `{payload.get('stale_exclusion', False)}`")
    lines.append(f"- mixed_timestamp_handling: `{payload.get('mixed_timestamp_handling', False)}`")
    lines.append(
        "- duplicate_evaluation_prevention: "
        f"`{payload.get('duplicate_evaluation_prevention', False)}`"
    )
    lines.append(f"- position_independence: `{payload.get('position_independence', False)}`")
    lines.append(f"- anti_lookahead_verified: `{payload.get('anti_lookahead_verified', False)}`")
    lines.append(f"- broker_write_calls: `{payload.get('broker_write_calls', 0)}`")
    lines.append("")
    lines.append("## Current Capabilities")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload.get("current_capabilities"), sort_keys=True, default=str))
    lines.append("```")
    lines.append("")
    lines.append("## Timeframe Readiness")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload.get("timeframe_readiness"), sort_keys=True, default=str))
    lines.append("```")
    lines.append("")
    lines.append("## Observation Snapshot")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload.get("scan_snapshot"), sort_keys=True, default=str))
    lines.append("```")
    lines.append("")
    lines.append("## Scanner Output")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload.get("scanner_output"), sort_keys=True, default=str))
    lines.append("```")
    lines.append("")
    lines.append("## Critical Blockers")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload.get("critical_blockers"), sort_keys=True, default=str))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _render_readonly_active_scan_cycle_report(payload: Mapping[str, object]) -> str:
    lines = [
        "# STEP 9.0A4 Read-Only Active Scan Cycle",
        "",
        f"- status: `{payload.get('status', 'UNKNOWN')}`",
        f"- scan_cycle_timestamp: `{payload.get('scan_cycle_timestamp', 'UNKNOWN')}`",
        f"- assets_requested: `{payload.get('assets_requested', 0)}`",
        f"- assets_comparable: `{payload.get('assets_comparable', 0)}`",
        f"- assets_excluded: `{payload.get('assets_excluded', 0)}`",
        f"- broker_write_calls: `{payload.get('broker_write_calls', 0)}`",
        f"- demo_execution_enabled: `{payload.get('demo_execution_enabled', False)}`",
        f"- real_execution_available: `{payload.get('real_execution_available', False)}`",
        "",
        "## Asset Observations",
        "",
        "```json",
        json.dumps(payload.get("asset_observations", ()), sort_keys=True, default=str),
        "```",
        "",
        "## Ranking",
        "",
        "```json",
        json.dumps(
            {
                "top_opportunities": payload.get("top_opportunities", ()),
                "watchlist": payload.get("watchlist", ()),
                "no_trade": payload.get("no_trade", ()),
                "rejected": payload.get("rejected", ()),
            },
            sort_keys=True,
            default=str,
        ),
        "```",
        "",
        "## Position Management Set",
        "",
        "```json",
        json.dumps(payload.get("positions_to_manage", ()), sort_keys=True, default=str),
        "```",
        "",
        "## Temporal Metadata",
        "",
        "```json",
        json.dumps(payload.get("temporal_metadata", {}), sort_keys=True, default=str),
        "```",
        "",
        "## Critical Blockers",
        "",
        "```json",
        json.dumps(payload.get("critical_blockers", ()), sort_keys=True, default=str),
        "```",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
