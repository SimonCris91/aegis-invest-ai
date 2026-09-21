"""Typed Home snapshot adapter; strategy and broker layers stay behind this boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WatchlistAsset(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    asset_class: str | None = None
    score: Decimal | None = None
    confidence: Decimal | None = None
    rank: int | None = None
    action: str | None = None
    timeframe: str | None = None
    current_market_state: str | None = None
    data_quality: str | None = None
    freshness: str | None = None
    provider_provenance: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    news_sentiment: str | None = None
    material_event_count: int | None = None
    headline_event_summaries: tuple[str, ...] = ()
    news_risk_flags: tuple[str, ...] = ()


class PositionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    asset_class: str | None = None
    value: Decimal | None = None
    score: Decimal | None = None
    confidence: Decimal | None = None
    action: str | None = None
    timeframe: str | None = None
    scan_cycle_timestamp: datetime | None = None
    bar_timestamp: datetime | None = None
    data_quality: str | None = None
    provider_provenance: tuple[str, ...] = ()
    freshness: str | None = None
    market_session_state: str | None = None
    state: str
    eligible_for_entry_comparison: bool | None = None
    eligibility_reason_code: str | None = None
    current_market_state: str | None = None
    risk_flags: tuple[str, ...] = ()


class CapitalSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal | None = None
    currency: str | None = None
    mode: Literal["SIMULATED", "PAPER", "LIVE", "UNKNOWN"] = "UNKNOWN"


class ScannerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    isolation_status: str
    assets_scanned: int | None = Field(default=None, ge=0)
    assets_comparable: int | None = Field(default=None, ge=0)
    catalog_count: int | None = Field(default=None, ge=0)
    catalog_verified_count: int | None = Field(default=None, ge=0)
    catalog_ready_count: int | None = Field(default=None, ge=0)
    catalog_blocked_count: int | None = Field(default=None, ge=0)
    catalog_pending_count: int | None = Field(default=None, ge=0)
    universe_count: int | None = Field(default=None, ge=0)
    coherent_now: int | None = Field(default=None, ge=0)
    top_opportunities: int | None = Field(default=None, ge=0)
    watchlist_count: int | None = Field(default=None, ge=0)
    no_trade_count: int | None = Field(default=None, ge=0)
    last_scan_watchlist_count: int | None = Field(default=None, ge=0)
    last_scan_no_trade_count: int | None = Field(default=None, ge=0)


class PositionsSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    open_count: int | None = Field(default=None, ge=0)
    items: tuple[PositionSummary, ...] = ()


class SafetySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_mode: Literal["READ_ONLY", "DEMO", "PAPER", "LIVE", "UNKNOWN"]
    broker_write_calls: int | None = Field(default=None, ge=0)


class DataHealthSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    one_hour_status: Literal["READY", "DEGRADED", "MISSING", "UNKNOWN"]
    freshness_state: str | None = None
    market_session_state: str | None = None
    scanner_cycle_status: str | None = None
    last_scan_at: datetime | None = None
    backend_status: Literal["READY", "DEGRADED", "ERROR", "UNKNOWN"]


class AegisHomeSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: datetime
    capital: CapitalSnapshot
    scanner: ScannerSnapshot
    top_opportunity_items: tuple[dict[str, object], ...] = ()
    last_scan_top_opportunities: tuple[dict[str, object], ...] = ()
    watchlist: tuple[WatchlistAsset, ...]
    positions: PositionsSnapshot
    safety: SafetySnapshot
    data_health: DataHealthSnapshot
    news: dict[str, object] = Field(default_factory=dict)
    live_system_status: dict[str, object] | None = None


RUNNER_HEARTBEAT_STALE_AFTER_SECONDS = 180


def home_snapshot_from_scan_cycle(
    report: dict[str, object],
    *,
    live_system_status: dict[str, object] | None = None,
) -> AegisHomeSnapshot:
    """Map the existing read-only scanner report into the UI contract."""
    status = str(report.get("status", "UNKNOWN"))
    scanner_output = _mapping(report.get("scanner_output"))
    watchlist_rows = _sequence(report.get("watchlist"))
    positions = _sequence(report.get("positions_to_manage"))
    as_of = _parse_datetime(report.get("scan_cycle_timestamp"))
    comparable = _optional_int(report.get("assets_comparable"))
    scanned = _optional_int(report.get("assets_requested"))
    backend_status = "READY" if status == "READ_ONLY_ACTIVE_SCAN_CYCLE_READY" else "DEGRADED"
    data_readiness = str(report.get("data_readiness", "UNKNOWN"))
    one_hour_status = (
        data_readiness if data_readiness in {"READY", "DEGRADED", "MISSING"} else "UNKNOWN"
    )
    execution_mode = str(report.get("execution_mode", "UNKNOWN"))
    if execution_mode not in {"READ_ONLY", "PAPER", "LIVE"}:
        execution_mode = "UNKNOWN"
    live_scanner = _mapping(_mapping(live_system_status).get("scanner"))
    live_universe = _mapping(_mapping(live_system_status).get("universe"))
    live_capital = _mapping(_mapping(live_system_status).get("capital"))
    live_demo = _mapping(_mapping(live_system_status).get("demo"))
    live_cycle = _mapping(_mapping(live_system_status).get("cycle"))
    live_acquisition = _mapping(_mapping(live_system_status).get("acquisition"))
    if live_system_status is not None:
        # The read-only report describes the dashboard, not runner authority.
        execution_mode = (
            "DEMO" if live_demo.get("execution_enabled") is True
            else "READ_ONLY" if live_demo.get("execution_enabled") is False
            else "UNKNOWN"
        )
        heartbeat = _mapping(live_system_status.get("heartbeat"))
        runner = _mapping(live_system_status.get("runner"))
        fresh_runner = runner.get("state") == "RUNNING" and heartbeat.get("stale") is False
        ratio = _optional_decimal(live_acquisition.get("coverage_ratio"))
        minimum = _optional_decimal(live_acquisition.get("minimum_coverage"))
        eligible = _optional_int(live_acquisition.get("eligible_count"))
        market_ready = (fresh_runner and live_acquisition.get("status") == "COMPLETE"
                        and ratio is not None and minimum is not None and ratio >= minimum
                        and eligible is not None and eligible > 0)
        one_hour_status = "READY" if market_ready else "DEGRADED"
        backend_status = "READY" if fresh_runner else "DEGRADED"
    live_top_count = (
        _optional_int(live_scanner.get("top_opportunity_count"))
        if "top_opportunity_count" in live_scanner
        else None
    )
    live_active_count = (
        _optional_int(live_universe.get("active_scanner_universe_count"))
        if "active_scanner_universe_count" in live_universe
        else None
    )
    # The accepted cycle remains the source for last-scan detail. Operational
    # counters, when persisted by the live runtime, must not be backfilled
    # from that historical snapshot.
    has_live_runtime = live_system_status is not None
    current_watchlist_count = (
        None
        if has_live_runtime and "watchlist_count" not in live_scanner
        else (
            _optional_int(report.get("watchlist_count"), default=0)
            if "watchlist_count" in report
            else len(watchlist_rows)
        )
    )
    current_no_trade_count = (
        None
        if has_live_runtime and "no_trade_count" not in live_scanner
        else (
            _optional_int(report.get("no_trade_count"), default=0)
            if "no_trade_count" in report
            else len(_sequence(report.get("no_trade")))
        )
    )
    return AegisHomeSnapshot(
        as_of=as_of,
        capital=CapitalSnapshot(
            amount=(
                _optional_decimal(live_capital.get("authorized_capital_eur"))
                if "authorized_capital_eur" in live_capital
                else _optional_decimal(scanner_output.get("simulated_capital"))
            ),
            currency=_optional_string(report.get("capital_currency")),
            mode=_capital_mode(report.get("capital_mode")),
        ),
        scanner=ScannerSnapshot(
            status=status,
            isolation_status="MULTI_CYCLE_ISOLATION_VERIFIED",
            assets_scanned=scanned,
            assets_comparable=comparable,
            catalog_count=_optional_int(live_universe.get("catalog_instrument_count")),
            catalog_verified_count=_optional_int(
                live_universe.get("catalog_verified_mapping_count")
            ),
            catalog_ready_count=_optional_int(live_universe.get("catalog_market_data_ready_count")),
            catalog_blocked_count=_optional_int(live_universe.get("catalog_blocked_count")),
            catalog_pending_count=_optional_int(live_universe.get("catalog_pending_count")),
            universe_count=live_active_count,
            coherent_now=_optional_int(live_acquisition.get("eligible_count")),
            top_opportunities=(
                live_top_count
                if live_top_count is not None
                else (
                    _optional_int(report.get("top_opportunities_count"), default=0)
                    if "top_opportunities_count" in report
                    else len(_sequence(report.get("top_opportunities")))
                )
            ),
            watchlist_count=current_watchlist_count,
            no_trade_count=current_no_trade_count,
            last_scan_watchlist_count=len(watchlist_rows),
            last_scan_no_trade_count=len(_sequence(report.get("no_trade"))),
        ),
        top_opportunity_items=_sequence(
            report.get("top_opportunity_items", report.get("top_opportunities"))
        ),
        last_scan_top_opportunities=_sequence(
            report.get(
                "last_scan_top_opportunities",
                report.get("top_opportunity_items", report.get("top_opportunities")),
            )
        ),
        watchlist=tuple(
            WatchlistAsset(
                symbol=str(row.get("symbol", "")),
                name=str(row.get("full_asset_name", row.get("symbol", ""))),
                asset_class=_optional_string(row.get("asset_class")),
                score=_optional_decimal(row.get("opportunity_score")),
                confidence=_optional_decimal(row.get("confidence")),
                rank=_optional_int(row.get("rank")),
                action=_optional_string(row.get("action", row.get("action_state"))),
                timeframe=_optional_string(row.get("timeframe")),
                current_market_state=_optional_string(row.get("current_market_state")),
                data_quality=_optional_string(row.get("data_quality", row.get("data_quality_state"))),
                freshness=_optional_string(row.get("freshness", row.get("freshness_state"))),
                provider_provenance=_strings(row.get("provider_provenance")),
                reasons=_strings(row.get("rejection_reasons")),
                risk_flags=_strings(row.get("risk_flags")),
                news_sentiment=_optional_string(row.get("news_sentiment")),
                material_event_count=_optional_int(row.get("material_event_count")),
                headline_event_summaries=_strings(row.get("headline_event_summaries")),
                news_risk_flags=_strings(row.get("news_risk_flags")),
            )
            for row in watchlist_rows
        ),
        positions=PositionsSnapshot(
            open_count=len(positions) if "positions_to_manage" in report else None,
            items=tuple(
                PositionSummary(
                    symbol=str(row.get("symbol", "")),
                    name=str(row.get("full_name", row.get("full_asset_name", row.get("symbol", "")))),
                    asset_class=_optional_string(row.get("asset_class")),
                    value=_optional_decimal(row.get("current_price")),
                    score=_optional_decimal(row.get("opportunity_score")),
                    confidence=_optional_decimal(row.get("confidence")),
                    action=_optional_string(row.get("action", row.get("action_state"))),
                    timeframe=_optional_string(row.get("timeframe")),
                    scan_cycle_timestamp=_parse_optional_datetime(row.get("scan_cycle_timestamp")),
                    bar_timestamp=_parse_optional_datetime(row.get("bar_timestamp")),
                    data_quality=_optional_string(row.get("data_quality", row.get("data_quality_state"))),
                    provider_provenance=_strings(row.get("provider_provenance")),
                    freshness=_optional_string(row.get("freshness", row.get("freshness_state"))),
                    market_session_state=_optional_string(row.get("market_session_state")),
                    state=str(row.get("existing_position_state", "UNKNOWN")),
                    eligible_for_entry_comparison=(
                        bool(row.get("eligible_for_entry_comparison"))
                        if "eligible_for_entry_comparison" in row
                        else None
                    ),
                    eligibility_reason_code=_optional_string(row.get("eligibility_reason_code")),
                    current_market_state=_optional_string(row.get("current_market_state")),
                    risk_flags=_strings(row.get("risk_flags")),
                )
                for row in positions
            ),
        ),
        safety=SafetySnapshot(
            execution_mode=execution_mode,
            broker_write_calls=(
                _optional_int(live_demo.get("broker_write_calls"))
                if "broker_write_calls" in live_demo
                else _optional_int(report.get("broker_write_calls"))
            ),
        ),
        data_health=DataHealthSnapshot(
            one_hour_status=one_hour_status,
            freshness_state=(
                _optional_string(live_cycle.get("freshness_state"))
                if "freshness_state" in live_cycle
                else _optional_string(report.get("data_freshness"))
            ),
            market_session_state=(
                _optional_string(live_cycle.get("market_session_state"))
                if "market_session_state" in live_cycle
                else _optional_string(report.get("market_session_state"))
            ),
            scanner_cycle_status=(
                _optional_string(live_cycle.get("state"))
                if "state" in live_cycle
                else _optional_string(report.get("scanner_cycle_status"))
            ),
            last_scan_at=as_of,
            backend_status=backend_status,
        ),
        news=_news_snapshot(report, live_system_status),
        live_system_status=live_system_status,
    )


def live_system_status_from_runtime(
    runtime: dict[str, object] | None,
    *,
    overnight_activity: dict[str, object] | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Project only persisted runner fields into the read-only Home contract."""
    if runtime is None:
        return None
    grouped: dict[str, dict[str, object]] = {
        "runner": {},
        "cycle": {},
        "scanner": {},
        "demo": {},
        "real": {},
        "capital": {},
        "activity": {},
        "universe": {},
        "acquisition": {},
        "news": {},
    }
    field_map = {
        "runner_state": ("runner", "state"),
        "cycle_state": ("cycle", "state"),
        "last_successful_scan_at": ("cycle", "last_successful_scan_at"),
        "market_session_state": ("cycle", "market_session_state"),
        "freshness_state": ("cycle", "freshness_state"),
        "top_opportunity_count": ("scanner", "top_opportunity_count"),
        "automatic_pilot_armed": ("demo", "automatic_pilot_armed"),
        "execution_enabled": ("demo", "execution_enabled"),
        "last_submission_status": ("demo", "last_submission_status"),
        "demo_broker_write_calls": ("demo", "broker_write_calls"),
        "execution_available": ("real", "execution_available"),
        "broker_write_calls_real": ("real", "broker_write_calls"),
        "activity_code": ("activity", "code"),
        "blockers": ("activity", "blockers"),
        "last_error": ("activity", "last_error"),
        "pilot_notional_eur": ("demo", "pilot_notional_eur"),
        "authorized_capital_eur": ("capital", "authorized_capital_eur"),
        "managed_exposure_eur": ("capital", "managed_exposure_eur"),
        "remaining_authorized_capital_eur": ("capital", "remaining_authorized_capital_eur"),
        "sizing_mode": ("capital", "sizing_mode"),
        "active_scanner_universe_count": ("universe", "active_scanner_universe_count"),
        "validated_baseline_count": ("universe", "validated_baseline_count"),
        "catalog_instrument_count": ("universe", "catalog_instrument_count"),
        "catalog_verified_mapping_count": ("universe", "catalog_verified_mapping_count"),
        "catalog_market_data_ready_count": ("universe", "catalog_market_data_ready_count"),
        "catalog_blocked_count": ("universe", "catalog_blocked_count"),
        "catalog_pending_count": ("universe", "catalog_pending_count"),
        "etoro_discovered_count": ("universe", "etoro_discovered_count"),
        "etoro_verified_count": ("universe", "etoro_verified_count"),
        "market_data_ready_count": ("universe", "market_data_ready_count"),
        "scanner_expansion_candidate_count": ("universe", "scanner_expansion_candidate_count"),
        "open_tradable_checked": ("scanner", "open_tradable_checked"),
        "acquisition_status": ("acquisition", "status"),
        "acquisition_instruments_requested": ("acquisition", "requested"),
        "acquisition_instruments_attempted": ("acquisition", "attempted"),
        "acquisition_instruments_not_attempted": ("acquisition", "not_attempted"),
        "acquisition_instruments_in_backoff": ("acquisition", "in_backoff"),
        "acquisition_instruments_updated": ("acquisition", "updated"),
        "acquisition_newest_completed_bar": ("acquisition", "newest_completed_bar"),
        "acquisition_outcome_counts": ("acquisition", "outcome_counts"),
        "coherent_eligible_count": ("acquisition", "eligible_count"),
        "coherent_coverage_ratio": ("acquisition", "coverage_ratio"),
        "coherent_coverage_minimum": ("acquisition", "minimum_coverage"),
        "news_provider": ("news", "provider"),
        "news_provider_status": ("news", "status"),
        "news_rate_limit_triggered": ("news", "rate_limit_triggered"),
        "news_provider_request_count": ("news", "provider_request_count"),
        "news_requests_suppressed_after_rate_limit": (
            "news",
            "requests_suppressed_after_rate_limit",
        ),
        "news_cache_hits": ("news", "cache_hits"),
        "news_cache_misses": ("news", "cache_misses"),
        "news_provider_diagnostics": ("news", "provider_diagnostics"),
        "news_event_digest": ("news", "event_digest"),
        "news_asset_contexts": ("news", "asset_contexts"),
        "global_risk_context": ("news", "global_risk_context"),
        "demo_submissions_attempted": ("demo", "submission_attempts"),
        "demo_writes": ("demo", "writes"),
        "real_writes": ("real", "writes"),
    }
    for source, (group, target) in field_map.items():
        if source in runtime:
            grouped[group][target] = runtime[source]
    heartbeat_at = _parse_optional_datetime(runtime.get("observed_at"))
    if heartbeat_at is not None:
        reference_time = now or datetime.now(UTC)
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=UTC)
        age_seconds = max(0.0, (reference_time - heartbeat_at).total_seconds())
        stale = age_seconds > RUNNER_HEARTBEAT_STALE_AFTER_SECONDS
        grouped["heartbeat"] = {
            "data_timestamp": heartbeat_at.isoformat(),
            "data_age_seconds": age_seconds,
            "last_activity_at": heartbeat_at.isoformat(),
            "age_seconds": age_seconds,
            "stale_after_seconds": RUNNER_HEARTBEAT_STALE_AFTER_SECONDS,
            "stale_threshold_seconds": RUNNER_HEARTBEAT_STALE_AFTER_SECONDS,
            "stale": stale,
            "is_stale": stale,
        }
        if stale and grouped.get("runner", {}).get("state") == "RUNNING":
            grouped["runner"]["state"] = "STALE"
    projected: dict[str, object] = {group: values for group, values in grouped.items() if values}
    if overnight_activity is not None:
        projected["overnight_activity"] = overnight_activity
        latest_exit_management = overnight_activity.get("latest_exit_management")
        if isinstance(latest_exit_management, dict):
            projected["exit_management"] = latest_exit_management
    return projected


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _news_snapshot(
    report: dict[str, object], live_system_status: dict[str, object] | None
) -> dict[str, object]:
    """Expose persisted news context without exposing raw provider payloads."""
    live_news = _mapping(_mapping(live_system_status).get("news"))
    live_asset_contexts = live_news.get("asset_contexts", report.get("news_asset_contexts", {}))
    payload = {
        "provider": live_news.get("provider", report.get("news_provider")),
        "status": live_news.get("status", report.get("news_provider_status")),
        "provider_diagnostics": live_news.get(
            "provider_diagnostics", report.get("news_provider_diagnostics", {})
        ),
        "global_risk": live_news.get(
            "global_risk_context", report.get("global_risk_context", {})
        ),
        "events": _sequence(
            live_news.get("event_digest", report.get("news_event_digest"))
        ),
        "asset_contexts": (
            live_asset_contexts
            if isinstance(live_asset_contexts, dict)
            else {}
        ),
    }
    return {key: value for key, value in payload.items() if value not in (None, {}, ())}


def _sequence(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(str(item) for item in value)


def _optional_int(value: object, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return None


def _optional_string(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _capital_mode(value: object) -> Literal["SIMULATED", "PAPER", "LIVE", "UNKNOWN"]:
    candidate = str(value) if value is not None else "UNKNOWN"
    if candidate == "SIMULATED":
        return "SIMULATED"
    if candidate == "PAPER":
        return "PAPER"
    if candidate == "LIVE":
        return "LIVE"
    return "UNKNOWN"


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("Home snapshot requires a causal scan timestamp")


def _parse_optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
