"""Offline runtime report for the active intelligence orchestrator."""

from __future__ import annotations

import json
import inspect
import os
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import sleep
from threading import Event, Thread
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.demo_pilot import (
    DemoSubmissionGateway,
    EtoroAutomaticDemoPilot,
    EtoroDemoPilotResult,
    EtoroDemoPilotSettings,
    EtoroDemoSubmissionPackage,
    RiskCheckedEtoroDemoSubmissionGateway,
    demo_pilot_settings,
)
from app.brokers.etoro.demo_exit import manage_demo_exits
from app.brokers.etoro.demo_preflight import (
    _instrument_from_eligibility,
    _portfolio_from_demo_snapshot,
    _preflight_market_status,
)
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.mapping import (
    asset_class_from_etoro_instrument_type,
    classify_etoro_instrument_metadata,
)
from app.brokers.etoro.runtime import runtime_credentials
from app.brokers.models import BrokerIdentity, ExecutionState
from app.brokers.preflight import evaluate_demo_preflight
from app.config.models import ApplicationConfig
from app.data.historical.cache import HistoricalDataCache
from app.data.historical.alpaca import AlpacaHistoricalMarketDataProvider
from app.data.historical.etoro import _normalize_etoro_candles
from app.data.models import ProviderInstrumentReference
from app.domain.enums import (
    AssetClass,
    BrokerExecutionMode,
    Currency,
    ExecutionPolicy,
    HoldingPeriod,
    MarketStatus,
    OperatingMode,
    ProviderMode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.risk import AuthorizedCapitalEnvelope, RiskContext
from app.domain.universe import UniversalInstrument
from app.execution.gate import RiskEnforcedExecutionGate
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.intelligence.confidence import (
    CONFIDENCE_MODEL_V2_B,
    CONFIDENCE_SEMANTICS_V2,
    V2_B_THRESHOLD,
    V2_B_THRESHOLD_PROVENANCE,
)
from app.news.alpaca import AlpacaNewsProvider
from app.news.alpha_vantage import AlphaVantageNewsProvider
from app.news.crosscheck import CrossCheckedNewsProvider
from app.news.intelligence import (
    GlobalNewsIntelligenceEngine,
    NewsFeedProvider,
    NewsSourceQuality,
    RawNewsItem,
)
from app.orchestration.active_intelligence import (
    ActiveIntelligenceAuditStore,
    ActiveIntelligenceCycleRecord,
    AegisActiveIntelligenceOrchestrator,
    _news_asset_contexts,
    _news_event_digest,
    causal_completed_bars,
    default_active_intelligence_audit_store,
)
from app.orchestration.market_acquisition import (
    EtoroOneHourAcquisitionCoordinator,
    build_coherent_one_hour_snapshot,
)
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.scanner.active import (
    ActiveScannerBucket,
    ActiveMarketScanner,
    ActiveScannerResult,
    _expected_completed_one_hour_bar_timestamp,
    _is_market_closed_as_of,
)
from app.storage.sqlite import RunnerLease, SqliteRecordStore

DEFAULT_ETORO_FULL_CATALOG_SESSION_AUDIT_PATH = (
    Path("work") / "etoro-full-catalog-session-audit.json"
)
RUNNER_LEASE_DURATION = timedelta(minutes=15)
# The Demo authorization is an account-currency envelope. The configured
# single-entry ceiling is intentionally kept separate from the total envelope
# so a confirmed Demo cycle cannot exceed the explicitly authorized amount.
MIN_DEMO_AUTHORIZED_CAPITAL = Decimal("200")
DEMO_MAX_SINGLE_ORDER_AMOUNT = Decimal("98000")


class CausalActiveCycleProducer(Protocol):
    @property
    def last_scanner_result(self) -> ActiveScannerResult | None: ...

    def run_if_new_bar_cycle(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal = Decimal("200"),
        asset_classes: frozenset[AssetClass] | None = None,
        force_universe_reconciliation: bool = False,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ActiveIntelligenceCycleRecord | None: ...


def _fixed_clock(timestamp: datetime) -> Callable[[], datetime]:
    return lambda: timestamp


class MarketDataAcquisitionOutcome(StrEnum):
    UPDATED = "UPDATED"
    ALREADY_CURRENT = "ALREADY_CURRENT"
    MARKET_CLOSED_NO_NEW_BAR = "MARKET_CLOSED_NO_NEW_BAR"
    NO_DATA = "NO_DATA"
    HTTP_ERROR = "HTTP_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PARSE_ERROR = "PARSE_ERROR"
    CAUSALITY_REJECTED = "CAUSALITY_REJECTED"
    INCOMPLETE_BAR_REJECTED = "INCOMPLETE_BAR_REJECTED"
    FUTURE_BAR_REJECTED = "FUTURE_BAR_REJECTED"
    OTHER_ERROR = "OTHER_ERROR"


def _classify_etoro_read_error(exc: EtoroApiError) -> MarketDataAcquisitionOutcome:
    if exc.status in {401, 403}:
        return MarketDataAcquisitionOutcome.AUTH_ERROR
    if exc.status == 429:
        return MarketDataAcquisitionOutcome.RATE_LIMITED
    if exc.status is not None:
        return MarketDataAcquisitionOutcome.HTTP_ERROR
    if exc.transport_detail == "TIMEOUT":
        return MarketDataAcquisitionOutcome.TIMEOUT
    return MarketDataAcquisitionOutcome.OTHER_ERROR


def _valid_candle_envelope(raw: object) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("candles"), list)


def _fetch_etoro_one_hour_bars(
    client: EtoroReadClient,
    instrument: UniversalInstrument,
    as_of: datetime,
) -> dict[str, object]:
    instrument_id = instrument.numeric_instrument_id
    if instrument_id is None:
        return {
            "instrument": instrument,
            "outcome": MarketDataAcquisitionOutcome.OTHER_ERROR,
            "error": "NUMERIC_INSTRUMENT_ID_REQUIRED",
            "bars": (),
        }
    try:
        raw = client.candle_history(
            instrument_id=instrument_id,
            direction="asc",
            interval="OneHour",
            candles_count=61,
        )
    except EtoroApiError as exc:
        return {
            "instrument": instrument,
            "outcome": _classify_etoro_read_error(exc),
            "error": exc.safe_metadata(),
            "bars": (),
        }
    except TimeoutError:
        return {
            "instrument": instrument,
            "outcome": MarketDataAcquisitionOutcome.TIMEOUT,
            "error": "TIMEOUT",
            "bars": (),
        }
    except Exception as exc:
        return {
            "instrument": instrument,
            "outcome": MarketDataAcquisitionOutcome.OTHER_ERROR,
            "error": type(exc).__name__,
            "bars": (),
        }
    if not _valid_candle_envelope(raw):
        return {
            "instrument": instrument,
            "outcome": MarketDataAcquisitionOutcome.PARSE_ERROR,
            "error": "INVALID_CANDLE_ENVELOPE",
            "bars": (),
        }
    normalized = _normalize_etoro_candles(
        raw,
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
    )
    if not normalized:
        return {
            "instrument": instrument,
            "outcome": MarketDataAcquisitionOutcome.NO_DATA,
            "error": None,
            "bars": (),
        }
    future = tuple(bar for bar in normalized if bar.timestamp > as_of)
    visible = tuple(bar for bar in normalized if bar.timestamp <= as_of)
    completed = causal_completed_bars(
        bars_by_symbol=visible,
        instrument=instrument,
        as_of=as_of,
        timeframe=TimeFrame.ONE_HOUR,
    )
    if not completed:
        if future and not visible:
            outcome = MarketDataAcquisitionOutcome.FUTURE_BAR_REJECTED
        elif visible:
            outcome = MarketDataAcquisitionOutcome.INCOMPLETE_BAR_REJECTED
        else:
            outcome = MarketDataAcquisitionOutcome.CAUSALITY_REJECTED
        return {
            "instrument": instrument,
            "outcome": outcome,
            "error": None,
            "bars": (),
        }
    return {
        "instrument": instrument,
        "outcome": None,
        "error": None,
        "bars": completed,
        "future_rejected": len(future),
        "incomplete_rejected": len(visible) - len(completed),
    }


def _refresh_active_one_hour_bars(
    *,
    client: EtoroReadClient,
    cache: object,
    instruments: tuple[UniversalInstrument, ...],
    as_of: datetime,
) -> tuple[dict[str, tuple[MarketBar, ...]], dict[str, object]]:
    """Refresh completed eToro 1H bars before the causal A4C gate."""
    if not isinstance(cache, HistoricalDataCache):
        raise TypeError("active market-data acquisition requires HistoricalDataCache")
    ordered = tuple(sorted(instruments, key=lambda item: (item.symbol, item.key)))
    outcomes: dict[str, dict[str, object]] = {}
    pending: list[UniversalInstrument] = []
    newest: datetime | None = None
    for instrument in ordered:
        latest = cache.last_timestamp(
            provider=instrument.broker,
            broker=instrument.broker,
            broker_instrument_id=instrument.broker_instrument_id,
            timeframe=TimeFrame.ONE_HOUR,
        )
        market_closed = instrument.asset_class in {
            AssetClass.EQUITY,
            AssetClass.ETF,
        } and _is_market_closed_as_of(as_of)
        if market_closed and latest is not None:
            outcomes[instrument.key] = {
                "instrument": instrument,
                "outcome": MarketDataAcquisitionOutcome.MARKET_CLOSED_NO_NEW_BAR,
                "error": None,
                "bars": (),
            }
            newest = latest if newest is None else max(newest, latest)
            continue
        if not market_closed and latest is not None:
            expected = _expected_completed_one_hour_bar_timestamp(as_of)
            if latest >= expected:
                outcomes[instrument.key] = {
                    "instrument": instrument,
                    "outcome": MarketDataAcquisitionOutcome.ALREADY_CURRENT,
                    "error": None,
                    "bars": (),
                }
                newest = latest if newest is None else max(newest, latest)
                continue
        pending.append(instrument)

    if pending:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
            fetched_rows = executor.map(
                lambda item: _fetch_etoro_one_hour_bars(client, item, as_of), pending
            )
            for row in fetched_rows:
                fetched_instrument = row["instrument"]
                assert isinstance(fetched_instrument, UniversalInstrument)
                outcomes[fetched_instrument.key] = row

    rows: list[dict[str, object]] = []
    for instrument in ordered:
        row = outcomes[instrument.key]
        completed = row.get("bars", ())
        outcome = row.get("outcome")
        if isinstance(completed, tuple) and completed:
            latest_before = cache.last_timestamp(
                provider=instrument.broker,
                broker=instrument.broker,
                broker_instrument_id=instrument.broker_instrument_id,
                timeframe=TimeFrame.ONE_HOUR,
            )
            stats = cache.upsert_bars_with_stats(
                provider=instrument.broker,
                bars=completed,
                fetched_at=as_of,
                mapping=ProviderInstrumentReference(
                    provider=instrument.broker,
                    provider_symbol=instrument.symbol,
                    broker=instrument.broker,
                    broker_symbol=instrument.symbol,
                    broker_instrument_id=instrument.broker_instrument_id,
                    exchange=instrument.exchange,
                    asset_class=instrument.asset_class,
                    currency=instrument.currency,
                    mapping_confidence=Decimal("1"),
                    mapping_source="etoro-native-instrument-id",
                    verified=True,
                ),
            )
            latest = max(bar.timestamp for bar in completed)
            newest = latest if newest is None else max(newest, latest)
            outcome = (
                MarketDataAcquisitionOutcome.UPDATED
                if latest_before is None or latest > latest_before or int(str(stats["updated"])) > 0
                else MarketDataAcquisitionOutcome.ALREADY_CURRENT
            )
        assert isinstance(outcome, MarketDataAcquisitionOutcome)
        rows.append(
            {
                "instrument_id": instrument.broker_instrument_id,
                "symbol": instrument.symbol,
                "asset_class": instrument.asset_class.value,
                "outcome": outcome.value,
                "error": row.get("error"),
                "future_bars_rejected": int(str(row.get("future_rejected", 0))),
                "incomplete_bars_rejected": int(str(row.get("incomplete_rejected", 0))),
            }
        )

    bars_by_symbol = {
        instrument.symbol: cache.get_bars(
            provider=instrument.broker,
            instrument_key=(instrument.broker, instrument.broker_instrument_id),
            timeframe=TimeFrame.ONE_HOUR,
            as_of=as_of,
            limit=60,
            instrument_factory=instrument.model_dump(mode="json"),
        )
        for instrument in instruments
    }
    counts = Counter(str(row["outcome"]) for row in rows)
    successful = {
        MarketDataAcquisitionOutcome.UPDATED.value,
        MarketDataAcquisitionOutcome.ALREADY_CURRENT.value,
        MarketDataAcquisitionOutcome.MARKET_CLOSED_NO_NEW_BAR.value,
    }
    usable_count = sum(counts.get(item, 0) for item in successful)
    status = "SUCCESS" if usable_count == len(rows) else "PARTIAL"
    if usable_count == 0:
        status = "PROVIDER_UNAVAILABLE"
    return bars_by_symbol, {
        "acquisition_attempted": True,
        "acquisition_provider": "etoro",
        "acquisition_instruments_requested": len(instruments),
        "acquisition_instruments_updated": counts.get("UPDATED", 0),
        "acquisition_newest_completed_bar": None if newest is None else newest.isoformat(),
        "acquisition_stale_count": len(rows) - usable_count,
        "acquisition_missing_count": counts.get("NO_DATA", 0),
        "acquisition_status": status,
        "acquisition_error": tuple(
            f"{row['symbol']}:{row['outcome']}" for row in rows if row["outcome"] not in successful
        ),
        "acquisition_outcome_counts": {
            outcome.value: counts.get(outcome.value, 0) for outcome in MarketDataAcquisitionOutcome
        },
        "acquisition_results": tuple(rows),
        "acquisition_cycle_eligible": usable_count > 0,
    }


type DemoSubmissionPackageProvider = Callable[
    [ActiveIntelligenceCycleRecord, ActiveScannerResult],
    Mapping[str, EtoroDemoSubmissionPackage],
]

ETORO_DEMO_RUNTIME_STATUS_KIND = "etoro-demo-runtime-status"
DEFAULT_ETORO_DEMO_RUNTIME_STORE_PATH = Path("work") / "etoro-demo-runtime.sqlite3"


class AegisEtoroAutomaticDemoRuntime:
    """Single runtime owner that may consume accepted A4C cycles for Demo only."""

    def __init__(
        self,
        *,
        config: ApplicationConfig,
        values: Mapping[str, str],
        orchestrator: CausalActiveCycleProducer,
        registry: SqliteRecordStore,
        gateway: DemoSubmissionGateway | None,
        package_provider: DemoSubmissionPackageProvider | None = None,
    ) -> None:
        self._config = config
        self._values = values
        self._orchestrator = orchestrator
        self._registry = registry
        self._gateway = gateway
        self._package_provider = package_provider

    def run_once(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal = Decimal("200"),
    ) -> dict[str, object]:
        run_cycle = self._orchestrator.run_if_new_bar_cycle
        run_cycle_parameters = inspect.signature(run_cycle).parameters
        if "asset_classes" not in run_cycle_parameters:
            # Compatibility for small test doubles and older integrations.
            cycle = run_cycle(
                scheduled_at=scheduled_at, instruments=instruments,
                bars_by_symbol=bars_by_symbol, portfolio=portfolio,
                timeframe=timeframe, shadow_capital=shadow_capital,
            )
        else:
            # The active Demo decision is a global comparison, not a race
            # between asset-class lanes.  The old implementation tried
            # Crypto first and stopped at the first fresh Crypto bar; that
            # made an otherwise global universe look like an eight-symbol
            # Crypto scanner and biased the ranking toward whichever lane
            # happened to update first.  A global call still triggers on any
            # causally new bar, while the scanner receives every instrument
            # that is coherent in the current snapshot.  Execution remains
            # fail-closed through the coverage, news, RiskManager and
            # preflight gates below.
            global_cycle_kwargs: dict[str, object] = {"asset_classes": None}
            if "force_universe_reconciliation" in run_cycle_parameters:
                global_cycle_kwargs["force_universe_reconciliation"] = True
            cycle = run_cycle(
                scheduled_at=scheduled_at, instruments=instruments,
                bars_by_symbol=bars_by_symbol, portfolio=portfolio,
                timeframe=timeframe, shadow_capital=shadow_capital,
                **global_cycle_kwargs,
            )
        if cycle is None:
            return _demo_runtime_payload(
                status="NO_CYCLE",
                pilot_enabled=self._config.etoro_demo_automatic_pilot_enabled,
            )
        scanner_result = self._orchestrator.last_scanner_result
        if scanner_result is None:
            return _demo_runtime_payload(
                status="BLOCKED",
                pilot_enabled=self._config.etoro_demo_automatic_pilot_enabled,
                blockers=("ACCEPTED_CYCLE_WITHOUT_SCANNER_RESULT",),
                cycle_id=cycle.cycle_id,
            )
        # Some persisted/reloaded scanner payloads carry the canonical
        # candidate list but omit the derived bucket collections. Rebuild
        # those collections from the authoritative candidate bucket so a
        # valid WATCHLIST candidate cannot disappear before the Demo pilot.
        if scanner_result.candidates and (
            not scanner_result.top_opportunities
            and not scanner_result.watchlist
            and not scanner_result.no_trade
            and not scanner_result.rejected
        ):
            scanner_result = scanner_result.model_copy(
                update={
                    "top_opportunities": tuple(
                        candidate
                        for candidate in scanner_result.candidates
                        if candidate.bucket is ActiveScannerBucket.TOP_OPPORTUNITIES
                    ),
                    "watchlist": tuple(
                        candidate
                        for candidate in scanner_result.candidates
                        if candidate.bucket is ActiveScannerBucket.WATCHLIST
                    ),
                    "no_trade": tuple(
                        candidate
                        for candidate in scanner_result.candidates
                        if candidate.bucket is ActiveScannerBucket.NO_TRADE
                    ),
                    "rejected": tuple(
                        candidate
                        for candidate in scanner_result.candidates
                        if candidate.bucket is ActiveScannerBucket.REJECTED
                    ),
                }
            )
        if not scanner_result.top_opportunities and not scanner_result.watchlist:
            return _demo_runtime_payload(
                status="NO_TOP_OPPORTUNITY",
                pilot_enabled=self._config.etoro_demo_automatic_pilot_enabled,
                cycle_id=cycle.cycle_id,
                **_cycle_news_payload(cycle),
            )
        if not self._config.etoro_demo_automatic_pilot_enabled:
            return _demo_runtime_payload(
                status="DEMO_PILOT_DISABLED",
                pilot_enabled=False,
                cycle_id=cycle.cycle_id,
                top_opportunity_count=len(scanner_result.top_opportunities),
                eligible_count=len(scanner_result.top_opportunities),
                **_cycle_news_payload(cycle),
            )
        if self._config.broker_execution_mode is not BrokerExecutionMode.DEMO_EXECUTION:
            return _demo_runtime_payload(
                status="EXECUTION_MODE_READ_ONLY",
                pilot_enabled=True,
                blockers=("DEMO_EXECUTION_MODE_REQUIRED",),
                cycle_id=cycle.cycle_id,
                top_opportunity_count=len(scanner_result.top_opportunities),
                eligible_count=len(scanner_result.top_opportunities),
                **_cycle_news_payload(cycle),
            )
        if self._config.execution_policy is not ExecutionPolicy.AUTONOMOUS:
            return _demo_runtime_payload(
                status="EXECUTION_POLICY_NOT_AUTONOMOUS",
                pilot_enabled=True,
                blockers=("AUTONOMOUS_EXECUTION_POLICY_REQUIRED",),
                cycle_id=cycle.cycle_id,
                top_opportunity_count=len(scanner_result.top_opportunities),
                eligible_count=len(scanner_result.top_opportunities),
                **_cycle_news_payload(cycle),
            )
        if self._config.kill_switch:
            return _demo_runtime_payload(
                status="KILL_SWITCH_ACTIVE",
                pilot_enabled=True,
                blockers=("KILL_SWITCH_ACTIVE",),
                cycle_id=cycle.cycle_id,
                top_opportunity_count=len(scanner_result.top_opportunities),
                eligible_count=len(scanner_result.top_opportunities),
                **_cycle_news_payload(cycle),
            )
        # A Demo order must not be authorised from a misleadingly narrow
        # "global" ranking.  Recent cycles were dominated by ASX symbols
        # because that exchange happened to have the freshest 1H bars, while
        # the news leg contained no fresh events.  Keep recording those cycles
        # for diagnostics, but stop them before broker packaging until the
        # comparison is genuinely global and news-backed.
        if self._package_provider is not None and cycle.news_provider_status not in {
            None,
            "PROVIDER_UNAVAILABLE",
            "unknown",
        }:
            coverage_blockers = _global_demo_selection_blockers(
                cycle=cycle,
                scanner_result=scanner_result,
                instruments=instruments,
            )
            if coverage_blockers:
                return _demo_runtime_payload(
                    status="INSUFFICIENT_GLOBAL_SELECTION_COVERAGE",
                    pilot_enabled=True,
                    cycle_id=cycle.cycle_id,
                    top_opportunity_count=len(scanner_result.top_opportunities),
                    eligible_count=0,
                    blockers=coverage_blockers,
                    **_cycle_news_payload(cycle),
                )
        settings = demo_pilot_settings(self._values)
        pilot = EtoroAutomaticDemoPilot(
            environment=self._config.operating_mode,
            settings=settings,
            registry=self._registry,
            gateway=self._gateway,
        )
        packages = (
            {}
            if self._package_provider is None
            else dict(self._package_provider(cycle, scanner_result))
        )
        result = pilot.run(
            cycle=cycle,
            scanner_result=scanner_result,
            broker_instrument_ids=_verified_broker_ids(instruments),
            submission_packages=packages,
            allow_exploratory_watchlist=True,
        )
        return _demo_runtime_payload(
            status=result.status.value,
            pilot_enabled=True,
            cycle_id=cycle.cycle_id,
            top_opportunity_count=len(scanner_result.top_opportunities),
            eligible_count=len(result.eligible_intents),
            submitted_count=sum(1 for item in result.submissions if item.submitted),
            blockers=tuple(item.value for item in result.blockers),
            demo_broker_write_calls=result.demo_broker_write_calls,
            broker_write_calls_real=result.broker_write_calls_real,
            risk_manager_reached=any(item.risk_manager_reached for item in result.submissions),
            execution_admission_gate_reached=any(
                item.execution_admission_gate_reached for item in result.submissions
            ),
            pilot_result=result,
            news_provider=cycle.news_provider,
            news_provider_status=cycle.news_provider_status,
            news_scan_cutoff_timestamp=cycle.news_cutoff_timestamp,
            news_scan_completed_at=cycle.news_scan_completed_at,
            news_events_received=cycle.news_events_received,
            news_events_fresh=cycle.news_events_fresh,
            news_events_material=cycle.news_events_material,
            news_duplicates_ignored=cycle.news_duplicates_ignored,
            news_acquisition_error_code=cycle.news_acquisition_error_code,
            news_acquisition_error_detail_safe=cycle.news_acquisition_error_detail_safe,
            news_provider_diagnostics=cycle.news_provider_diagnostics,
        )


class EtoroDemoContinuousRunner:
    """Poll the validated one-shot Demo runtime without owning scan logic."""

    def __init__(
        self,
        *,
        run_once: Callable[[], Mapping[str, object]],
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        poll_interval_seconds: float = 60.0,
        error_backoff_seconds: float = 60.0,
        max_backoff_seconds: float = 300.0,
        status_store: SqliteRecordStore | None = None,
    ) -> None:
        if poll_interval_seconds <= 0 or error_backoff_seconds <= 0 or max_backoff_seconds <= 0:
            raise ValueError("runner intervals must be positive")
        self._run_once = run_once
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or sleep
        self._poll_interval = poll_interval_seconds
        self._error_backoff = error_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._status_store = status_store

    def run(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_iterations: int | None = None,
    ) -> dict[str, object]:
        if max_iterations is not None and max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        lease: RunnerLease | None = None
        if self._status_store is not None:
            acquired_at = self._clock()
            lease = self._status_store.acquire_runner_lease(
                owner_token=str(uuid4()),
                acquired_at=acquired_at,
                expires_at=acquired_at + RUNNER_LEASE_DURATION,
            )
            if lease is None:
                return {
                    "runner_state": "ALREADY_RUNNING",
                    "poll_count": 0,
                    "accepted_cycle_count": 0,
                    "last_poll_timestamp": None,
                    "last_accepted_cycle_timestamp": None,
                    "last_cycle_result": None,
                    "top_opportunity_count": 0,
                    "eligible_demo_candidates": 0,
                    "last_demo_submission_status": None,
                    "demo_broker_write_calls": 0,
                    "broker_write_calls_real": 0,
                    "last_error": "RUNNER_LEASE_HELD",
                }
        should_stop = stop_requested or (lambda: False)
        if lease is not None:
            requested_stop = should_stop

            def stop_requested_by_store() -> bool:
                return requested_stop() or self._status_store.runner_stop_requested()  # type: ignore[union-attr]

            should_stop = stop_requested_by_store
        state = "RUNNING"
        poll_count = 0
        accepted_count = 0
        demo_writes = 0
        real_writes = 0
        last_poll_at: datetime | None = None
        last_accepted_at: str | None = None
        last_result: Mapping[str, object] | None = None
        last_error: str | None = None
        heartbeat_stop = Event()
        heartbeat_thread: Thread | None = None
        if lease is not None and self._status_store is not None:
            heartbeat_store_path = self._status_store.path

            def keep_lease_alive() -> None:
                # sqlite3 connections are thread-affine by default.  Open the
                # heartbeat connection inside the heartbeat thread instead of
                # constructing it in the runner thread and then reusing it
                # here.  The runner and heartbeat still coordinate through the
                # same file and SQLite busy_timeout handles short write-lock
                # contention.
                heartbeat_store = SqliteRecordStore(heartbeat_store_path)
                try:
                    interval = min(30.0, max(5.0, RUNNER_LEASE_DURATION.total_seconds() / 3))
                    while not heartbeat_stop.wait(interval):
                        heartbeat_at = datetime.now(UTC)
                        try:
                            heartbeat_store.heartbeat_runner_lease(
                                lease=lease,
                                heartbeat_at=heartbeat_at,
                                expires_at=heartbeat_at + RUNNER_LEASE_DURATION,
                            )
                        except sqlite3.OperationalError:
                            # A long acquisition may briefly hold the shared
                            # SQLite write lock. Retry on the next heartbeat.
                            continue
                finally:
                    heartbeat_store.close()

            heartbeat_thread = Thread(
                target=keep_lease_alive,
                name="aegis-runner-lease-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()

        while not should_stop() and (max_iterations is None or poll_count < max_iterations):
            last_poll_at = self._clock()
            poll_count += 1
            if lease is not None:
                heartbeat_ok = self._status_store.heartbeat_runner_lease(  # type: ignore[union-attr]
                    lease=lease,
                    heartbeat_at=last_poll_at,
                    expires_at=last_poll_at + RUNNER_LEASE_DURATION,
                )
                if not heartbeat_ok:
                    state = "LEASE_LOST"
                    last_error = "RUNNER_LEASE_LOST"
                    self._persist_status(
                        state=state,
                        observed_at=last_poll_at,
                        poll_count=poll_count,
                        last_result=last_result,
                        last_accepted_at=last_accepted_at,
                        demo_writes=demo_writes,
                        real_writes=real_writes,
                        last_error=last_error,
                    )
                    break
            self._persist_status(
                state="RUNNING",
                observed_at=last_poll_at,
                poll_count=poll_count,
                last_result=last_result,
                last_accepted_at=last_accepted_at,
                demo_writes=demo_writes,
                real_writes=real_writes,
            )
            try:
                result = dict(self._run_once())
            except KeyboardInterrupt:
                state = "STOPPED"
                break
            except Exception as exc:  # pragma: no cover - exact branch is asserted via output
                state = "ERROR_BACKOFF"
                last_error = type(exc).__name__
                self._persist_status(
                    state=state,
                    observed_at=last_poll_at,
                    poll_count=poll_count,
                    last_result=last_result,
                    last_accepted_at=last_accepted_at,
                    demo_writes=demo_writes,
                    real_writes=real_writes,
                    last_error=last_error,
                )
                if not should_stop() and (max_iterations is None or poll_count < max_iterations):
                    self._sleep(min(self._error_backoff, self._max_backoff))
                continue

            last_result = result
            if result.get("cycle_id") is not None:
                accepted_count += 1
                candidate_timestamp = result.get("cycle_as_of_resolved")
                if isinstance(candidate_timestamp, str):
                    last_accepted_at = candidate_timestamp
            demo_writes += _nonnegative_int(result.get("demo_broker_write_calls"))
            real_writes += _nonnegative_int(result.get("broker_write_calls_real"))
            state = "RUNNING"
            self._persist_status(
                state=state,
                observed_at=last_poll_at,
                poll_count=poll_count,
                last_result=result,
                last_accepted_at=last_accepted_at,
                demo_writes=demo_writes,
                real_writes=real_writes,
            )
            if not should_stop() and (max_iterations is None or poll_count < max_iterations):
                try:
                    self._sleep(self._poll_interval)
                except KeyboardInterrupt:
                    state = "STOPPED"
                    break

        if state != "STOPPED":
            state = "STOPPED"
        self._persist_status(
            state=state,
            observed_at=last_poll_at or self._clock(),
            poll_count=poll_count,
            last_result=last_result,
            last_accepted_at=last_accepted_at,
            demo_writes=demo_writes,
            real_writes=real_writes,
            last_error=last_error,
        )
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2.0)
        if lease is not None:
            self._status_store.release_runner_lease(  # type: ignore[union-attr]
                lease=lease,
                released_at=last_poll_at or self._clock(),
            )
        return {
            "runner_state": state,
            "poll_count": poll_count,
            "accepted_cycle_count": accepted_count,
            "last_poll_timestamp": None if last_poll_at is None else last_poll_at.isoformat(),
            "last_accepted_cycle_timestamp": last_accepted_at,
            "last_cycle_result": last_result,
            "top_opportunity_count": (
                0
                if last_result is None
                else _nonnegative_int(last_result.get("top_opportunity_count"))
            ),
            "eligible_demo_candidates": (
                0 if last_result is None else _nonnegative_int(last_result.get("eligible_count"))
            ),
            "last_demo_submission_status": (
                None if last_result is None else last_result.get("status")
            ),
            "demo_broker_write_calls": demo_writes,
            "broker_write_calls_real": real_writes,
            "last_error": last_error,
        }

    def _persist_status(
        self,
        *,
        state: str,
        observed_at: datetime,
        poll_count: int,
        last_result: Mapping[str, object] | None,
        last_accepted_at: str | None,
        demo_writes: int,
        real_writes: int,
        last_error: str | None = None,
    ) -> None:
        if self._status_store is None:
            return
        result = last_result or {}
        cycle_state = result.get("status")
        activity = (
            "WAITING_FOR_ELIGIBLE_COMPLETED_BAR" if cycle_state == "NO_CYCLE" else cycle_state
        )
        universe = result.get("universe")
        universe_status = universe if isinstance(universe, Mapping) else {}
        if not universe_status or any(
            key not in universe_status
            for key in (
                "catalog_pending_count",
                "catalog_not_ready_count",
            )
        ):
            # Keep catalog coverage visible even while the first live cycle
            # is still acquiring bars and has not produced a result payload.
            try:
                from app.data.runtime import read_etoro_dynamic_universe_artifact

                artifact = read_etoro_dynamic_universe_artifact()
                if isinstance(artifact, Mapping):
                    universe_status = {**artifact, **universe_status}
            except (OSError, ValueError, TypeError):
                if not universe_status:
                    universe_status = {}
        self._status_store.append(
            ETORO_DEMO_RUNTIME_STATUS_KIND,
            {
                "observed_at": observed_at.isoformat(),
                "runner_state": state,
                "cycle_state": cycle_state,
                "last_successful_scan_at": last_accepted_at,
                "top_opportunity_count": _nonnegative_int(result.get("top_opportunity_count")),
                "eligible_demo_candidates": _nonnegative_int(result.get("eligible_count")),
                "automatic_pilot_armed": bool(result.get("pilot_enabled", False)),
                "execution_enabled": bool(result.get("execution_enabled", False)),
                "last_submission_status": result.get("status"),
                "demo_broker_write_calls": demo_writes,
                "execution_available": bool(result.get("execution_available", False)),
                "broker_write_calls_real": real_writes,
                "activity_code": activity,
                "last_error": last_error,
                "poll_count": poll_count,
                "pilot_notional_eur": result.get("pilot_notional_eur"),
                "authorized_capital_eur": result.get("authorized_capital_eur"),
                "managed_exposure_eur": result.get("managed_exposure_eur"),
                "remaining_authorized_capital_eur": result.get("remaining_authorized_capital_eur"),
                "sizing_mode": result.get("sizing_mode"),
                "active_scanner_universe_count": result.get("active_scanner_universe_count"),
                "validated_baseline_count": result.get("validated_baseline_count"),
                "catalog_instrument_count": universe_status.get("catalog_instrument_count"),
                "catalog_verified_mapping_count": universe_status.get("verified_mapping_count"),
                "catalog_market_data_ready_count": universe_status.get("market_data_ready_count"),
                "catalog_blocked_count": universe_status.get("blocked_count"),
                "catalog_pending_count": universe_status.get("catalog_pending_count"),
                "catalog_not_ready_count": universe_status.get("catalog_not_ready_count"),
                "news_provider": result.get("news_provider"),
                "news_provider_status": result.get("news_provider_status"),
                "news_scan_cutoff_timestamp": result.get("news_scan_cutoff_timestamp"),
                "news_scan_completed_at": result.get("news_scan_completed_at"),
                "news_events_received": result.get("news_events_received"),
                "news_events_fresh": result.get("news_events_fresh"),
                "news_events_material": result.get("news_events_material"),
                "news_duplicates_ignored": result.get("news_duplicates_ignored"),
                "news_acquisition_error_code": result.get("news_acquisition_error_code"),
                "news_acquisition_error_detail_safe": result.get(
                    "news_acquisition_error_detail_safe"
                ),
                "news_provider_diagnostics": result.get("news_provider_diagnostics", {}),
                "news_provider_request_count": result.get("news_provider_request_count", 0),
                "news_event_digest": result.get("news_event_digest", ()),
                "news_asset_contexts": result.get("news_asset_contexts", {}),
                "global_risk_context": result.get("global_risk_context", {}),
                "acquisition_attempted": result.get("acquisition_attempted", False),
                "acquisition_provider": result.get("acquisition_provider"),
                "acquisition_instruments_requested": result.get(
                    "acquisition_instruments_requested", 0
                ),
                "acquisition_instruments_updated": result.get("acquisition_instruments_updated", 0),
                "acquisition_instruments_attempted": result.get(
                    "acquisition_instruments_attempted", 0
                ),
                "acquisition_instruments_not_attempted": result.get(
                    "acquisition_instruments_not_attempted", 0
                ),
                "acquisition_instruments_in_backoff": result.get(
                    "acquisition_instruments_in_backoff", 0
                ),
                "acquisition_newest_completed_bar": result.get("acquisition_newest_completed_bar"),
                "acquisition_stale_count": result.get("acquisition_stale_count", 0),
                "acquisition_missing_count": result.get("acquisition_missing_count", 0),
                "acquisition_status": result.get("acquisition_status"),
                "acquisition_error": result.get("acquisition_error", ()),
                "acquisition_outcome_counts": result.get("acquisition_outcome_counts", {}),
                "coherent_eligible_count": result.get("eligible_for_target_bar"),
                "coherent_coverage_ratio": result.get("coverage_ratio"),
                "coherent_coverage_minimum": result.get("minimum_coverage_ratio"),
                "a4c_reason": result.get("a4c_reason", result.get("blockers")),
                "blockers": result.get("blockers", ()),
                "package_material_diagnostics": result.get(
                    "package_material_diagnostics", {}
                ),
                "pilot_result": result.get("pilot_result"),
                "scanner_reached": result.get("scanner_reached", False),
                "news_reached": result.get("news_reached", False),
                "risk_manager_reached": result.get("risk_manager_reached", False),
                "execution_admission_gate_reached": result.get(
                    "execution_admission_gate_reached", False
                ),
            },
        )


def read_etoro_demo_runtime_status(
    path: Path = DEFAULT_ETORO_DEMO_RUNTIME_STORE_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Read the runner's latest status without creating or changing its store."""
    status = SqliteRecordStore.read_latest_read_only(path, ETORO_DEMO_RUNTIME_STATUS_KIND)
    if status is None:
        return None
    lease = SqliteRecordStore.read_runner_lease_read_only(path)
    control = SqliteRecordStore.read_runner_control_read_only(path)
    reference = now or datetime.now(UTC)
    persisted_state = status.get("runner_state")
    status["persisted_runner_state"] = persisted_state
    status["runner_lease"] = lease
    status["runner_control"] = control
    if persisted_state == "RUNNING":
        if lease is None or lease.get("status") != "ACTIVE":
            status["runner_state"] = "STALE"
        else:
            expiry = datetime.fromisoformat(str(lease["expires_at"]))
            if expiry <= reference:
                status["runner_state"] = "LEASE_EXPIRED"
                effective_lease = dict(lease)
                effective_lease["status"] = "EXPIRED"
                effective_lease["owner_present"] = False
                status["runner_lease"] = effective_lease
            elif bool(control["stop_requested"]):
                status["runner_state"] = "STOP_REQUESTED"
    return status


def request_etoro_demo_runtime_stop(
    *,
    path: Path = DEFAULT_ETORO_DEMO_RUNTIME_STORE_PATH,
    requested_at: datetime | None = None,
) -> dict[str, object]:
    """Request a graceful stop without interrupting or killing the runner."""
    store = SqliteRecordStore(path)
    previous = read_etoro_demo_runtime_status(path)
    effective_requested_at = requested_at or datetime.now(UTC)
    result = store.request_runner_stop(requested_at=effective_requested_at)
    if (
        result == "STOPPED"
        and previous is not None
        and previous.get("runner_state")
        in {
            "RUNNING",
            "LEASE_EXPIRED",
            "STALE",
        }
    ):
        stopped = dict(previous)
        stopped.update(
            {
                "runner_state": "STOPPED",
                "persisted_runner_state": "STOPPED",
                "observed_at": effective_requested_at.isoformat(),
                "activity_code": "RUNNER_STOPPED",
                "last_error": None,
            }
        )
        store.append(ETORO_DEMO_RUNTIME_STATUS_KIND, stopped)
        previous = stopped
    return {
        "status": "STOP_REQUESTED",
        "previous_runner_state": None if previous is None else previous.get("runner_state"),
        "runner_presence": result,
        "broker_write_calls": 0,
        "broker_write_calls_real": 0,
    }


def etoro_demo_runtime_status(
    *,
    path: Path = DEFAULT_ETORO_DEMO_RUNTIME_STORE_PATH,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return persisted runner status without starting runtime work."""
    status = read_etoro_demo_runtime_status(path, now=now)
    if status is None:
        return {
            "status": "ABSENT",
            "runner_state": None,
            "broker_write_calls": 0,
            "broker_write_calls_real": 0,
        }
    return {
        "status": "OK",
        "runner_state": status.get("runner_state"),
        "persisted_runner_state": status.get("persisted_runner_state"),
        "runner_lease": status.get("runner_lease"),
        "runner_control": status.get("runner_control"),
        "observed_at": status.get("observed_at"),
        "cycle_state": status.get("cycle_state"),
        "poll_count": status.get("poll_count"),
        "broker_write_calls": 0,
        "broker_write_calls_real": status.get("broker_write_calls_real", 0),
    }


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


class _EtoroReadback:
    def __init__(self, client: EtoroReadClient) -> None:
        self._client = client
        self._identity: BrokerIdentity | None = None

    def demo_order_state(self, instrument_id: int, order_id: str) -> ExecutionState:
        if self._identity is None:
            self._identity = self._client.identity()
        return self._client.demo_order_state(self._identity, instrument_id, order_id)


def _verified_broker_ids(instruments: tuple[UniversalInstrument, ...]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for instrument in instruments:
        instrument_id = instrument.numeric_instrument_id
        if instrument_id is not None:
            ids[instrument.symbol] = instrument_id
    return ids


def _demo_runtime_payload(
    *,
    status: str,
    pilot_enabled: bool,
    cycle_id: str | None = None,
    top_opportunity_count: int = 0,
    eligible_count: int = 0,
    submitted_count: int = 0,
    blockers: tuple[str, ...] = (),
    demo_broker_write_calls: int = 0,
    broker_write_calls_real: int = 0,
    risk_manager_reached: bool = False,
    execution_admission_gate_reached: bool = False,
    pilot_result: EtoroDemoPilotResult | None = None,
    authorized_capital_eur: Decimal | None = None,
    news_provider: str | None = None,
    news_provider_status: str | None = None,
    news_scan_cutoff_timestamp: datetime | None = None,
    news_scan_completed_at: datetime | None = None,
    news_events_received: int = 0,
    news_events_fresh: int = 0,
    news_events_material: int = 0,
    news_duplicates_ignored: int = 0,
    news_acquisition_error_code: str | None = None,
    news_acquisition_error_detail_safe: str | None = None,
    news_provider_diagnostics: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "pilot_enabled": pilot_enabled,
        "cycle_id": cycle_id,
        "top_opportunity_count": top_opportunity_count,
        "eligible_count": eligible_count,
        "submitted_count": submitted_count,
        "blockers": blockers,
        "demo_broker_write_calls": demo_broker_write_calls,
        "broker_write_calls_real": broker_write_calls_real,
        "risk_manager_reached": risk_manager_reached,
        "execution_admission_gate_reached": execution_admission_gate_reached,
        "write_request_sent_to_real": False,
        "pilot_result": None if pilot_result is None else pilot_result.model_dump(mode="json"),
        "authorized_capital_eur": (
            None if authorized_capital_eur is None else str(authorized_capital_eur)
        ),
        "sizing_mode": "RISK_MANAGER_AUTHORIZED_CAPITAL",
        "news_provider": news_provider,
        "news_provider_status": news_provider_status,
        "news_scan_cutoff_timestamp": (
            None if news_scan_cutoff_timestamp is None else news_scan_cutoff_timestamp.isoformat()
        ),
        "news_scan_completed_at": (
            None if news_scan_completed_at is None else news_scan_completed_at.isoformat()
        ),
        "news_events_received": news_events_received,
        "news_events_fresh": news_events_fresh,
        "news_events_material": news_events_material,
        "news_duplicates_ignored": news_duplicates_ignored,
        "news_acquisition_error_code": news_acquisition_error_code,
        "news_acquisition_error_detail_safe": news_acquisition_error_detail_safe,
        "news_provider_diagnostics": dict(news_provider_diagnostics or {}),
    }


def _cycle_news_payload(cycle: ActiveIntelligenceCycleRecord) -> dict[str, Any]:
    return {
        "news_provider": cycle.news_provider,
        "news_provider_status": cycle.news_provider_status,
        "news_scan_cutoff_timestamp": cycle.news_cutoff_timestamp,
        "news_scan_completed_at": cycle.news_scan_completed_at,
        "news_events_received": cycle.news_events_received,
        "news_events_fresh": cycle.news_events_fresh,
        "news_events_material": cycle.news_events_material,
        "news_duplicates_ignored": cycle.news_duplicates_ignored,
        "news_acquisition_error_code": cycle.news_acquisition_error_code,
        "news_acquisition_error_detail_safe": cycle.news_acquisition_error_detail_safe,
        "news_provider_diagnostics": cycle.news_provider_diagnostics,
    }


def _news_result_payload(news_result: object) -> dict[str, object]:
    """Project a completed read-only news probe into runtime status.

    News is observational and must remain visible even when the market-data
    coverage gate blocks the Aegis decision cycle. Keep this projection
    bounded and sanitized: provider adapters already redact credentials and
    the digest/context helpers never include raw provider payloads.
    """
    snapshot = getattr(news_result, "global_risk_snapshot", None)
    global_risk = (
        snapshot.model_dump(mode="json")
        if snapshot is not None and hasattr(snapshot, "model_dump")
        else {}
    )
    as_of = getattr(news_result, "as_of", None)
    normalized_events = getattr(news_result, "normalized_events", ())
    event_clusters = getattr(news_result, "event_clusters", ())
    return {
        "news_provider": getattr(news_result, "provider_name", None),
        "news_provider_status": getattr(
            getattr(news_result, "provider_status", None), "value", None
        ),
        "news_scan_cutoff_timestamp": as_of.isoformat() if as_of is not None else None,
        "news_scan_completed_at": as_of.isoformat() if as_of is not None else None,
        "news_events_received": len(normalized_events),
        "news_events_fresh": len(normalized_events),
        "news_events_material": sum(
            1
            for cluster in event_clusters
            if getattr(getattr(cluster, "canonical_event", None), "impact_score", 0)
            >= Decimal("0.70")
        ),
        "news_duplicates_ignored": max(0, len(normalized_events) - len(event_clusters)),
        "news_acquisition_error_code": getattr(news_result, "provider_error_code", None),
        "news_acquisition_error_detail_safe": getattr(
            news_result, "provider_error_detail_safe", None
        ),
        "news_provider_request_count": getattr(news_result, "provider_read_calls", 0),
        "news_provider_diagnostics": getattr(news_result, "provider_diagnostics", {}),
        "news_event_digest": _news_event_digest(news_result),
        "news_asset_contexts": _news_asset_contexts(news_result),
        "global_risk_context": global_risk,
    }


def _configured_runtime_block(
    config: ApplicationConfig,
    *,
    blocker: str,
    credentials_present: bool,
    pilot_enabled: bool | None = None,
) -> dict[str, object]:
    payload = _demo_runtime_payload(
        status="BLOCKED",
        pilot_enabled=(
            config.etoro_demo_automatic_pilot_enabled if pilot_enabled is None else pilot_enabled
        ),
        blockers=(blocker,),
        authorized_capital_eur=config.authorized_capital_eur,
    )
    payload.update(_runtime_execution_state(config, credentials_present=credentials_present))
    return payload


def build_etoro_demo_runtime_once_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str],
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Execute exactly one configured Demo runtime iteration from cached 1H data."""
    if config.operating_mode is not OperatingMode.ETORO_DEMO:
        return _configured_runtime_block(
            config,
            blocker="ETORO_DEMO_MODE_REQUIRED",
            credentials_present=False,
            pilot_enabled=False,
        )
    if not config.etoro_api_enabled:
        return _configured_runtime_block(
            config, blocker="ETORO_READ_API_NOT_ENABLED", credentials_present=False
        )
    credentials = runtime_credentials(values)
    if credentials is None:
        return _configured_runtime_block(
            config, blocker="ETORO_CREDENTIALS_NOT_CONFIGURED", credentials_present=False
        )
    if config.authorized_capital_eur is None:
        return _configured_runtime_block(
            config, blocker="AUTHORIZED_CAPITAL_NOT_CONFIGURED", credentials_present=True
        )
    if config.authorized_capital_eur < MIN_DEMO_AUTHORIZED_CAPITAL:
        return _configured_runtime_block(
            config,
            blocker="AUTHORIZED_CAPITAL_BELOW_MINIMUM_200",
            credentials_present=True,
        )
    minimum_coverage = config.scanner.active_cycle_minimum_coverage_ratio
    if minimum_coverage is None:
        return _configured_runtime_block(
            config,
            blocker="ACTIVE_CYCLE_MINIMUM_COVERAGE_RATIO_NOT_CONFIGURED",
            credentials_present=True,
        )

    from app.data.runtime import (
        DEFAULT_MARKET_DATA_CACHE_PATH,
        load_etoro_dynamic_active_scanner_instruments,
        read_etoro_dynamic_universe_artifact,
    )

    effective_clock = clock or (lambda: datetime.now(UTC))
    cycle_as_of = effective_clock()
    cache = HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    try:
        instruments = load_etoro_dynamic_active_scanner_instruments()
    except ValueError as exc:
        return _configured_runtime_block(config, blocker=str(exc), credentials_present=True)
    scheduled_at = cycle_as_of
    if not instruments:
        return _configured_runtime_block(
            config, blocker="NO_VALIDATED_1H_INSTRUMENTS", credentials_present=True
        )
    registry = SqliteRecordStore(Path("work") / "etoro-demo-runtime.sqlite3")
    from app.orchestration.session_state import partition_runtime_instruments

    instruments, excluded_internal = partition_runtime_instruments(instruments)
    registry.append("etoro-runtime-universe-admission", {
        "observed_at": scheduled_at.isoformat(),
        "source_count": len(instruments) + len(excluded_internal),
        "runtime_count": len(instruments),
        "excluded": [{"instrument_id": item.broker_instrument_id, "symbol": item.symbol,
                      "reason": "UNSUPPORTED_INTERNAL_INSTRUMENT"}
                     for item in excluded_internal],
    })
    http = DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
    client = EtoroReadClient(credentials, http)
    # Exit management is intentionally independent from the 1H entry cycle:
    # an open Demo position must still be protected while market coverage is
    # incomplete or the scanner has no new bar.  The manager is Demo-only and
    # keeps a successful/ambiguous close from being replayed automatically.
    try:
        demo_exit_report = manage_demo_exits(
            client=client,
            identity=client.identity(),
            credentials=credentials,
            http=http,
            registry=registry,
            observed_at=scheduled_at,
        )
    except EtoroApiError as exc:
        # Exit management must never take the whole read/scanner runner down.
        # The next poll retries the read, while the open lifecycle record
        # remains FILLED and therefore keeps its capital reservation.
        demo_exit_report = {
            "enabled": True,
            "evaluated": 0,
            "held": 0,
            "close_triggered": 0,
            "close_write_calls": 0,
            "closed_confirmed": 0,
            "pending_confirmation": 0,
            "blocked": 1,
            "errors": ("IDENTITY_READ_FAILED", exc.status or exc.category.value),
            "policy": "EXITPOLICY_V2_GUARDED",
        }
    registry.append(
        "etoro-demo-exit-management",
        {"observed_at": scheduled_at.isoformat(), **demo_exit_report},
    )
    from app.orchestration.session_state import enrich_instrument_session_state

    instruments = enrich_instrument_session_state(
        client=client,
        store=registry,
        instruments=instruments,
        as_of=scheduled_at,
        concurrency=config.scanner.live_acquisition_concurrency,
        batch_size=config.scanner.live_acquisition_batch_size,
        refresh_ahead=timedelta(seconds=120),
    )
    session_states = registry.etoro_session_states(as_of=scheduled_at)
    from app.orchestration.session_state import partition_temporarily_observed_dash

    # Refresh above always includes DASH, allowing automatic re-admission.
    instruments, observed_dash = partition_temporarily_observed_dash(
        instruments, states=session_states, as_of=scheduled_at,
    )
    registry.append("etoro-runtime-temporary-buy-holds", {
        "observed_at": scheduled_at.isoformat(),
        "runtime_count": len(instruments),
        "observed": [{"instrument_id": item.broker_instrument_id,
                      "symbol": item.symbol, "reason": "CRYPTO_BUY_DISABLED",
                      "recheck": "SESSION_TTL", "scope": "NEW_AUTONOMOUS_BUYS"}
                     for item in observed_dash],
    })
    # Rank the complete validated universe together.  Asset class remains an
    # input to the asset-specific strategy profile, while session state only
    # controls whether an instrument can reach execution.  Previously this
    # selected the first open lane (usually seven Crypto instruments), which
    # made the global ranking misleading and hid valid equity/ETF candidates.
    registry.append("etoro-runtime-universe-scope", {
        "observed_at": scheduled_at.isoformat(),
        "asset_classes": sorted({instrument.asset_class.value for instrument in instruments}),
        "runtime_count": len(instruments),
        "reason": "FULL_VALIDATED_UNIVERSE_RANKING",
    })
    session_counts = Counter(str(state["session_state"]) for state in session_states.values())
    registry.append(
        "etoro-session-state-refresh",
        {
            "provider": "etoro",
            "observed_at": scheduled_at.isoformat(),
            "counts": dict(sorted(session_counts.items())),
            "instruments": len(instruments),
        },
    )
    coordinator = EtoroOneHourAcquisitionCoordinator(
        client=client,
        cache=cache,
        store=registry,
        batch_size=config.scanner.live_acquisition_batch_size,
        concurrency=config.scanner.live_acquisition_concurrency,
        crypto_fallback_provider=_alpaca_crypto_market_data_fallback(values),
    )
    bars_by_symbol, acquisition = coordinator.refresh(
        instruments=instruments,
        as_of=scheduled_at,
    )
    registry.append("etoro-market-data-acquisition", acquisition)
    snapshot = build_coherent_one_hour_snapshot(
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        as_of=scheduled_at,
        minimum_coverage_ratio=minimum_coverage,
        include_closed_for_ranking=True,
    )
    coverage = {
        "total_universe": snapshot.total_universe,
        "eligible_for_target_bar": snapshot.eligible_for_target_bar,
        "stale": snapshot.stale,
        "unavailable": snapshot.unavailable,
        "session_not_expected": snapshot.session_not_expected,
        "coverage_denominator": snapshot.coverage_denominator,
        "coverage_ratio": str(snapshot.coverage_ratio),
        "minimum_coverage_ratio": str(snapshot.minimum_coverage_ratio),
        "target_completed_bar": snapshot.target_completed_bar.isoformat(),
        "eligible_by_session_group": snapshot.eligible_by_session_group,
        "excluded_by_session_group": snapshot.excluded_by_session_group,
        "target_completed_bars_by_session_group": {
            group: timestamp.isoformat()
            for group, timestamp in snapshot.target_completed_bars_by_session_group.items()
        },
    }
    from app.data.historical.quotes import QuoteObservationStore
    from app.orchestration.quote_acquisition import observe_runtime_quotes

    quote_store = QuoteObservationStore(Path("work") / "quote-observations.sqlite3")
    try:
        # Respect throttling from the shared market-data provider stream.
        if acquisition.get("acquisition_outcome_counts", {}).get("RATE_LIMITED", 0):
            quote_store.set_cooldown(until=effective_clock() + timedelta(minutes=15))
        quote_report = observe_runtime_quotes(
            client=EtoroReadClient(credentials, DisciplinedHttpClient(
                UrllibTransport(config.etoro_transport_mode), max_read_attempts=1)),
            store=quote_store, instruments=snapshot.instruments,
            cutoff=scheduled_at, clock=effective_clock,
        )
        registry.append("etoro-runtime-quote-observations", quote_report)
    finally:
        quote_store.close()
    if not snapshot.coverage_sufficient:
        coverage_blocker = (
            "NO_OPEN_MARKETS"
            if snapshot.total_universe > 0
            and snapshot.session_not_expected == snapshot.total_universe
            else "INSUFFICIENT_COHERENT_MARKET_COVERAGE"
        )
        # News is an independent observational input. Run it before the
        # market-data gate so the dashboard can show current geopolitical and
        # provider state even while Aegis fail-closes trading decisions.
        degraded_news = build_runtime_news_engine(config, values).analyze(
            instruments=tuple(instruments),
            as_of=scheduled_at,
        )
        blocked_payload = _demo_runtime_payload(
            status="BLOCKED",
            pilot_enabled=True,
            blockers=(coverage_blocker,),
            authorized_capital_eur=config.authorized_capital_eur,
        )
        blocked_payload.update(acquisition)
        blocked_payload.update(coverage)
        blocked_payload["current_spread_assessments"] = quote_report["spread_assessments"]
        blocked_payload["demo_exit"] = demo_exit_report
        blocked_payload.update(_news_result_payload(degraded_news))
        blocked_payload["a4c_reason"] = coverage_blocker
        blocked_payload["scanner_reached"] = False
        blocked_payload["news_reached"] = True
        blocked_payload.update(_runtime_execution_state(config, credentials_present=True))
        return blocked_payload
    portfolio = PortfolioSnapshot(as_of=scheduled_at, currency=Currency.EUR, cash=Decimal("200"))
    audit_store = default_active_intelligence_audit_store()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        news_engine=build_runtime_news_engine(config, values),
        audit_store=audit_store,
    )
    switch = KillSwitch(
        active=config.kill_switch,
        reason="configured Demo runtime state",
        clock=effective_clock,
    )
    risk_manager = RiskManager(
        config.risk,
        switch,
        authorization_key=b"automatic-demo-runtime-risk-authorization-key!",
        clock=effective_clock,
    )
    execution_available = _demo_execution_available(config, credentials_present=True)
    gateway = (
        RiskCheckedEtoroDemoSubmissionGateway(
            environment=config.operating_mode,
            credentials=credentials,
            http=http,
            risk_manager=risk_manager,
            gate=RiskEnforcedExecutionGate(risk_manager),
            kill_switch=switch,
            registry=registry,
            readback=_EtoroReadback(client),
            tradability_revalidator=lambda instrument_id, symbol, as_of: _fresh_demo_tradability(
                client, instrument_id, symbol, as_of
            ),
        )
        if execution_available
        else None
    )
    package_material_diagnostics: dict[str, object] = {}
    runtime = AegisEtoroAutomaticDemoRuntime(
        config=config,
        values=values,
        orchestrator=orchestrator,
        registry=registry,
        gateway=gateway,
        package_provider=(
            lambda cycle, result: _build_live_submission_packages(
                cycle=cycle,
                scanner_result=result,
                client=client,
                config=config,
                settings=demo_pilot_settings(values),
                registry=registry,
                diagnostics=package_material_diagnostics,
            )
        )
        if execution_available
        else None,
    )
    result = runtime.run_once(
        scheduled_at=scheduled_at,
        instruments=snapshot.instruments,
        bars_by_symbol=snapshot.bars_by_symbol,
        portfolio=portfolio,
        timeframe=TimeFrame.ONE_HOUR,
        shadow_capital=Decimal("200"),
    )
    result.update(acquisition)
    result.update(coverage)
    result["package_material_diagnostics"] = package_material_diagnostics
    result["current_spread_assessments"] = quote_report["spread_assessments"]
    result["demo_exit"] = demo_exit_report
    result["scheduled_at"] = scheduled_at.isoformat()
    result["cycle_as_of_resolved"] = cycle_as_of.isoformat()
    result["package_policy"] = (
        "verified execution packages are required; unavailable materials fail closed"
    )
    result.update(_runtime_execution_state(config, credentials_present=True))
    result["scanner_reached"] = result.get("cycle_id") is not None
    result["news_reached"] = result.get("news_scan_completed_at") is not None
    settings = demo_pilot_settings(values)
    if settings.notional_eur is not None:
        result["pilot_notional_eur"] = str(settings.notional_eur)
    result["authorized_capital_eur"] = str(config.authorized_capital_eur)
    result["sizing_mode"] = "RISK_MANAGER_AUTHORIZED_CAPITAL"
    result["active_scanner_universe_count"] = len(instruments)
    artifact = read_etoro_dynamic_universe_artifact()
    if artifact is not None:
        result["universe"] = {
            "catalog_snapshot_id": artifact.get("source_snapshot_id"),
            "catalog_instrument_count": artifact.get("catalog_instrument_count"),
            "verified_mapping_count": artifact.get("verified_mapping_count"),
            "market_data_ready_count": artifact.get("market_data_ready_count"),
            "blocked_count": artifact.get("blocked_count"),
            "blocked_reasons": artifact.get("blocked_reasons", {}),
        }
    from app.data.runtime import EXIT_EVIDENCE_SYMBOLS_BY_CLASS

    result["validated_baseline_count"] = sum(
        len(symbols) for symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.values()
    )
    return result


def build_etoro_calibration_read_only_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str],
    clock: Callable[[], datetime] | None = None,
    max_iterations: int = 1,
    candidate_ids: Collection[str] | None = None,
    acquisition_batch_size: int | None = None,
) -> dict[str, object]:
    """Run the production market read path without the production coverage gate.

    This is deliberately a diagnostic path: it never creates an A4C cycle,
    execution gateway, claim, or broker write.  The minimum used for the
    coherence model is a local diagnostic value and is never persisted as
    configuration.
    """
    from app.data.runtime import (
        DEFAULT_MARKET_DATA_CACHE_PATH,
        _catalog_items,
        _catalog_value,
        _etoro_bootstrap_instrument,
        load_etoro_dynamic_active_scanner_instruments,
        read_etoro_instrument_catalog_snapshot,
    )

    if config.operating_mode is not OperatingMode.ETORO_DEMO:
        return {"status": "BLOCKED", "blocker": "ETORO_DEMO_MODE_REQUIRED", "broker_write_calls": 0}
    credentials = runtime_credentials(values)
    if credentials is None:
        return {
            "status": "BLOCKED",
            "blocker": "ETORO_CREDENTIALS_NOT_CONFIGURED",
            "broker_write_calls": 0,
        }
    try:
        if candidate_ids is None:
            instruments = load_etoro_dynamic_active_scanner_instruments()
        else:
            catalog = read_etoro_instrument_catalog_snapshot()
            if catalog is None:
                raise ValueError("ETORO_CATALOG_SNAPSHOT_MISSING")
            wanted = {str(instrument_id) for instrument_id in candidate_ids}
            items = {
                _catalog_value(item, "instrumentID", "instrumentId"): item
                for item in _catalog_items(catalog["raw_response"])
            }
            missing = sorted(wanted - set(items))
            if missing:
                raise ValueError("CANDIDATE_INSTRUMENTS_MISSING:" + ",".join(missing))
            now = (clock or (lambda: datetime.now(UTC)))()
            instruments = tuple(
                _etoro_bootstrap_instrument(
                    items[instrument_id],
                    snapshot_id=str(catalog["snapshot_id"]),
                    now=now,
                )
                for instrument_id in sorted(wanted, key=lambda value: (int(value), value))
            )
    except ValueError as exc:
        return {"status": "BLOCKED", "blocker": str(exc), "broker_write_calls": 0}
    if not instruments:
        return {"status": "BLOCKED", "blocker": "NO_ACTIVE_INSTRUMENTS", "broker_write_calls": 0}

    calibration_clock = clock or (lambda: datetime.now(UTC))
    store = SqliteRecordStore(Path("work") / "etoro-coverage-calibration.sqlite3")
    client = EtoroReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
    from app.orchestration.session_state import (
        enrich_instrument_session_state,
        session_state_reconciliation,
    )

    coordinator = EtoroOneHourAcquisitionCoordinator(
        client=client,
        cache=HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH),
        store=store,
        batch_size=acquisition_batch_size or config.scanner.live_acquisition_batch_size,
        concurrency=config.scanner.live_acquisition_concurrency,
        crypto_fallback_provider=_alpaca_crypto_market_data_fallback(values),
    )
    iterations = max(1, max_iterations)
    acquisition: dict[str, object] = {}
    bars_by_symbol: dict[str, tuple[MarketBar, ...]] = {}
    as_of = calibration_clock()
    enriched = instruments
    for _ in range(iterations):
        as_of = calibration_clock()
        enriched = enrich_instrument_session_state(
            client=client,
            store=store,
            instruments=instruments,
            as_of=as_of,
            concurrency=config.scanner.live_acquisition_concurrency,
        )
        bars_by_symbol, acquisition = coordinator.refresh(instruments=enriched, as_of=as_of)
        if acquisition.get("acquisition_status") == "COMPLETE":
            break
    states = store.etoro_session_states(as_of=as_of)
    session_counts, unknown_breakdown = session_state_reconciliation(
        instruments=enriched, states=states, as_of=as_of
    )
    unsupported_internal = session_counts.get("UNSUPPORTED_INTERNAL", 0)
    genuine_unknown = session_counts.get("UNKNOWN", 0)
    snapshot = build_coherent_one_hour_snapshot(
        instruments=enriched,
        bars_by_symbol=bars_by_symbol,
        as_of=as_of,
        minimum_coverage_ratio=Decimal("0.0001"),
    )
    portfolio = PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("2000"))
    news = build_runtime_news_engine(config, values).analyze(
        instruments=snapshot.instruments,
        as_of=as_of,
    )
    scanner = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=snapshot.instruments,
        bars_by_symbol=snapshot.bars_by_symbol,
        portfolio=portfolio,
        as_of=as_of,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("2000"),
        news_context_by_symbol=news.asset_contexts,
    )
    by_class = Counter(instrument.asset_class.value for instrument in enriched)
    active_session = sum(
        1
        for instrument in enriched
        if states.get(instrument.broker_instrument_id, {}).get("session_state") == "OPEN_TRADABLE"
    )
    eligible = sum(
        1
        for instrument in snapshot.instruments
        if states.get(instrument.broker_instrument_id, {}).get("session_state") == "OPEN_TRADABLE"
    )
    runtime_denominator = snapshot.coverage_denominator
    runtime_eligible = snapshot.eligible_for_target_bar
    ratio = snapshot.coverage_ratio
    raw_acquisition_counts = acquisition.get("acquisition_outcome_counts")
    acquisition_counts = (
        {str(key): value for key, value in raw_acquisition_counts.items() if isinstance(value, int)}
        if isinstance(raw_acquisition_counts, Mapping)
        else {}
    )
    calibration_valid, calibration_invalid_reasons = _calibration_validity(
        acquisition_status=str(acquisition.get("acquisition_status", "UNKNOWN")),
        unknown_count=genuine_unknown,
        active_session_denominator=runtime_denominator,
        acquisition_outcome_counts=acquisition_counts,
    )
    report = {
        "status": "CALIBRATION_READ_ONLY_COMPLETE",
        "calibration_only": True,
        "as_of": as_of.isoformat(),
        "session_state_reconciliation": {
            "OPEN_TRADABLE": session_counts.get("OPEN_TRADABLE", 0),
            "OPEN_NOT_TRADABLE": session_counts.get("OPEN_NOT_TRADABLE", 0),
            "CLOSED": session_counts.get("CLOSED", 0),
            "UNKNOWN": session_counts.get("UNKNOWN", 0),
            "UNSUPPORTED_INTERNAL": unsupported_internal,
            "TOTAL": len(enriched),
            "by_asset_class": dict(sorted(by_class.items())),
        },
        "unknown_breakdown": dict(sorted(unknown_breakdown.items())),
        "catalog_total": len(enriched),
        "calibratable_total": len(enriched) - unsupported_internal,
        "unsupported_internal_count": unsupported_internal,
            "active_session_denominator": active_session,
            "runtime_coverage_denominator": runtime_denominator,
            "runtime_coverage_numerator": runtime_eligible,
            "causally_eligible": eligible,
            "measured_coverage_ratio": str(ratio),
        "scanner_input_count": len(scanner.candidates),
        "nonzero_score_count": sum(item.opportunity_score > 0 for item in scanner.candidates),
        "nonzero_confidence_count": sum(item.confidence > 0 for item in scanner.candidates),
        "top_count": len(scanner.top_opportunities),
        "watchlist_count": len(scanner.watchlist),
        "no_trade_count": len(scanner.no_trade),
        "top_candidates": [
            {
                "symbol": item.symbol,
                "asset_class": item.asset_class.value,
                "score": str(item.opportunity_score),
                "confidence": str(item.confidence),
                "decision": item.decision.value,
                "bucket": item.bucket.value,
                "rank": item.rank,
            }
            for item in scanner.top_opportunities
        ],
        "acquisition": acquisition,
        "news": {
            "provider": news.provider_name,
            "status": news.provider_status.value,
            "events_received": len(news.normalized_events),
            "cutoff": as_of.isoformat(),
        },
        "calibrated_minimum_coverage_ratio": None,
        "calibration_justification": (
            "No production threshold configured or changed; evidence is reported for review."
        ),
        "calibration_valid": calibration_valid,
        "calibration_invalid_reasons": calibration_invalid_reasons,
        "broker_write_calls": 0,
        "demo_writes": 0,
        "real_writes": 0,
    }
    if candidate_ids is not None:
        report["candidate_set_read_only"] = True
        report["candidate_instrument_ids"] = [
            instrument.broker_instrument_id for instrument in instruments
        ]
    store.append("etoro-calibration-read-only", report)
    return report


def build_etoro_full_catalog_candidate_calibration_read_only_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str],
    clock: Callable[[], datetime] | None = None,
    max_iterations: int = 1,
    acquisition_batch_size: int = 64,
    audit_path: Path = DEFAULT_ETORO_FULL_CATALOG_SESSION_AUDIT_PATH,
) -> dict[str, object]:
    """Evaluate full-catalog OPEN_TRADABLE discoveries without mutating Demo universe."""
    if not audit_path.exists():
        return {
            "status": "BLOCKED",
            "blocker": "FULL_CATALOG_AUDIT_MISSING",
            "broker_write_calls": 0,
        }
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        records = audit.get("records", {})
        if not isinstance(records, dict):
            raise ValueError("FULL_CATALOG_AUDIT_INVALID")
        from app.data.runtime import read_etoro_dynamic_universe_artifact

        active_artifact = read_etoro_dynamic_universe_artifact()
        active_records: list[object] = (
            cast(list[object], active_artifact.get("active_records", []))
            if isinstance(active_artifact, Mapping)
            and isinstance(active_artifact.get("active_records", []), list)
            else []
        )
        active_ids = {
            str(row.get("etoro_instrument_id")) for row in active_records if isinstance(row, dict)
        }
        candidate_ids = sorted(
            {
                str(instrument_id)
                for instrument_id, record in records.items()
                if isinstance(record, dict)
                and record.get("outcome") == "OPEN_TRADABLE"
                and str(instrument_id) not in active_ids
            },
            key=lambda value: (int(value), value),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "BLOCKED",
            "blocker": f"FULL_CATALOG_AUDIT_INVALID:{exc}",
            "broker_write_calls": 0,
        }
    report = build_etoro_calibration_read_only_report(
        config,
        values=values,
        clock=clock,
        max_iterations=max_iterations,
        candidate_ids=candidate_ids,
        acquisition_batch_size=acquisition_batch_size,
    )
    report["candidate_source"] = str(audit_path)
    report["candidate_count"] = len(candidate_ids)
    return report


def _legacy_build_etoro_full_catalog_session_audit_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    batch_size: int = 64,
    concurrency: int = 4,
    audit_path: Path = DEFAULT_ETORO_FULL_CATALOG_SESSION_AUDIT_PATH,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Audit current eToro session state for the full catalog, read-only.

    This deliberately does not bootstrap candles or rewrite the operational
    active-universe artifact.  Its cursor lets repeated invocations make
    bounded progress through the catalog while reusing the normal session DB.
    """
    if batch_size <= 0 or concurrency <= 0:
        raise ValueError("full-catalog audit limits must be positive")
    from app.data.runtime import (
        _catalog_asset_class,
        _catalog_items,
        _etoro_bootstrap_instrument,
        read_etoro_dynamic_universe_artifact,
        read_etoro_instrument_catalog_snapshot,
    )

    snapshot = read_etoro_instrument_catalog_snapshot()
    if snapshot is None:
        return {
            "status": "BLOCKED",
            "blocker": "ETORO_CATALOG_SNAPSHOT_MISSING",
            "broker_write_calls": 0,
        }
    raw_items = _catalog_items(snapshot.get("raw_response"))
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None:
        raise ValueError("full-catalog audit timestamp must be timezone-aware")
    internal_ids = {
        str(item["instrumentID"])
        for item in raw_items
        if item.get("isInternalInstrument") is True and item.get("instrumentID") is not None
    }
    external_items = tuple(
        item for item in raw_items if str(item.get("instrumentID")) not in internal_ids
    )
    cursor = 0
    if audit_path.exists():
        try:
            saved = json.loads(audit_path.read_text(encoding="utf-8"))
            if saved.get("source_snapshot_id") == snapshot.get("snapshot_id"):
                cursor = int(saved.get("cursor", 0)) % max(1, len(external_items))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            cursor = 0
    credentials = runtime_credentials(values)
    if client is None and credentials is None:
        return {
            "status": "BLOCKED",
            "blocker": "ETORO_CREDENTIALS_NOT_CONFIGURED",
            "full_catalog_total": len(raw_items),
            "broker_write_calls": 0,
            "real_execution_available": False,
        }
    if client is None and not config.etoro_api_enabled:
        return {
            "status": "BLOCKED",
            "blocker": "ETORO_API_DISABLED",
            "full_catalog_total": len(raw_items),
            "broker_write_calls": 0,
            "real_execution_available": False,
        }
    read_client = client
    if read_client is None:
        assert credentials is not None
        read_client = EtoroReadClient(
            credentials,
            DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
        )
    from app.orchestration.session_state import enrich_instrument_session_state

    batch = tuple(
        external_items[(cursor + offset) % len(external_items)]
        for offset in range(min(batch_size, len(external_items)))
    )
    instruments = tuple(
        _etoro_bootstrap_instrument(item, snapshot_id=str(snapshot["snapshot_id"]), now=now)
        for item in batch
        if _catalog_asset_class(item) in {"EQUITY", "ETF", "CRYPTO"}
    )
    store = SqliteRecordStore(Path("work") / "etoro-coverage-calibration.sqlite3")
    if instruments:
        enrich_instrument_session_state(
            client=read_client,
            store=store,
            instruments=instruments,
            as_of=now,
            concurrency=concurrency,
        )
    next_cursor = (cursor + len(batch)) % max(1, len(external_items))
    audit_payload = {
        "schema_version": 1,
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_endpoint": "etoro-market-data-search",
        "updated_at": now.isoformat(),
        "cursor": next_cursor,
        "batch_size": batch_size,
        "last_batch_instrument_ids": [str(item["instrumentID"]) for item in batch],
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=audit_path.parent, delete=False
        ) as temporary:
            temporary_path = temporary.name
            json.dump(audit_payload, temporary, sort_keys=True)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, audit_path)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)

    states = store.etoro_session_states(as_of=now)
    state_counts: Counter[str] = Counter()
    for item in raw_items:
        instrument_id = str(item.get("instrumentID"))
        if instrument_id in internal_ids:
            state_counts["UNSUPPORTED_INTERNAL"] += 1
        else:
            state_counts[
                str(states.get(instrument_id, {}).get("session_state", "NOT_YET_AUDITED"))
            ] += 1
    active_artifact = read_etoro_dynamic_universe_artifact()
    active_rows: list[object] = []
    if isinstance(active_artifact, Mapping) and isinstance(
        active_artifact.get("active_records"), list
    ):
        active_rows = cast(list[object], active_artifact["active_records"])
    active_ids = {
        str(row.get("etoro_instrument_id")) for row in active_rows if isinstance(row, dict)
    }
    open_ids = {
        str(item["instrumentID"])
        for item in raw_items
        if str(item.get("instrumentID")) in states
        and states[str(item["instrumentID"])].get("session_state") == "OPEN_TRADABLE"
    }
    return {
        "status": "ETORO_FULL_CATALOG_SESSION_AUDIT_COMPLETE",
        "as_of": now.isoformat(),
        "full_catalog_total": len(raw_items),
        "audited_total": sum(
            state_counts[key] for key in ("OPEN_TRADABLE", "OPEN_NOT_TRADABLE", "CLOSED", "UNKNOWN")
        )
        + state_counts["UNSUPPORTED_INTERNAL"],
        "open_tradable": state_counts["OPEN_TRADABLE"],
        "open_not_tradable": state_counts["OPEN_NOT_TRADABLE"],
        "closed": state_counts["CLOSED"],
        "unknown": state_counts["UNKNOWN"],
        "unsupported_internal": state_counts["UNSUPPORTED_INTERNAL"],
        "not_yet_audited": state_counts["NOT_YET_AUDITED"],
        "current_633_open_tradable": len(open_ids & active_ids),
        "new_open_tradable_outside_current_633": len(open_ids - active_ids),
        "data_capable_1h_1d": len(open_ids & active_ids),
        "candidate_universe_read_only": sorted(open_ids),
        "cursor": next_cursor,
        "batch_audited": len(batch),
        "audit_path": str(audit_path),
        "broker_write_calls": 0,
        "demo_writes": 0,
        "real_writes": 0,
        "real_execution_available": False,
    }


def build_etoro_full_catalog_session_audit_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    batch_size: int = 64,
    concurrency: int = 4,
    audit_path: Path = DEFAULT_ETORO_FULL_CATALOG_SESSION_AUDIT_PATH,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Audit the full catalog with cumulative per-instrument accounting."""
    if batch_size <= 0 or concurrency <= 0:
        raise ValueError("full-catalog audit limits must be positive")
    from app.data.runtime import (
        _catalog_items,
        _etoro_bootstrap_instrument,
        read_etoro_dynamic_universe_artifact,
        read_etoro_instrument_catalog_snapshot,
    )

    snapshot = read_etoro_instrument_catalog_snapshot()
    if snapshot is None:
        return {
            "status": "BLOCKED",
            "blocker": "ETORO_CATALOG_SNAPSHOT_MISSING",
            "broker_write_calls": 0,
        }
    raw_items = _catalog_items(snapshot.get("raw_response"))
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None:
        raise ValueError("full-catalog audit timestamp must be timezone-aware")
    internal_ids = {
        str(item["instrumentID"])
        for item in raw_items
        if item.get("isInternalInstrument") is True and item.get("instrumentID") is not None
    }
    external_items = tuple(
        item for item in raw_items if str(item.get("instrumentID")) not in internal_ids
    )
    from app.data.runtime import catalog_session_exclusion_reason

    auditable_items = tuple(
        item for item in external_items if catalog_session_exclusion_reason(item) is None
    )
    not_auditable_items = tuple(item for item in external_items if item not in auditable_items)
    progress: dict[str, dict[str, object]] = {}
    cursor = 0
    if audit_path.exists():
        try:
            saved = json.loads(audit_path.read_text(encoding="utf-8"))
            if saved.get("source_snapshot_id") == snapshot.get("snapshot_id"):
                cursor = int(saved.get("cursor", 0)) % max(1, len(auditable_items))
                raw_progress = saved.get("records", {})
                if isinstance(raw_progress, dict):
                    progress = {
                        str(key): value
                        for key, value in raw_progress.items()
                        if isinstance(value, dict)
                    }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            cursor = 0
    for item in not_auditable_items:
        instrument_id = str(item["instrumentID"])
        progress.setdefault(
            instrument_id,
            {
                "instrument_id": instrument_id,
                "symbol": item.get("symbolFull"),
                "outcome": "NOT_AUDITABLE_CURRENT_PROVIDER",
                "reason": catalog_session_exclusion_reason(item),
                "observed_at": None,
            },
        )
    credentials = runtime_credentials(values)
    if client is None and credentials is None:
        return {
            "status": "BLOCKED",
            "blocker": "ETORO_CREDENTIALS_NOT_CONFIGURED",
            "full_catalog_total": len(raw_items),
            "broker_write_calls": 0,
            "real_execution_available": False,
        }
    if client is None and not config.etoro_api_enabled:
        return {
            "status": "BLOCKED",
            "blocker": "ETORO_API_DISABLED",
            "full_catalog_total": len(raw_items),
            "broker_write_calls": 0,
            "real_execution_available": False,
        }
    read_client = client
    if read_client is None:
        assert credentials is not None
        read_client = EtoroReadClient(
            credentials,
            DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
        )
    from app.orchestration.session_state import enrich_instrument_session_state

    retryable_error_classes = {"HTTP_ERROR", "NETWORK_TRANSPORT_ERROR"}
    retryable = tuple(
        item
        for item in auditable_items
        if str(item["instrumentID"]) in progress
        and str(progress[str(item["instrumentID"])].get("outcome", "UNKNOWN")) == "UNKNOWN"
        and str(
            progress[str(item["instrumentID"])].get("error_class")
            or progress[str(item["instrumentID"])].get("reason")
            or ""
        ) in retryable_error_classes
    )
    unqueried = tuple(item for item in auditable_items if str(item["instrumentID"]) not in progress)
    already_audited_count = len(auditable_items) - len(unqueried)
    ordered = tuple(
        auditable_items[(cursor + offset) % len(auditable_items)]
        for offset in range(len(auditable_items))
    )
    queued = retryable + unqueried + tuple(
        item for item in ordered if item not in retryable and item not in unqueried
    )
    selected = queued[: min(batch_size, len(auditable_items))]
    instruments = tuple(
        _etoro_bootstrap_instrument(item, snapshot_id=str(snapshot["snapshot_id"]), now=now)
        for item in selected
    )
    store = SqliteRecordStore(Path("work") / "etoro-coverage-calibration.sqlite3")
    if instruments:
        enrich_instrument_session_state(
            client=read_client,
            store=store,
            instruments=instruments,
            as_of=now,
            concurrency=concurrency,
            force_refresh=True,
        )
    states = store.etoro_session_states(as_of=now)
    for item in selected:
        instrument_id = str(item["instrumentID"])
        state = states.get(instrument_id)
        progress[instrument_id] = {
            "instrument_id": instrument_id,
            "symbol": item.get("symbolFull"),
            "outcome": (
                str(state.get("session_state", "UNKNOWN")) if state is not None else "UNKNOWN"
            ),
            "error_class": state.get("error_class") if state is not None else "NO_STATE_PERSISTED",
            "observed_at": state.get("observed_at") if state is not None else now.isoformat(),
        }
    next_cursor = (cursor + len(selected)) % max(1, len(auditable_items))
    audit_payload = {
        "schema_version": 2,
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_endpoint": "etoro-market-data-search",
        "updated_at": now.isoformat(),
        "cursor": next_cursor,
        "batch_size": batch_size,
        "last_selected_ids": [str(item["instrumentID"]) for item in selected],
        "records": progress,
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=audit_path.parent, delete=False
        ) as temporary:
            temporary_path = temporary.name
            json.dump(audit_payload, temporary, sort_keys=True)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, audit_path)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    audited = [
        row for row in progress.values() if row.get("outcome") != "NOT_AUDITABLE_CURRENT_PROVIDER"
    ]
    counts = Counter(str(row.get("outcome", "UNKNOWN")) for row in audited)
    active_artifact = read_etoro_dynamic_universe_artifact()
    active_rows: list[object] = []
    if isinstance(active_artifact, Mapping) and isinstance(
        active_artifact.get("active_records"), list
    ):
        active_rows = cast(list[object], active_artifact["active_records"])
    active_ids = {
        str(row.get("etoro_instrument_id")) for row in active_rows if isinstance(row, dict)
    }
    open_ids = {
        str(row["instrument_id"]) for row in audited if row.get("outcome") == "OPEN_TRADABLE"
    }
    return {
        "status": "ETORO_FULL_CATALOG_SESSION_AUDIT_COMPLETE",
        "as_of": now.isoformat(),
        "full_catalog_total": len(raw_items),
        "static_internal": len(internal_ids),
        "unsupported_internal": len(internal_ids),
        "auditable_current_provider": len(auditable_items),
        "not_auditable_current_provider": len(not_auditable_items),
        "selected_this_run": len(selected),
        "queried_this_run": len(selected),
        "persisted_this_run": sum(1 for item in selected if str(item["instrumentID"]) in states),
        "already_audited": already_audited_count,
        "audited_cumulative": len(audited),
        "not_yet_audited": len(auditable_items) - len(audited),
        "open_tradable": counts.get("OPEN_TRADABLE", 0),
        "open_not_tradable": counts.get("OPEN_NOT_TRADABLE", 0),
        "closed": counts.get("CLOSED", 0),
        "unknown": counts.get("UNKNOWN", 0),
        "current_633_open_tradable": len(open_ids & active_ids),
        "new_open_tradable_outside_current_633": len(open_ids - active_ids),
        "candidate_universe_read_only": sorted(open_ids),
        "cursor": next_cursor,
        "audit_path": str(audit_path),
        "broker_write_calls": 0,
        "demo_writes": 0,
        "real_writes": 0,
        "real_execution_available": False,
    }


def _calibration_validity(
    *,
    acquisition_status: str,
    unknown_count: int,
    active_session_denominator: int,
    acquisition_outcome_counts: Mapping[str, int],
) -> tuple[bool, tuple[str, ...]]:
    """Decide whether calibration evidence is promotable, independently of its ratio."""
    reasons: list[str] = []
    if acquisition_status != "COMPLETE":
        reasons.append(f"ACQUISITION_STATUS_{acquisition_status}")
    if unknown_count > 0:
        reasons.append("UNKNOWN_SESSION_STATE_PRESENT")
    if active_session_denominator <= 0:
        reasons.append("NO_ACTIVE_SESSION_DENOMINATOR")
    for outcome in (
        "NOT_ATTEMPTED",
        "RETRY_BACKOFF",
        "RATE_LIMITED",
        "HTTP_ERROR",
        "PROVIDER_UNAVAILABLE",
        "AUTH_ERROR",
        "TIMEOUT",
        "PARSE_ERROR",
        "OTHER_ERROR",
    ):
        count = acquisition_outcome_counts.get(outcome, 0)
        if count:
            reasons.append(f"ACQUISITION_{outcome}:{count}")
    return not reasons, tuple(reasons)


def collect_etoro_calibration_evidence(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str],
    max_attempts: int | None = None,
    interval_seconds: float = 900.0,
    minimum_useful_runs: int = 3,
    minimum_active_denominator: int = 2,
    sleeper: Callable[[float], None] | None = None,
    run_report: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Collect bounded, read-only calibration observations until useful evidence exists."""
    if interval_seconds <= 0 or minimum_useful_runs <= 0 or minimum_active_denominator <= 0:
        raise ValueError("calibration collector limits must be positive")
    if max_attempts is not None and max_attempts <= 0:
        raise ValueError("max_attempts must be positive when supplied")

    execute = run_report or (
        lambda: build_etoro_calibration_read_only_report(
            config,
            values=values,
            max_iterations=1,
        )
    )
    store = SqliteRecordStore(Path("work") / "etoro-coverage-calibration.sqlite3")
    observations: list[dict[str, object]] = []
    useful_count = 0
    attempts = 0
    sleep_fn = sleeper or sleep
    try:
        while max_attempts is None or attempts < max_attempts:
            attempts += 1
            report = execute()
            session = report.get("session_state_reconciliation")
            session_data = session if isinstance(session, Mapping) else {}
            acquisition = report.get("acquisition")
            acquisition_data = acquisition if isinstance(acquisition, Mapping) else {}
            denominator = _collector_int(
                report.get("runtime_coverage_denominator", report.get("active_session_denominator", 0))
            )
            useful = (
                bool(report.get("calibration_valid")) and denominator >= minimum_active_denominator
            )
            invalid_reasons = _collector_sequence(report.get("calibration_invalid_reasons", ()))
            if useful:
                classification = "USEFUL_EVIDENCE"
                useful_count += 1
            elif str(acquisition_data.get("acquisition_status")) != "COMPLETE" or any(
                "RETRY_BACKOFF" in str(reason)
                or "RATE_LIMITED" in str(reason)
                or "PROVIDER_UNAVAILABLE" in str(reason)
                for reason in invalid_reasons
            ):
                classification = "TRANSIENT_OR_BACKOFF"
            else:
                classification = "INVALID_NON_PROMOTABLE"
            observation = {
                "collector_attempt": attempts,
                "classification": classification,
                "observed_at": report.get("as_of"),
                "catalog_total": report.get("catalog_total", session_data.get("TOTAL", 0)),
                "calibratable_total": report.get("calibratable_total"),
                "active_session_denominator": denominator,
                "causally_eligible": report.get("causally_eligible", 0),
                "measured_coverage_ratio": report.get("measured_coverage_ratio"),
                "open_tradable": session_data.get("OPEN_TRADABLE", 0),
                "closed": session_data.get("CLOSED", 0),
                "unknown": session_data.get("UNKNOWN", 0),
                "unsupported_internal": session_data.get("UNSUPPORTED_INTERNAL", 0),
                "scanner_input_count": report.get("scanner_input_count", 0),
                "nonzero_score_count": report.get("nonzero_score_count", 0),
                "nonzero_confidence_count": report.get("nonzero_confidence_count", 0),
                "top_count": report.get("top_count", 0),
                "calibration_valid": bool(report.get("calibration_valid")),
                "calibration_invalid_reasons": invalid_reasons,
                "acquisition_status": acquisition_data.get("acquisition_status"),
            }
            store.append("etoro-calibration-collector-observation", observation)
            observations.append(observation)
            if useful_count >= minimum_useful_runs:
                stop_reason = "USEFUL_EVIDENCE_TARGET_REACHED"
                break
            if max_attempts is not None and attempts >= max_attempts:
                stop_reason = "MAX_ATTEMPTS_REACHED"
                break
            sleep_fn(interval_seconds)
    except KeyboardInterrupt:
        stop_reason = "STOP_REQUESTED"
    else:
        stop_reason = locals().get("stop_reason", "COLLECTOR_STOPPED")

    summary = {
        "status": "CALIBRATION_EVIDENCE_COLLECTION_COMPLETE",
        "collector_only": True,
        "attempts": attempts,
        "useful_evidence_runs": useful_count,
        "observations": tuple(observations),
        "minimum_useful_runs": minimum_useful_runs,
        "minimum_active_denominator": minimum_active_denominator,
        "interval_seconds": interval_seconds,
        "stop_reason": stop_reason,
        "production_threshold_changed": False,
        "broker_write_calls": 0,
        "demo_writes": 0,
        "real_writes": 0,
    }
    store.append("etoro-calibration-collector-summary", summary)
    return summary


def _collector_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _collector_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _demo_execution_available(config: ApplicationConfig, *, credentials_present: bool) -> bool:
    return bool(
        config.operating_mode is OperatingMode.ETORO_DEMO
        and config.broker_execution_mode is BrokerExecutionMode.DEMO_EXECUTION
        and config.execution_policy is ExecutionPolicy.AUTONOMOUS
        and config.etoro_api_enabled
        and config.etoro_demo_execution_enabled
        and config.etoro_demo_automatic_pilot_enabled
        and credentials_present
        and config.authorized_capital_eur is not None
        and config.authorized_capital_eur >= MIN_DEMO_AUTHORIZED_CAPITAL
        and not config.kill_switch
    )


def _runtime_execution_state(
    config: ApplicationConfig, *, credentials_present: bool
) -> dict[str, object]:
    return {
        "operating_mode": config.operating_mode.value,
        "broker_execution_mode": config.broker_execution_mode.value,
        "execution_policy": config.execution_policy.value,
        "execution_enabled": bool(
            config.broker_execution_mode is BrokerExecutionMode.DEMO_EXECUTION
            and config.etoro_demo_execution_enabled
        ),
        "execution_available": _demo_execution_available(
            config, credentials_present=credentials_present
        ),
    }


def _fresh_demo_tradability(
    client: EtoroReadClient, instrument_id: int, symbol: str, as_of: datetime
) -> bool:
    """Require a fresh authoritative Demo-side tradability answer before POST."""
    from app.brokers.etoro.tradability_revalidation import fresh_demo_tradability

    return fresh_demo_tradability(client, instrument_id, symbol, as_of)


def build_etoro_demo_runtime_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str],
    clock: Callable[[], datetime] | None = None,
    max_iterations: int | None = None,
) -> dict[str, object]:
    """Run the continuous Demo poller around the existing one-shot runtime."""
    poll_interval = _configured_runtime_seconds(
        values.get("AEGIS_ETORO_DEMO_POLL_INTERVAL_SECONDS"), default=60.0
    )
    error_backoff = _configured_runtime_seconds(
        values.get("AEGIS_ETORO_DEMO_ERROR_BACKOFF_SECONDS"), default=60.0
    )
    runner = EtoroDemoContinuousRunner(
        run_once=lambda: build_etoro_demo_runtime_once_report(
            config,
            values=values,
            clock=clock,
        ),
        clock=clock,
        poll_interval_seconds=poll_interval,
        error_backoff_seconds=error_backoff,
        status_store=SqliteRecordStore(DEFAULT_ETORO_DEMO_RUNTIME_STORE_PATH),
    )
    return runner.run(max_iterations=max_iterations)


def _configured_runtime_seconds(raw: str | None, *, default: float) -> float:
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        return default
    return value if 1.0 <= value <= 3600.0 else default


def build_runtime_news_engine(
    config: ApplicationConfig, values: Mapping[str, str]
) -> GlobalNewsIntelligenceEngine:
    """Build the explicitly configured read-only news source for the runner."""
    provider_mode = config.providers.news
    if provider_mode is ProviderMode.ALPHA_VANTAGE:
        primary = AlphaVantageNewsProvider(api_key=values.get("ALPHA_VANTAGE_API_KEY"))
        secondary_name = values.get("AEGIS_NEWS_SECONDARY_PROVIDER", "").strip().lower()
        if secondary_name == "alpaca":
            secondary = AlpacaNewsProvider(
                api_key_id=_first_nonempty(values, "ALPACA_API_KEY_ID", "APCA_API_KEY_ID"),
                api_secret_key=_first_nonempty(
                    values, "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY"
                ),
            )
            return GlobalNewsIntelligenceEngine(CrossCheckedNewsProvider(primary, secondary))
        return GlobalNewsIntelligenceEngine(
            primary
        )
    if provider_mode is ProviderMode.NONE:
        return GlobalNewsIntelligenceEngine(NewsFeedProvider(unavailable=True))
    return GlobalNewsIntelligenceEngine(NewsFeedProvider())


def _first_nonempty(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    return None


def _alpaca_crypto_market_data_fallback(
    values: Mapping[str, str],
) -> AlpacaHistoricalMarketDataProvider | None:
    """Create a read-only crypto bar fallback when Alpaca credentials exist."""
    key_id = _first_nonempty(values, "ALPACA_API_KEY_ID", "APCA_API_KEY_ID")
    secret = _first_nonempty(values, "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not key_id or not secret:
        return None
    return AlpacaHistoricalMarketDataProvider(
        api_key_id=key_id,
        api_secret_key=secret,
    )


def _candidate_news_available(
    cycle: ActiveIntelligenceCycleRecord, symbol: str
) -> bool:
    """Require fresh, causal news evidence for this candidate in the accepted cycle."""
    context = cycle.news_asset_contexts.get(symbol, {})
    if cycle.news_provider_status not in {"AVAILABLE", "PARTIAL"}:
        return False
    if context.get("freshness") != "NEWS_FRESH":
        return False
    raw_timestamp = context.get("latest_material_event_timestamp")
    if not isinstance(raw_timestamp, str):
        return False
    try:
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return timestamp.tzinfo is not None and timestamp <= cycle.news_cutoff_timestamp


def _global_demo_selection_blockers(
    *,
    cycle: ActiveIntelligenceCycleRecord,
    scanner_result: ActiveScannerResult,
    instruments: tuple[UniversalInstrument, ...],
) -> tuple[str, ...]:
    """Return quality blockers for the real Demo package path.

    This is deliberately a package-path gate, not a scanner gate: cycles are
    still persisted so the missing coverage is visible and measurable.
    """
    blockers: list[str] = []
    if cycle.news_events_fresh <= 0:
        blockers.append("FRESH_NEWS_REQUIRED")
    # Use the full evaluated candidate set for coverage diagnostics.  The
    # executable candidate may still need to be in an open session, but the
    # comparison itself must see the regions represented by the cached causal
    # universe rather than only the currently open exchange.
    candidates = tuple(scanner_result.candidates)
    by_symbol = {instrument.symbol: instrument for instrument in instruments}
    regions = {
        _selection_region(candidate.symbol, candidate.asset_class.value, by_symbol.get(candidate.symbol))
        for candidate in candidates
    }
    non_crypto_regions = {region for region in regions if region != "CRYPTO"}
    if non_crypto_regions and len(non_crypto_regions) < 2:
        blockers.append("MULTI_REGION_COVERAGE_REQUIRED")
    return tuple(blockers)


def _selection_region(
    symbol: str,
    asset_class: str,
    instrument: UniversalInstrument | None,
) -> str:
    if asset_class == AssetClass.CRYPTO.value:
        return "CRYPTO"
    exchange = (instrument.exchange if instrument is not None else None) or ""
    market = (instrument.market if instrument is not None else None) or ""
    raw = f"{exchange} {market} {symbol}".upper()
    for marker, region in (
        ("ASX", "AU"),
        ("HK", "HK"),
        (".PA", "FR"),
        (".DE", "DE"),
        (".MI", "IT"),
        (".L", "UK"),
        ("NASDAQ", "US"),
        ("NYSE", "US"),
    ):
        if marker in raw:
            return region
    return "OTHER"


def _build_live_submission_packages(
    *,
    cycle: ActiveIntelligenceCycleRecord,
    scanner_result: ActiveScannerResult,
    client: EtoroReadClient,
    config: ApplicationConfig,
    settings: EtoroDemoPilotSettings,
    registry: SqliteRecordStore,
    diagnostics: dict[str, object] | None = None,
) -> Mapping[str, EtoroDemoSubmissionPackage]:
    """Build execution material only after an accepted decision cycle."""
    def record(symbol: str, reason: str) -> None:
        if diagnostics is not None:
            diagnostics[symbol] = reason

    if config.authorized_capital_eur is None:
        if diagnostics is not None:
            diagnostics["_runtime"] = "AUTHORIZED_CAPITAL_MISSING"
        return {}
    managed_exposure = registry.managed_demo_exposure_eur()
    if managed_exposure is None:
        if diagnostics is not None:
            diagnostics["_runtime"] = "MANAGED_EXPOSURE_UNAVAILABLE"
        return {}
    try:
        identity = client.identity()
        demo_snapshot = client.demo_account(identity)
    except EtoroApiError as exc:
        # Account-read failures must not erase an already completed global
        # intelligence cycle or stop the continuous scanner.  Packaging is
        # an execution-time concern: keep the cycle observable, return no
        # executable package, and let the pilot fail closed for this poll.
        record(
            "_runtime",
            f"DEMO_ACCOUNT_READ_FAILED:{exc.status or exc.category.value}",
        )
        return {}
    # The eToro Demo account is denominated in USD in the live session, while
    # Aegis' authorization envelope is expressed in EUR.  Do not ask the
    # generic preflight to invent an FX conversion: size the Demo order in the
    # account currency. The configured 200-unit envelope is the total hard
    # upper bound, while each individual entry is capped separately so the
    # strategy can add a second tranche only after the first is confirmed.
    if demo_snapshot.currency not in {Currency.EUR, Currency.USD}:
        if diagnostics is not None:
            diagnostics["_runtime"] = (
                "DEMO_ACCOUNT_CURRENCY_UNSUPPORTED:" + demo_snapshot.currency.value
            )
        return {}
    demo_order_amount = min(
        config.authorized_capital_eur,
        DEMO_MAX_SINGLE_ORDER_AMOUNT,
        demo_snapshot.cash,
    )
    if demo_order_amount <= 0:
        if diagnostics is not None:
            diagnostics["_runtime"] = "DEMO_BUYING_POWER_UNAVAILABLE"
        return {}
    packages: dict[str, EtoroDemoSubmissionPackage] = {}
    # Prepare the TOP candidate first, but also prepare a bounded fallback
    # lane.  A transient resolution/quote/eligibility failure for the first
    # candidate must not turn a valid cycle into the historic zero-order dead
    # end.  The pilot still selects only package-ready candidates and keeps
    # every preflight, RiskManager and execution gate intact.
    ranked_watchlist = tuple(
        sorted(
            scanner_result.watchlist,
            key=lambda candidate: (
                candidate.asset_class.value == "CRYPTO",
                candidate.opportunity_score,
                candidate.confidence,
            ),
            reverse=True,
        )[:8]
    )
    candidates_for_demo = tuple(
        dict.fromkeys(
            candidate.symbol
            for candidate in tuple(scanner_result.top_opportunities) + ranked_watchlist
        )
    )
    candidate_by_symbol = {
        candidate.symbol: candidate
        for candidate in tuple(scanner_result.top_opportunities) + ranked_watchlist
    }
    candidates_for_demo = tuple(candidate_by_symbol[symbol] for symbol in candidates_for_demo)
    for candidate in candidates_for_demo:
        # A provider rate limit is local to this candidate.  It must not
        # abort the already completed intelligence cycle or kill the runner.
        try:
            resolution = _read_etoro_with_rate_limit_retry(
                lambda: client.resolve_instrument(candidate.symbol, as_of=cycle.scheduled_at)
            )
        except EtoroApiError as exc:
            record(candidate.symbol, f"RESOLUTION_READ_FAILED:{exc.status or 'UNKNOWN'}")
            continue
        if not resolution.verified or not resolution.structurally_supported:
            record(candidate.symbol, "RESOLUTION_NOT_VERIFIED_OR_UNSUPPORTED")
            continue
        resolved_class = asset_class_from_etoro_instrument_type(resolution.instrument_type)
        if resolved_class is AssetClass.UNKNOWN:
            resolved_class = classify_etoro_instrument_metadata(
                resolution.classification_metadata
            ).asset_class
        if resolved_class is not candidate.asset_class:
            record(candidate.symbol, "ASSET_CLASS_MISMATCH")
            continue
        instrument_id = resolution.instrument_id
        try:
            quote = _read_etoro_with_rate_limit_retry(
                lambda: client.quote(
                    instrument_id, candidate.symbol, currency=demo_snapshot.currency
                )
            )
            eligibility = _read_etoro_with_rate_limit_retry(
                lambda: client.demo_eligibility(
                    instrument_id,
                    candidate.symbol,
                    currency=demo_snapshot.currency,
                )
            )
        except EtoroApiError as exc:
            record(candidate.symbol, f"QUOTE_OR_ELIGIBILITY_READ_FAILED:{exc.status or 'UNKNOWN'}")
            continue
        # The rates endpoint intentionally maps market_status to UNKNOWN;
        # resolution is the authoritative live-session/tradability source.
        # Reuse the same Crypto 24/7-aware mapping as the controlled preflight
        # instead of failing every valid Crypto candidate at the next gate.
        quote = quote.model_copy(update={"market_status": _preflight_market_status(resolution)})
        instrument = _instrument_from_eligibility(resolution, eligibility, quote.as_of)
        risk_portfolio = _portfolio_from_demo_snapshot(
            demo_snapshot,
            target_instrument_id=instrument_id,
            target_symbol=candidate.symbol,
        )
        idempotency_key = f"etoro-demo-pilot:{cycle.cycle_id}:{candidate.symbol}:OPEN"
        proposal = TradeProposal(
            proposal_id=uuid5(NAMESPACE_URL, idempotency_key),
            idempotency_key=idempotency_key,
            created_at=cycle.scheduled_at,
            instrument_id=instrument_id,
            symbol=candidate.symbol,
            asset_class=candidate.asset_class,
            side=TradeSide.BUY,
            intent=TradeIntent.OPEN,
            amount=demo_order_amount,
            currency=demo_snapshot.currency,
            target_weight=config.strategy.target_position_weight,
            current_weight=risk_portfolio.weight_for(instrument_id),
            leverage=1,
            settlement_type=SettlementType.REAL,
            reason=(
                "accepted A4C TOP_OPPORTUNITY; execution-time checks are separate"
                if candidate.bucket.value == "TOP_OPPORTUNITIES"
                else "accepted Demo exploratory WATCHLIST candidate; hard gates remain active"
            ),
            evidence=tuple(
                EvidenceItem(
                    source="aegis-a4c-cycle",
                    timestamp=cycle.scheduled_at,
                    summary=(
                        f"accepted {candidate.bucket.value} rank={candidate.rank}"
                    ),
                    confidence=candidate.confidence,
                )
                for _ in (0,)
            ),
            confidence=candidate.confidence,
            confidence_model_version=CONFIDENCE_MODEL_V2_B,
            confidence_semantics_version=CONFIDENCE_SEMANTICS_V2,
            confidence_threshold=V2_B_THRESHOLD,
            confidence_threshold_provenance=V2_B_THRESHOLD_PROVENANCE,
            risk_factors=candidate.risk_flags or ("market risk", "model risk"),
            invalidation_conditions=("accepted A4C thesis no longer holds",),
            expected_holding_period=HoldingPeriod.DAYS,
        )
        global_news_available = (
            cycle.news_events_fresh > 0
            and cycle.news_provider_status in {"AVAILABLE", "PARTIAL"}
        )
        risk_context = RiskContext(
            evaluated_at=quote.as_of,
            portfolio=risk_portfolio,
            price=quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            # The cycle already passed the global news acquisition.  Prefer
            # asset-linked evidence when available, but do not turn a missing
            # ticker-specific mapping into a permanent zero-order dead end.
            news_data_available=(
                _candidate_news_available(cycle, candidate.symbol)
                or global_news_available
            ),
            daily_new_trade_count=0,
            recent_idempotency_keys=frozenset(),
            api_state_consistent=True,
            capital_envelope=AuthorizedCapitalEnvelope(
                authorized_capital_eur=config.authorized_capital_eur,
                managed_exposure_eur=managed_exposure,
                # These are alternative packages, not reservations. Capital
                # is reserved only by the broker submission record after a
                # candidate actually reaches the execution adapter. Counting
                # every alternative here made the second candidate look like
                # it had no capital left after the first candidate was merely
                # rejected by RiskManager.
                reserved_capital_eur=Decimal("0"),
            ),
        )
        preflight = evaluate_demo_preflight(
            proposal=proposal,
            portfolio=demo_snapshot,
            eligibility=eligibility,
            quote=quote,
            instrument=instrument,
            kill_switch=KillSwitch(
                active=False,
                reason="preflight",
                clock=_fixed_clock(quote.as_of),
            ),
            now=quote.as_of,
            maximum_age_seconds=config.risk.max_price_age_seconds,
        )
        packages[candidate.symbol] = EtoroDemoSubmissionPackage(
            proposal=proposal,
            risk_context=risk_context,
            preflight=preflight,
            exploratory=candidate.bucket.value == "WATCHLIST",
        )
        record(candidate.symbol, "PACKAGE_READY")
    return packages


def _read_etoro_with_rate_limit_retry[T](operation: Callable[[], T]) -> T:
    """Retry one read after a broker 429 without weakening execution gates."""
    for attempt in range(2):
        try:
            return operation()
        except EtoroApiError as exc:
            if exc.status != 429 or attempt == 1:
                raise
            sleep(2.0)
    raise RuntimeError("unreachable rate-limit retry state")


def build_active_intelligence_orchestrator_report(
    config: ApplicationConfig,
) -> dict[str, object]:
    start = datetime(2026, 8, 28, 9, tzinfo=UTC)
    end = datetime(2026, 8, 28, 18, tzinfo=UTC)
    instruments = _orchestrator_demo_instruments(start)
    bars_by_symbol = {
        instrument.symbol: _orchestrator_demo_bars(instrument, start=start, end=end)
        for instrument in instruments
    }
    news_engine = GlobalNewsIntelligenceEngine(
        NewsFeedProvider(items=_orchestrator_demo_news(start))
    )
    portfolio = PortfolioSnapshot(
        as_of=start,
        currency=Currency.EUR,
        cash=Decimal("170"),
        positions=(
            Position(
                position_id="shadow-aapl",
                instrument_id=1001,
                symbol="AAPL",
                settlement_type=SettlementType.REAL,
                units=Decimal("0.15"),
                average_entry_price=Decimal("190"),
                market_price=Decimal("200"),
            ),
        ),
    )
    audit_store = default_active_intelligence_audit_store()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        news_engine=news_engine, audit_store=audit_store
    )
    records = tuple(
        record
        for scheduled_at in orchestrator.schedule(start=start, end=end)
        if (
            record := orchestrator.run_if_new_bar_cycle(
                scheduled_at=scheduled_at,
                instruments=instruments,
                bars_by_symbol=bars_by_symbol,
                portfolio=portfolio.model_copy(update={"as_of": scheduled_at}),
                timeframe=TimeFrame.ONE_HOUR,
                shadow_capital=Decimal("200"),
            )
        )
        is not None
    )
    unavailable = AegisActiveIntelligenceOrchestrator(
        news_engine=GlobalNewsIntelligenceEngine(NewsFeedProvider(unavailable=True)),
        audit_store=ActiveIntelligenceAuditStore(SqliteRecordStore(Path(":memory:"))),
    ).run_if_new_bar_cycle(
        scheduled_at=start,
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        portfolio=portfolio,
        timeframe=TimeFrame.ONE_HOUR,
        shadow_capital=Decimal("200"),
    )
    if unavailable is None:
        raise RuntimeError("offline unavailable fixture did not produce a cycle")
    if not records:
        persisted_cycles = audit_store.cycles()
        return {
            "status": "NO_CYCLE",
            "phase": "STEP_9_0E_ACTIVE_INTELLIGENCE_ORCHESTRATOR",
            "broker_write": False,
            "broker_write_calls": 0,
            "demo_execution_enabled": config.etoro_demo_execution_enabled,
            "real_execution_available": False,
            "last_successful_cycle": (persisted_cycles[-1] if persisted_cycles else None),
            "reason": "NO_NEW_CAUSALLY_COMPLETED_1H_BAR",
        }
    return {
        "status": "ACTIVE_INTELLIGENCE_ORCHESTRATOR_READY",
        "phase": "STEP_9_0E_ACTIVE_INTELLIGENCE_ORCHESTRATOR",
        "orchestrator_architecture": (
            "CLOCK T",
            "ingest latest causal market state",
            "ingest latest causal news/events",
            "update GLOBAL_RISK_CONTEXT",
            "update per-asset NEWS_CONTEXT",
            "run ActiveMarketScanner",
            "monitor existing positions",
            "detect meaningful state changes",
            "persist immutable audit cycle",
        ),
        "cadence": {
            "default_minutes": 10,
            "meaning": "scan cadence; does not imply trading cadence",
            "one_hour_market_bar_handling": (
                "same 1H bar may be reused across 10-minute cycles; no fabricated bar"
            ),
        },
        "synthetic_full_day_timeline": tuple(_timeline_payload(record) for record in records),
        "example_cycle_output": records[0].model_dump(mode="json"),
        "provider_unavailable_example": unavailable.model_dump(mode="json"),
        "audit_model": {
            "store": "work/active-intelligence-cycles.sqlite3",
            "record_kind": "active-intelligence-cycle",
            "immutable_fields": (
                "cycle_id",
                "scheduled_at",
                "market_data_timestamp",
                "news_cutoff_timestamp",
                "scanner_result",
                "global_risk_context",
                "broker_write_calls",
            ),
        },
        "real_provider_integration_points": {
            "market": ("Alpaca", "eToro read-only", "future Massive/other provider"),
            "news": ("Alpha Vantage", "future Massive News", "official macro feeds"),
            "remaining_blockers": (
                "Windows validation of fresh 1H market data",
                "one real read-only Alpha Vantage news probe",
                "persistent scheduled runner for prospective shadow mode",
            ),
        },
        "euro_200_context": {
            "shadow_capital": "200",
            "available_simulated_cash": str(records[0].available_simulated_cash),
            "existing_exposure": str(records[0].existing_exposure),
            "allocation_diagnostics": records[0].allocation_diagnostics,
        },
        "safety": {
            "news_can_trigger_trade": False,
            "riskmanager_changed": False,
            "strategy_thresholds_changed": False,
            "broker_write_calls": sum(record.broker_write_calls for record in records),
            "demo_execution_enabled": config.etoro_demo_execution_enabled,
            "real_execution_available": False,
        },
        "broker_write": False,
        "broker_write_calls": sum(record.broker_write_calls for record in records),
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def _timeline_payload(record: ActiveIntelligenceCycleRecord) -> dict[str, object]:
    return {
        "time": record.scheduled_at.strftime("%H:%M"),
        "change": record.change_classification.value,
        "market_data_timestamp": (
            None
            if record.market_data_timestamp is None
            else record.market_data_timestamp.isoformat()
        ),
        "fresh_news_events": record.fresh_news_events,
        "duplicate_events_ignored": record.duplicate_events_ignored,
        "material_events": record.material_events,
        "top_opportunities": record.top_opportunities,
        "watchlist": record.watchlist,
        "no_trade": record.no_trade,
        "rejected": record.rejected,
        "data_health": record.data_health_state.value,
        "positions_monitored": record.positions_monitored,
        "decision_changes": record.decision_change_events,
        "broker_write_calls": record.broker_write_calls,
    }


def _orchestrator_demo_news(start: datetime) -> tuple[RawNewsItem, ...]:
    return (
        _raw_news(
            "Apple raises guidance after product launch", start + timedelta(hours=1, minutes=20)
        ),
        _raw_news(
            "Federal Reserve signals inflation risk and possible rate hike",
            start + timedelta(hours=1, minutes=30),
            source="Federal Reserve",
            quality=NewsSourceQuality.PRIMARY_OFFICIAL,
        ),
        _raw_news(
            "Federal Reserve signals inflation risk and possible rate hike",
            start + timedelta(hours=1, minutes=35),
            source="Reuters",
        ),
        _raw_news(
            "SEC crypto regulation creates Bitcoin token uncertainty",
            start + timedelta(hours=2, minutes=30),
            source="SEC",
            quality=NewsSourceQuality.REGULATORY_GOVERNMENT,
        ),
        _raw_news(
            "Apple faces legal lawsuit over platform fees", start + timedelta(hours=3, minutes=10)
        ),
    )


def _raw_news(
    headline: str,
    published_at: datetime,
    *,
    source: str = "Fixture Source",
    quality: NewsSourceQuality = NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
) -> RawNewsItem:
    return RawNewsItem(
        headline=headline,
        source=source,
        published_at=published_at,
        source_quality=quality,
        geographic_scope="US",
    )


def _orchestrator_demo_instruments(as_of: datetime) -> tuple[UniversalInstrument, ...]:
    return (
        _instrument("AAPL", AssetClass.EQUITY, "1001", as_of, "Apple"),
        _instrument("SPY", AssetClass.ETF, "3001", as_of, "SPY"),
        _instrument("BTC", AssetClass.CRYPTO, "4001", as_of, "Bitcoin"),
    )


def _instrument(
    symbol: str,
    asset_class: AssetClass,
    broker_instrument_id: str,
    as_of: datetime,
    display_name: str,
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id=broker_instrument_id,
        symbol=symbol,
        display_name=display_name,
        asset_class=asset_class,
        currency=Currency.USD,
        exchange="TEST",
        market_status=(
            MarketStatus.CONTINUOUS_24_7 if asset_class is AssetClass.CRYPTO else MarketStatus.OPEN
        ),
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=as_of,
    )


def _orchestrator_demo_bars(
    instrument: UniversalInstrument,
    *,
    start: datetime,
    end: datetime,
) -> tuple[MarketBar, ...]:
    warmup_start = start - timedelta(hours=65)
    rows: list[MarketBar] = []
    price = Decimal("100")
    current = warmup_start
    while current <= end:
        drift = Decimal("1.001") if current.minute == 0 else Decimal("1")
        close = (price * drift).quantize(Decimal("0.0001"))
        rows.append(
            MarketBar(
                instrument=instrument,
                timestamp=current,
                timeframe=TimeFrame.ONE_HOUR,
                open=price,
                high=max(price, close) * Decimal("1.01"),
                low=min(price, close) * Decimal("0.99"),
                close=close,
                volume=Decimal("100000"),
                currency=Currency.USD,
                source="synthetic-offline-fixture",
                data_quality=FeatureQuality.GOOD,
            )
        )
        price = close
        current += timedelta(hours=1)
    return tuple(rows)
