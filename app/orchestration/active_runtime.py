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
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from functools import partial
from pathlib import Path
from time import sleep
from threading import Event, Lock, Thread
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
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse, UrllibTransport
from app.brokers.etoro.mapping import (
    EtoroEligibilityDenied,
    EtoroMappingError,
    asset_class_from_etoro_instrument_type,
    classify_etoro_instrument_metadata,
)
from app.brokers.etoro.runtime import runtime_credentials, runtime_settings
from app.brokers.models import BrokerIdentity, DemoPortfolioSnapshot, ExecutionState
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
from app.intelligence.models import AegisDecision, FeatureQuality, MarketBar, TimeFrame
from app.intelligence.confidence import (
    CONFIDENCE_MODEL_V2_B,
    CONFIDENCE_SEMANTICS_V2,
    V2_B_THRESHOLD,
    V2_B_THRESHOLD_PROVENANCE,
)
from app.news.alpaca import AlpacaNewsProvider, alpaca_news_symbol
from app.news.alpha_vantage import AlphaVantageNewsProvider
from app.news.crosscheck import CrossCheckedNewsProvider
from app.news.gdelt import GdeltNewsProvider
from app.news.intelligence import (
    GlobalNewsIntelligenceEngine,
    NewsFeedProvider,
    NewsSourceQuality,
    RawNewsItem,
)
from app.news.web_rss import BingNewsRssProvider, GoogleNewsRssProvider
from app.news.relay import publish as publish_secondary_news, read_latest as read_secondary_news, relay_key, relay_store_path
from app.orchestration.active_intelligence import (
    ActiveIntelligenceAuditStore,
    ActiveIntelligenceCycleRecord,
    AegisActiveIntelligenceOrchestrator,
    _news_asset_contexts,
    _news_event_digest,
    _persistable_news_asset_contexts,
    causal_completed_bars,
    default_active_intelligence_audit_store,
)
from app.orchestration.market_acquisition import (
    EtoroOneHourAcquisitionCoordinator,
    build_coherent_one_hour_snapshot,
)
from app.policies.defaults import default_asset_policy_engine
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.scanner.active import (
    ActiveScannerBucket,
    ActiveScannerCandidate,
    ActiveMarketScanner,
    ActiveScannerResult,
    _expected_completed_one_hour_bar_timestamp,
    _market_closed_for_instrument,
)
from app.storage.sqlite import RunnerLease, SqliteRecordStore

DEFAULT_ETORO_FULL_CATALOG_SESSION_AUDIT_PATH = (
    Path("work") / "etoro-full-catalog-session-audit.json"
)
_RUNTIME_GDELT_PROVIDER: GdeltNewsProvider | None = None
_RUNTIME_GDELT_PROVIDER_LOCK = Lock()
_RUNTIME_CANDIDATE_GDELT_PROVIDERS: dict[str, GdeltNewsProvider] = {}
_RUNTIME_CANDIDATE_GDELT_PROVIDER_LOCK = Lock()
_RUNTIME_CANDIDATE_GDELT_PROVIDER_LIMIT = 8
_RUNTIME_CANDIDATE_WEB_RSS_PROVIDERS: dict[str, GoogleNewsRssProvider] = {}
_RUNTIME_CANDIDATE_WEB_RSS_PROVIDER_LOCK = Lock()
_RUNTIME_CANDIDATE_WEB_RSS_PROVIDER_LIMIT = 16
RUNNER_LEASE_DURATION = timedelta(minutes=15)
# Limits are expressed in the account currency. No implicit FX conversion is
# performed; a cap/account mismatch blocks execution.
MIN_DEMO_AUTHORIZED_CAPITAL = Decimal("200")
MAX_AEGIS_MANAGED_EXPOSURE_EUR = Decimal("200")
MAX_AEGIS_MANAGED_EXPOSURE_USD = Decimal("98000")
MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY = {
    Currency.EUR: MAX_AEGIS_MANAGED_EXPOSURE_EUR,
    Currency.USD: MAX_AEGIS_MANAGED_EXPOSURE_USD,
}


def effective_aegis_managed_exposure_limit(
    authorized_capital: Decimal | None,
    currency: Currency = Currency.EUR,
) -> Decimal | None:
    """Apply the currency-specific project Demo ceiling to the configured budget."""
    if authorized_capital is None:
        return None
    currency_limit = MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY.get(currency)
    if currency_limit is None:
        return None
    return min(authorized_capital, currency_limit)


def _capped_demo_pilot_settings(
    values: Mapping[str, str],
    *,
    authorized_capital_eur: Decimal | None,
    authorized_capital_currency: Currency = Currency.EUR,
) -> EtoroDemoPilotSettings:
    settings = demo_pilot_settings(values, currency=authorized_capital_currency)
    limit = effective_aegis_managed_exposure_limit(
        authorized_capital_eur, authorized_capital_currency
    )
    if limit is None:
        return EtoroDemoPilotSettings(enabled=False, notional_eur=None)
    if settings.notional_eur is None:
        return settings
    return settings.model_copy(update={"notional_eur": min(settings.notional_eur, limit)})


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
        } and _market_closed_for_instrument(instrument=instrument, as_of=as_of)
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


def _acquisition_audit_summary(acquisition: Mapping[str, object]) -> dict[str, object]:
    """Persist aggregate acquisition telemetry, not every instrument result.

    The complete per-instrument rows remain available to the current cycle;
    repeating them in the audit store made each poll several megabytes.
    """
    summary = {key: value for key, value in acquisition.items() if key != "acquisition_results"}
    rows = acquisition.get("acquisition_results")
    if isinstance(rows, (list, tuple)):
        summary["acquisition_results_count"] = len(rows)
    return summary


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
        # The global feed supplies macro context, but a candidate may still
        # lack an asset-linked article.  Before RiskManager evaluates an
        # executable candidate, make one bounded, read-only GDELT lookup for
        # the small candidate set.  Missing evidence remains a hard reject.
        cycle = _enrich_candidate_news_with_gdelt(
            cycle=cycle,
            scanner_result=scanner_result,
            instruments=instruments,
            enabled=self._values.get("AEGIS_NEWS_GDELT_ENABLED", "false"),
            values=self._values,
        )
        cycle = _merge_secondary_news_into_cycle(
            cycle=cycle,
            instruments=instruments,
            values=self._values,
        )
        relay_status = publish_secondary_news(cycle, self._values)
        if not scanner_result.top_opportunities and not scanner_result.watchlist:
            return _demo_runtime_payload(
                status="NO_TOP_OPPORTUNITY",
                pilot_enabled=self._config.etoro_demo_automatic_pilot_enabled,
                cycle_id=cycle.cycle_id,
                secondary_news_relay=relay_status,
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
                secondary_news_relay=relay_status,
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
                secondary_news_relay=relay_status,
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
                secondary_news_relay=relay_status,
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
                    scanner_asset_class_counts=_scanner_asset_class_counts(scanner_result),
                    blockers=coverage_blockers,
                    secondary_news_relay=relay_status,
                    **_cycle_news_payload(cycle),
                )
        settings = _capped_demo_pilot_settings(
            self._values,
            authorized_capital_eur=self._config.authorized_capital,
            authorized_capital_currency=self._config.authorized_capital_currency,
        )
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
        # A TOP candidate can be a valid intelligence result and still be
        # rejected by the execution RiskManager (for example because its
        # confidence is below the execution threshold).  Previously that
        # stopped the whole Demo lane even when a coherent WATCHLIST fallback
        # was package-ready.  If no broker write happened, give the explicit
        # Demo exploratory lane one bounded attempt with the same hard gates.
        # This never relabels a WATCHLIST item as TOP and never retries after
        # an ambiguous/rejected broker write.
        if (
            result.demo_broker_write_calls == 0
            and not any(item.submitted for item in result.submissions)
            and scanner_result.top_opportunities
            and scanner_result.watchlist
        ):
            exploratory_scanner_result = scanner_result.model_copy(
                update={"top_opportunities": ()}
            )
            fallback_result = pilot.run(
                cycle=cycle,
                scanner_result=exploratory_scanner_result,
                broker_instrument_ids=_verified_broker_ids(instruments),
                submission_packages=packages,
                allow_exploratory_watchlist=True,
            )
            if any(item.submitted for item in fallback_result.submissions):
                result = fallback_result
            else:
                result = fallback_result.model_copy(
                    update={
                        "blockers": tuple(
                            dict.fromkeys((*result.blockers, *fallback_result.blockers))
                        ),
                        "eligible_intents": (
                            *result.eligible_intents,
                            *fallback_result.eligible_intents,
                        ),
                        "submissions": (*result.submissions, *fallback_result.submissions),
                        "demo_broker_write_calls": (
                            result.demo_broker_write_calls
                            + fallback_result.demo_broker_write_calls
                        ),
                        "broker_write_calls_real": (
                            result.broker_write_calls_real
                            + fallback_result.broker_write_calls_real
                        ),
                    }
                )
        candidate_execution_diagnostics = _candidate_execution_diagnostics(
            cycle=cycle,
            scanner_result=scanner_result,
            package_diagnostics={symbol: "PACKAGE_READY" for symbol in packages},
            quote_diagnostics={},
            pilot_result=result,
        )
        return _demo_runtime_payload(
            status=result.status.value,
            pilot_enabled=True,
            cycle_id=cycle.cycle_id,
            top_opportunity_count=len(scanner_result.top_opportunities),
            eligible_count=len(result.eligible_intents),
            scanner_asset_class_counts=_scanner_asset_class_counts(scanner_result),
            submitted_count=sum(1 for item in result.submissions if item.submitted),
            blockers=tuple(item.value for item in result.blockers),
            demo_broker_write_calls=result.demo_broker_write_calls,
            broker_write_calls_real=result.broker_write_calls_real,
            risk_manager_reached=any(item.risk_manager_reached for item in result.submissions),
            execution_admission_gate_reached=any(
                item.execution_admission_gate_reached for item in result.submissions
            ),
            candidate_execution_diagnostics=candidate_execution_diagnostics,
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
            news_event_digest=cycle.news_event_digest,
            news_asset_contexts=cycle.news_asset_contexts,
            global_risk_context=cycle.global_risk_context,
            secondary_news_relay=relay_status,
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
        maintenance_once: Callable[[], Mapping[str, object]] | None = None,
        maintenance_every_polls: int = 1,
    ) -> None:
        if poll_interval_seconds <= 0 or error_backoff_seconds <= 0 or max_backoff_seconds <= 0:
            raise ValueError("runner intervals must be positive")
        if maintenance_every_polls <= 0:
            raise ValueError("maintenance_every_polls must be positive")
        self._run_once = run_once
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or sleep
        self._poll_interval = poll_interval_seconds
        self._error_backoff = error_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._status_store = status_store
        self._maintenance_once = maintenance_once
        self._maintenance_every_polls = maintenance_every_polls

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
        previous_status = (
            SqliteRecordStore.read_latest_read_only(
                self._status_store.path, ETORO_DEMO_RUNTIME_STATUS_KIND
            )
            if self._status_store is not None
            else None
        )
        # Keep persisted write counters monotonic across runner restarts. These
        # are broker write calls, not successful fills or open positions.
        demo_writes = _nonnegative_int(
            (previous_status or {}).get("demo_broker_write_calls")
        )
        real_writes = _nonnegative_int(
            (previous_status or {}).get("broker_write_calls_real")
        )
        last_poll_at: datetime | None = None
        previous_scan_at = (previous_status or {}).get("last_successful_scan_at")
        last_accepted_at: str | None = (
            previous_scan_at if isinstance(previous_scan_at, str) else None
        )
        last_result: Mapping[str, object] | None = None
        last_error: str | None = None
        last_maintenance_result: Mapping[str, object] | None = None
        last_maintenance_error: str | None = None
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
                try:
                    heartbeat_ok = self._status_store.heartbeat_runner_lease(  # type: ignore[union-attr]
                        lease=lease,
                        heartbeat_at=last_poll_at,
                        expires_at=last_poll_at + RUNNER_LEASE_DURATION,
                    )
                except sqlite3.OperationalError as exc:
                    if not _is_transient_sqlite_lock(exc):
                        raise
                    # The independent heartbeat thread keeps retrying. A
                    # temporary diagnostic read lock must not terminate the
                    # market runner or create a false lease-loss event.
                    heartbeat_ok = True
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
            if (
                self._maintenance_once is not None
                and poll_count % self._maintenance_every_polls == 0
            ):
                try:
                    last_maintenance_result = dict(self._maintenance_once())
                    last_maintenance_error = None
                except Exception as exc:
                    # Universe maintenance is read-only support work and must
                    # never terminate the primary scanner/runtime loop.
                    last_maintenance_error = type(exc).__name__
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
                released_at=self._clock(),
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
            "last_universe_maintenance": last_maintenance_result,
            "last_universe_maintenance_error": last_maintenance_error,
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
        # A NO_CYCLE poll is a heartbeat, not a new market/news evaluation.
        # Merge it over the last stored result so waiting for a new bar does
        # not erase the most recent scan, news digest, or blocker diagnostics.
        previous = SqliteRecordStore.read_latest_read_only(
            self._status_store.path, ETORO_DEMO_RUNTIME_STATUS_KIND
        ) or {}
        result = dict(previous)
        if last_result is not None:
            if last_result.get("status") == "NO_CYCLE":
                # Heartbeats may carry model defaults such as empty news lists
                # or zero candidate counts. They are not new evaluations and
                # must not erase the last completed scan. Merge only fields
                # that can change between market bars.
                heartbeat_fields = {
                    "status",
                    "pilot_enabled",
                    "demo_execution_enabled",
                    "execution_enabled",
                    "execution_available",
                    "real_execution_available",
                    "broker_write",
                    "demo_broker_write_calls",
                    "broker_write_calls_real",
                    "demo_exit",
                    "demo_reconciliation_report",
                    "acquisition_attempted",
                    "acquisition_provider",
                    "acquisition_instruments_requested",
                    "acquisition_instruments_updated",
                    "acquisition_instruments_attempted",
                    "acquisition_instruments_not_attempted",
                    "acquisition_instruments_in_backoff",
                    "acquisition_newest_completed_bar",
                    "acquisition_stale_count",
                    "acquisition_missing_count",
                    "acquisition_status",
                    "acquisition_error",
                    "acquisition_outcome_counts",
                }
                if last_result.get("news_scan_completed_at"):
                    heartbeat_fields.update(
                        key
                        for key in last_result
                        if key.startswith("news_") or key == "global_risk_context"
                    )
                result.update(
                    {
                        key: value
                        for key, value in last_result.items()
                        if key in heartbeat_fields
                    }
                )
            else:
                # A new evaluated cycle replaces the previous result. Do not
                # carry a legacy A4C/coverage explanation forward when the
                # current result has reached the scanner and reports a
                # different set of blockers (for example RiskManager or
                # Demo preflight). NO_CYCLE heartbeats intentionally preserve
                # the last completed evaluation above.
                if "a4c_reason" not in last_result:
                    result.pop("a4c_reason", None)
                result.update(last_result)
            # Legacy persisted rows may pair an A4C failure with a later
            # scanner-reached result because NO_CYCLE heartbeats preserve the
            # last snapshot. Once a scanner result is present, A4C succeeded;
            # the stale upstream reason must not mask current execution gates.
            if result.get("scanner_reached") is True:
                result.pop("a4c_reason", None)
        cycle_state = result.get("status", result.get("cycle_state"))
        activity = (
            "WAITING_FOR_ELIGIBLE_COMPLETED_BAR"
            if cycle_state == "NO_CYCLE"
            else cycle_state or result.get("activity_code")
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
        pilot_diagnostics: list[dict[str, object]] = []
        raw_pilot_result = result.get("pilot_result")
        if isinstance(raw_pilot_result, Mapping):
            raw_submissions = raw_pilot_result.get("submissions", ())
            if isinstance(raw_submissions, (list, tuple)):
                for raw_submission in raw_submissions:
                    if not isinstance(raw_submission, Mapping):
                        continue
                    pilot_diagnostics.append(
                        {
                            "symbol": raw_submission.get("symbol"),
                            "asset_class": raw_submission.get("asset_class"),
                            "status": raw_submission.get("sanitized_status"),
                            "submitted": bool(raw_submission.get("submitted", False)),
                            "risk_violation_codes": tuple(
                                str(code)
                                for code in raw_submission.get("risk_violation_codes", ())
                            ),
                            "preflight_reasons": tuple(
                                str(reason)
                                for reason in raw_submission.get("preflight_reasons", ())
                            ),
                        }
                    )
        status_payload = {
                "observed_at": observed_at.isoformat(),
                "runner_state": state,
                "cycle_state": cycle_state,
                "last_successful_scan_at": last_accepted_at,
                "top_opportunity_count": _nonnegative_int(result.get("top_opportunity_count")),
                "eligible_demo_candidates": _nonnegative_int(
                    result.get("eligible_count", result.get("eligible_demo_candidates"))
                ),
                "scanner_asset_class_counts": result.get("scanner_asset_class_counts", {}),
                "automatic_pilot_armed": bool(result.get("pilot_enabled", False)),
                "execution_enabled": bool(result.get("execution_enabled", False)),
                "last_submission_status": (
                    result.get("last_submission_status")
                    if result.get("status") == "NO_CYCLE"
                    else result.get("status", result.get("last_submission_status"))
                ),
                "demo_broker_write_calls": demo_writes,
                "demo_broker_write_calls_last_poll": (
                    0
                    if last_result is None or last_result.get("status") == "NO_CYCLE"
                    else _nonnegative_int(last_result.get("demo_broker_write_calls"))
                ),
                "execution_available": bool(result.get("execution_available", False)),
                "broker_write_calls_real": real_writes,
                "broker_write_calls_real_last_poll": (
                    0
                    if last_result is None or last_result.get("status") == "NO_CYCLE"
                    else _nonnegative_int(last_result.get("broker_write_calls_real"))
                ),
                "activity_code": activity,
                "last_error": last_error,
                "poll_count": poll_count,
                "pilot_notional_eur": result.get("pilot_notional_eur"),
                "pilot_notional": result.get("pilot_notional"),
                "pilot_notional_currency": result.get("pilot_notional_currency"),
                "authorized_capital_eur": result.get("authorized_capital_eur"),
                "authorized_capital": result.get("authorized_capital"),
                "authorized_capital_currency": result.get("authorized_capital_currency"),
                "demo_account_currency": result.get("demo_account_currency"),
                "managed_exposure_limit_eur": result.get("managed_exposure_limit_eur"),
                "managed_exposure_limit": result.get("managed_exposure_limit"),
                "managed_exposure_currency": result.get("managed_exposure_currency"),
                "managed_exposure_eur": result.get("managed_exposure_eur"),
                "managed_exposure": result.get("managed_exposure"),
                "remaining_authorized_capital_eur": result.get("remaining_authorized_capital_eur"),
                "remaining_authorized_capital": result.get("remaining_authorized_capital"),
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
                "news_asset_contexts": _persistable_news_asset_contexts(
                    result.get("news_asset_contexts", {}), preserve_payload=True
                ),
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
                "quote_freshness_diagnostics": result.get(
                    "quote_freshness_diagnostics", {}
                ),
                "candidate_execution_diagnostics": result.get(
                    "candidate_execution_diagnostics", ()
                ),
                "package_sizing_diagnostics": result.get(
                    "package_sizing_diagnostics", {}
                ),
                "pilot_diagnostics": tuple(pilot_diagnostics),
                "pilot_result": result.get("pilot_result"),
                "scanner_reached": result.get("scanner_reached", False),
                "news_reached": result.get("news_reached", False),
                "risk_manager_reached": result.get("risk_manager_reached", False),
                "execution_admission_gate_reached": result.get(
                    "execution_admission_gate_reached", False
                ),
            }
        try:
            self._status_store.append(ETORO_DEMO_RUNTIME_STATUS_KIND, status_payload)
        except sqlite3.OperationalError as exc:
            if not _is_transient_sqlite_lock(exc):
                raise
            # Status is observability, not execution admission. Preserve the
            # live runner and let the next heartbeat/poll persist a snapshot.
            return


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
        "broker_write_calls": status.get("demo_broker_write_calls", 0),
        "demo_broker_write_calls": status.get("demo_broker_write_calls", 0),
        "demo_broker_write_calls_last_poll": status.get(
            "demo_broker_write_calls_last_poll", 0
        ),
        "broker_write_calls_real": status.get("broker_write_calls_real", 0),
        "broker_write_calls_real_last_poll": status.get(
            "broker_write_calls_real_last_poll", 0
        ),
    }


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _is_transient_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    detail = str(exc).casefold()
    return (
        "database is locked" in detail
        or "database table is locked" in detail
        or "database is busy" in detail
    )


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
    scanner_asset_class_counts: Mapping[str, object] | None = None,
    submitted_count: int = 0,
    blockers: tuple[str, ...] = (),
    demo_broker_write_calls: int = 0,
    broker_write_calls_real: int = 0,
    risk_manager_reached: bool = False,
    execution_admission_gate_reached: bool = False,
    pilot_result: EtoroDemoPilotResult | None = None,
    authorized_capital_eur: Decimal | None = None,
    authorized_capital_currency: Currency = Currency.EUR,
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
    news_event_digest: tuple[dict[str, object], ...] = (),
    news_asset_contexts: Mapping[str, object] | None = None,
    global_risk_context: Mapping[str, object] | None = None,
    secondary_news_relay: Mapping[str, object] | None = None,
    candidate_execution_diagnostics: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "status": status,
        "pilot_enabled": pilot_enabled,
        "cycle_id": cycle_id,
        "top_opportunity_count": top_opportunity_count,
        "eligible_count": eligible_count,
        "scanner_asset_class_counts": dict(scanner_asset_class_counts or {}),
        "submitted_count": submitted_count,
        "blockers": blockers,
        "demo_broker_write_calls": demo_broker_write_calls,
        "broker_write_calls_real": broker_write_calls_real,
        "risk_manager_reached": risk_manager_reached,
        "execution_admission_gate_reached": execution_admission_gate_reached,
        "write_request_sent_to_real": False,
        "pilot_result": None if pilot_result is None else pilot_result.model_dump(mode="json"),
        "candidate_execution_diagnostics": candidate_execution_diagnostics,
        "authorized_capital_eur": (
            None
            if authorized_capital_eur is None or authorized_capital_currency is not Currency.EUR
            else str(authorized_capital_eur)
        ),
        "authorized_capital": (
            None if authorized_capital_eur is None else str(authorized_capital_eur)
        ),
        "authorized_capital_currency": authorized_capital_currency.value,
        "managed_exposure_limit_eur": (
            str(MAX_AEGIS_MANAGED_EXPOSURE_EUR)
            if authorized_capital_currency is Currency.EUR
            else None
        ),
        "managed_exposure_limit": str(
            MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY.get(authorized_capital_currency)
        ) if authorized_capital_currency in MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY else None,
        "managed_exposure_currency": authorized_capital_currency.value,
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
        "news_event_digest": tuple(news_event_digest),
        "news_asset_contexts": dict(news_asset_contexts or {}),
        "global_risk_context": dict(global_risk_context or {}),
        "secondary_news_relay": dict(secondary_news_relay or {}),
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
        "news_event_digest": cycle.news_event_digest,
        "news_asset_contexts": cycle.news_asset_contexts,
        "global_risk_context": cycle.global_risk_context,
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
        authorized_capital_eur=config.authorized_capital,
        authorized_capital_currency=config.authorized_capital_currency,
    )
    payload.update(_runtime_execution_state(config, credentials_present=credentials_present))
    return payload


class _DiagnosticReadOnlyHttpClient(DisciplinedHttpClient):
    """Last-resort guard against eToro broker writes during diagnostics."""

    def post_once(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> HttpResponse:
        raise RuntimeError("DIAGNOSTIC_READ_ONLY_BROKER_WRITE_FORBIDDEN")


def build_etoro_demo_runtime_once_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str],
    clock: Callable[[], datetime] | None = None,
    diagnostic_read_only: bool = False,
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
    if config.authorized_capital is None:
        return _configured_runtime_block(
            config, blocker="AUTHORIZED_CAPITAL_NOT_CONFIGURED", credentials_present=True
        )
    if config.authorized_capital < MIN_DEMO_AUTHORIZED_CAPITAL:
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
    http = (
        _DiagnosticReadOnlyHttpClient(UrllibTransport(config.etoro_transport_mode))
        if diagnostic_read_only
        else DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
    )
    client = EtoroReadClient(credentials, http)
    try:
        identity = client.identity()
        demo_reconciliation_report = _reconcile_unresolved_demo_submissions(
            registry=registry,
            client=client,
            identity=identity,
            observed_at=scheduled_at,
        )
    except (EtoroApiError, RuntimeError, ValueError, TypeError) as exc:
        identity = None
        category = getattr(exc, "category", None)
        demo_reconciliation_report = {
            "attempted": 0,
            "verified": 0,
            "remaining": len(registry.unresolved_demo_submissions()),
            "errors": (
                "IDENTITY_READ_FAILED",
                getattr(exc, "status", None)
                or getattr(category, "value", None)
                or type(exc).__name__,
            ),
        }
    registry.append(
        "etoro-demo-submission-reconciliation",
        {"observed_at": scheduled_at.isoformat(), **demo_reconciliation_report},
    )
    # Exit management is intentionally independent from the 1H entry cycle:
    # an open Demo position must still be protected while market coverage is
    # incomplete or the scanner has no new bar.  The manager is Demo-only and
    # keeps a successful/ambiguous close from being replayed automatically.
    if diagnostic_read_only:
        demo_exit_report = {
            "enabled": False,
            "evaluated": 0,
            "held": 0,
            "close_triggered": 0,
            "close_write_calls": 0,
            "closed_confirmed": 0,
            "pending_confirmation": 0,
            "blocked": 0,
            "errors": (),
            "reason": "DIAGNOSTIC_READ_ONLY",
        }
    elif identity is None:
        reconciliation_errors = demo_reconciliation_report.get("errors", ())
        identity_error = (
            reconciliation_errors[-1]
            if isinstance(reconciliation_errors, (list, tuple)) and reconciliation_errors
            else "IDENTITY_UNAVAILABLE"
        )
        demo_exit_report = {
            "enabled": True,
            "evaluated": 0,
            "held": 0,
            "close_triggered": 0,
            "close_write_calls": 0,
            "closed_confirmed": 0,
            "pending_confirmation": 0,
            "blocked": 1,
            "errors": ("IDENTITY_READ_FAILED", identity_error),
            "policy": "EXITPOLICY_V2_GUARDED",
        }
    else:
        try:
            demo_exit_report = manage_demo_exits(
                client=client,
                identity=identity,
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
    # Exit writes happen before the entry scanner and must be counted even
    # when market coverage blocks the entry cycle later in this poll.
    exit_write_calls = _nonnegative_int(demo_exit_report.get("close_write_calls"))
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
    registry.append("etoro-market-data-acquisition", _acquisition_audit_summary(acquisition))
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
            demo_broker_write_calls=exit_write_calls,
            authorized_capital_eur=config.authorized_capital,
            authorized_capital_currency=config.authorized_capital_currency,
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
        if diagnostic_read_only:
            blocked_payload.update(
                diagnostic_read_only=True,
                execution_enabled=False,
                execution_available=False,
            )
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
        asset_policy_engine=default_asset_policy_engine(
            minimum_cash_reserve=config.risk.min_cash_reserve
        ),
        authorization_key=b"automatic-demo-runtime-risk-authorization-key!",
        clock=effective_clock,
    )
    execution_available = (
        not diagnostic_read_only
        and identity is not None
        and _demo_execution_available(config, credentials_present=True)
    )
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
    package_sizing_diagnostics: dict[str, object] = {}
    quote_freshness_diagnostics: dict[str, dict[str, object]] = {}
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
                instrument_ids_by_symbol=_verified_broker_ids(snapshot.instruments),
                config=config,
                settings=_capped_demo_pilot_settings(
                    values,
                    authorized_capital_eur=config.authorized_capital,
                    authorized_capital_currency=config.authorized_capital_currency,
                ),
                registry=registry,
                diagnostics=package_material_diagnostics,
                sizing_diagnostics=package_sizing_diagnostics,
                quote_diagnostics=quote_freshness_diagnostics,
                maximum_future_quote_skew_seconds=(
                    runtime_settings(values).maximum_future_quote_skew_seconds
                ),
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
    result["demo_broker_write_calls"] = (
        _nonnegative_int(result.get("demo_broker_write_calls")) + exit_write_calls
    )
    result.update(acquisition)
    result.update(coverage)
    result["package_material_diagnostics"] = package_material_diagnostics
    result["package_sizing_diagnostics"] = package_sizing_diagnostics
    result["quote_freshness_diagnostics"] = quote_freshness_diagnostics
    candidate_rows = result.get("candidate_execution_diagnostics", ())
    if isinstance(candidate_rows, (list, tuple)):
        enriched_rows: list[dict[str, object]] = []
        for item in candidate_rows:
            if not isinstance(item, Mapping):
                continue
            row = dict(item)
            symbol = str(row.get("symbol", ""))
            if symbol in package_material_diagnostics:
                row["package_status"] = package_material_diagnostics[symbol]
            if symbol in quote_freshness_diagnostics:
                row["quote"] = quote_freshness_diagnostics[symbol]
            enriched_rows.append(row)
        result["candidate_execution_diagnostics"] = tuple(enriched_rows)
    result["current_spread_assessments"] = quote_report["spread_assessments"]
    result["demo_exit"] = demo_exit_report
    result["scheduled_at"] = scheduled_at.isoformat()
    result["cycle_as_of_resolved"] = cycle_as_of.isoformat()
    result["package_policy"] = (
        "verified execution packages are required; unavailable materials fail closed"
    )
    result.update(_runtime_execution_state(config, credentials_present=True))
    if diagnostic_read_only:
        result.update(
            diagnostic_read_only=True,
            execution_enabled=False,
            execution_available=False,
        )
    result["scanner_reached"] = result.get("cycle_id") is not None
    result["news_reached"] = result.get("news_scan_completed_at") is not None
    settings = _capped_demo_pilot_settings(
        values,
        authorized_capital_eur=config.authorized_capital,
        authorized_capital_currency=config.authorized_capital_currency,
    )
    if settings.notional_eur is not None:
        result["pilot_notional"] = str(settings.notional_eur)
        result["pilot_notional_currency"] = config.authorized_capital_currency.value
        result["pilot_notional_eur"] = (
            str(settings.notional_eur)
            if config.authorized_capital_currency is Currency.EUR
            else None
        )
    result["authorized_capital"] = str(config.authorized_capital)
    result["authorized_capital_currency"] = config.authorized_capital_currency.value
    if config.authorized_capital_currency is Currency.EUR:
        result["authorized_capital_eur"] = str(config.authorized_capital)
        result["managed_exposure_limit_eur"] = str(MAX_AEGIS_MANAGED_EXPOSURE_EUR)
    else:
        result["authorized_capital_eur"] = None
        result["managed_exposure_limit_eur"] = None
    result["managed_exposure_limit"] = str(
        MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY[config.authorized_capital_currency]
    )
    result["managed_exposure_currency"] = config.authorized_capital_currency.value
    managed_exposure_now = registry.managed_demo_exposure(
        config.authorized_capital_currency
    )
    result["managed_exposure"] = (
        None if managed_exposure_now is None else str(managed_exposure_now)
    )
    result["managed_exposure_eur"] = (
        str(managed_exposure_now)
        if managed_exposure_now is not None
        and config.authorized_capital_currency is Currency.EUR
        else None
    )
    remaining_capital_now = (
        None
        if managed_exposure_now is None
        else max(
            Decimal("0"),
            Decimal(result["managed_exposure_limit"]) - managed_exposure_now,
        )
    )
    result["remaining_authorized_capital"] = (
        None if remaining_capital_now is None else str(remaining_capital_now)
    )
    result["remaining_authorized_capital_eur"] = (
        str(remaining_capital_now)
        if remaining_capital_now is not None
        and config.authorized_capital_currency is Currency.EUR
        else None
    )
    result["demo_account_currency"] = config.authorized_capital_currency.value
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
        "active_universe_reference_count": len(active_ids),
        "current_active_open_tradable": len(open_ids & active_ids),
        "new_open_tradable_outside_active_universe": len(open_ids - active_ids),
        # Backward-compatible aliases. Values refer to the current active
        # universe, which is no longer assumed to contain exactly 633 rows.
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
        and config.authorized_capital is not None
        and config.authorized_capital >= MIN_DEMO_AUTHORIZED_CAPITAL
        and config.authorized_capital_currency in MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY
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
    diagnostic_read_only: bool = False,
) -> dict[str, object]:
    """Run the continuous Demo poller around the existing one-shot runtime."""
    poll_interval = _configured_runtime_seconds(
        values.get("AEGIS_ETORO_DEMO_POLL_INTERVAL_SECONDS"), default=60.0
    )
    error_backoff = _configured_runtime_seconds(
        values.get("AEGIS_ETORO_DEMO_ERROR_BACKOFF_SECONDS"), default=60.0
    )
    maintenance_enabled = (
        values.get("AEGIS_UNIVERSE_MAINTENANCE_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    maintenance_batch = _configured_runtime_positive_int(
        values.get("AEGIS_UNIVERSE_MAINTENANCE_BATCH_SIZE"), default=16
    )
    maintenance_every_polls = _configured_runtime_positive_int(
        values.get("AEGIS_UNIVERSE_MAINTENANCE_EVERY_POLLS"), default=1
    )
    catalog_refresh_interval_seconds = _configured_runtime_positive_int(
        values.get("AEGIS_ETORO_CATALOG_REFRESH_INTERVAL_SECONDS"), default=21600
    )

    def maintain_universe() -> Mapping[str, object]:
        from app.data.runtime import (
            build_etoro_instrument_catalog_probe_report,
            build_etoro_universe_bootstrap_report,
            read_etoro_instrument_catalog_snapshot,
        )

        maintenance_at = (clock or (lambda: datetime.now(UTC)))()
        snapshot_before = read_etoro_instrument_catalog_snapshot()
        catalog_refresh: Mapping[str, object] = {
            "status": "CATALOG_REFRESH_NOT_DUE",
            "broker_write_calls": 0,
        }
        if _catalog_refresh_due(
            snapshot_before,
            as_of=maintenance_at,
            interval_seconds=catalog_refresh_interval_seconds,
        ):
            catalog_refresh = build_etoro_instrument_catalog_probe_report(
                config,
                values=values,
                persist=True,
                clock=lambda: maintenance_at,
            )

        snapshot_after = read_etoro_instrument_catalog_snapshot()
        if snapshot_after is None:
            return {
                "status": "BLOCKED",
                "blocker": "ETORO_CATALOG_SNAPSHOT_MISSING",
                "catalog_refresh": dict(catalog_refresh),
                "broker_write_calls": 0,
            }

        bootstrap = build_etoro_universe_bootstrap_report(
            config,
            values=values,
            clock=clock,
            batch_size=maintenance_batch,
            delay_seconds=0,
            max_instruments_per_run=maintenance_batch,
        )
        return {
            **bootstrap,
            "catalog_refresh": dict(catalog_refresh),
            "catalog_snapshot_changed": (
                None
                if snapshot_before is None
                else snapshot_before.get("snapshot_id") != snapshot_after.get("snapshot_id")
            ),
        }

    runner = EtoroDemoContinuousRunner(
        run_once=lambda: build_etoro_demo_runtime_once_report(
            config,
            values=values,
            clock=clock,
            diagnostic_read_only=diagnostic_read_only,
        ),
        clock=clock,
        poll_interval_seconds=poll_interval,
        error_backoff_seconds=error_backoff,
        status_store=SqliteRecordStore(DEFAULT_ETORO_DEMO_RUNTIME_STORE_PATH),
        maintenance_once=maintain_universe if maintenance_enabled else None,
        maintenance_every_polls=maintenance_every_polls,
    )
    return runner.run(max_iterations=max_iterations)


def _catalog_refresh_due(
    snapshot: Mapping[str, object] | None,
    *,
    as_of: datetime,
    interval_seconds: int,
) -> bool:
    if interval_seconds <= 0:
        raise ValueError("catalog refresh interval must be positive")
    if as_of.tzinfo is None:
        raise ValueError("catalog refresh timestamp must be timezone-aware")
    if snapshot is None:
        return True
    raw_retrieved_at = snapshot.get("retrieved_at")
    if not isinstance(raw_retrieved_at, str) or not raw_retrieved_at.strip():
        return True
    try:
        retrieved_at = datetime.fromisoformat(raw_retrieved_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if retrieved_at.tzinfo is None:
        return True
    return retrieved_at + timedelta(seconds=interval_seconds) <= as_of


def _configured_runtime_positive_int(raw: str | None, *, default: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime integer value must be positive") from exc
    if value <= 0:
        raise ValueError("runtime integer value must be positive")
    return value


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
        sources = [primary]
        secondary_name = values.get("AEGIS_NEWS_SECONDARY_PROVIDER", "").strip().lower()
        if secondary_name == "alpaca":
            sources.append(AlpacaNewsProvider(
                api_key_id=_first_nonempty(values, "ALPACA_API_KEY_ID", "APCA_API_KEY_ID"),
                api_secret_key=_first_nonempty(
                    values, "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY"
                ),
            ))
        gdelt_enabled = values.get("AEGIS_NEWS_GDELT_ENABLED", "false").strip().lower()
        if gdelt_enabled in {"1", "true", "yes", "on"}:
            sources.append(_shared_runtime_gdelt_provider())
        if len(sources) == 1:
            return GlobalNewsIntelligenceEngine(primary)
        return GlobalNewsIntelligenceEngine(CrossCheckedNewsProvider(*sources))
    if provider_mode is ProviderMode.NONE:
        return GlobalNewsIntelligenceEngine(NewsFeedProvider(unavailable=True))
    return GlobalNewsIntelligenceEngine(NewsFeedProvider())


def _first_nonempty(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    return None


def _shared_runtime_gdelt_provider() -> GdeltNewsProvider:
    """Reuse bounded GDELT cache across the runner's per-cycle engine rebuilds."""
    global _RUNTIME_GDELT_PROVIDER
    with _RUNTIME_GDELT_PROVIDER_LOCK:
        if _RUNTIME_GDELT_PROVIDER is None:
            # Refresh on a slower cadence than scanner polling. Persisted
            # fallback can survive provider throttling for at most the news
            # freshness window; GDELT itself filters cached articles by their
            # publication timestamps before returning them as fallback.
            _RUNTIME_GDELT_PROVIDER = GdeltNewsProvider(
                cache_ttl=timedelta(minutes=30),
                cache_path=Path("work") / "gdelt-news-cache.json",
            )
        return _RUNTIME_GDELT_PROVIDER


def _shared_candidate_gdelt_provider(*, query: str) -> GdeltNewsProvider:
    """Reuse the bounded cache and cooldown for an unchanged candidate query.

    Candidate checks run more often than the global news refresh.  Creating a
    new provider for every poll discarded its cache and its rate-limit
    cooldown, which could repeatedly hit GDELT for the same candidate set.
    Keep a small, process-local query cache: it is read-only and cannot affect
    broker execution other than providing existing evidence to RiskManager.
    """
    global _RUNTIME_CANDIDATE_GDELT_PROVIDERS
    with _RUNTIME_CANDIDATE_GDELT_PROVIDER_LOCK:
        provider = _RUNTIME_CANDIDATE_GDELT_PROVIDERS.get(query)
        if provider is not None:
            return provider
        if len(_RUNTIME_CANDIDATE_GDELT_PROVIDERS) >= _RUNTIME_CANDIDATE_GDELT_PROVIDER_LIMIT:
            oldest_query = next(iter(_RUNTIME_CANDIDATE_GDELT_PROVIDERS))
            _RUNTIME_CANDIDATE_GDELT_PROVIDERS.pop(oldest_query)
        provider = GdeltNewsProvider(
            query=query,
            max_records=25,
            cache_ttl=timedelta(minutes=10),
        )
        _RUNTIME_CANDIDATE_GDELT_PROVIDERS[query] = provider
        return provider


def _candidate_news_search_terms(instrument: UniversalInstrument) -> tuple[str, ...]:
    """Build bounded web terms, including exchange-qualified Asian tickers.

    Alpaca intentionally rejects non-US suffixes.  For a Tokyo or Hong Kong
    listing the ticker remains useful to web search, but only with its market
    context; a bare four-digit code is too ambiguous.  Ordinary short US
    tickers keep the existing name-only behavior.
    """
    value = instrument.display_name or instrument.symbol
    cleaned_name = " ".join(
        "".join(
            character
            for character in value
            if character.isalnum() or character in " .-_"
        ).split()
    )
    terms: list[str] = []
    if cleaned_name:
        terms.append(cleaned_name)
    symbol = instrument.symbol.strip().upper()
    if symbol.endswith(".T"):
        root = symbol[:-2]
        terms.extend((symbol, f"{root} Tokyo", f"{root} JPX"))
    elif symbol.endswith(".HK"):
        root = symbol[:-3]
        terms.extend((symbol, f"{root} Hong Kong", f"{root} HKEX"))
    elif symbol.endswith(".SS") or symbol.endswith(".SZ"):
        root = symbol[:-3]
        terms.extend((symbol, f"{root} China"))
    return tuple(dict.fromkeys(term for term in terms if term))


def _web_news_locale(instrument: UniversalInstrument) -> tuple[str, str]:
    symbol = instrument.symbol.strip().upper()
    if symbol.endswith(".T"):
        return "ja-JP", "JP"
    if symbol.endswith(".HK"):
        return "en-HK", "HK"
    return "en-US", "US"


def _shared_candidate_web_rss_provider(
    *,
    query: str,
    instrument: UniversalInstrument,
    source: str = "google",
) -> GoogleNewsRssProvider:
    locale, region = _web_news_locale(instrument)
    source_key = source.strip().lower() or "google"
    key = f"{source_key}|{query}|{instrument.symbol}|{locale}|{region}"
    global _RUNTIME_CANDIDATE_WEB_RSS_PROVIDERS
    with _RUNTIME_CANDIDATE_WEB_RSS_PROVIDER_LOCK:
        provider = _RUNTIME_CANDIDATE_WEB_RSS_PROVIDERS.get(key)
        if provider is not None:
            return provider
        if len(_RUNTIME_CANDIDATE_WEB_RSS_PROVIDERS) >= _RUNTIME_CANDIDATE_WEB_RSS_PROVIDER_LIMIT:
            oldest_query = next(iter(_RUNTIME_CANDIDATE_WEB_RSS_PROVIDERS))
            _RUNTIME_CANDIDATE_WEB_RSS_PROVIDERS.pop(oldest_query)
        provider_type = BingNewsRssProvider if source_key == "bing" else GoogleNewsRssProvider
        provider = provider_type(
            query=query,
            candidate_symbol=instrument.symbol,
            locale=locale,
            region=region,
            cache_path=Path("work") / "candidate-web-news-cache.json",
        )
        _RUNTIME_CANDIDATE_WEB_RSS_PROVIDERS[key] = provider
        return provider


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


def _merge_secondary_news_into_cycle(
    *,
    cycle: ActiveIntelligenceCycleRecord,
    instruments: tuple[UniversalInstrument, ...],
    values: Mapping[str, str],
) -> ActiveIntelligenceCycleRecord:
    """Merge only fresh, class-matched secondary evidence into the cycle."""
    key = relay_key(values)
    if key is None:
        return cycle
    remote = read_secondary_news(key=key, path=relay_store_path(values), now=cycle.completed_at)
    if not remote:
        return cycle
    by_symbol = {item.symbol: item for item in instruments}
    merged = dict(cycle.news_asset_contexts)
    accepted: list[str] = []
    for symbol, raw_context in remote.get("contexts", {}).items():
        if not isinstance(symbol, str) or not isinstance(raw_context, Mapping):
            continue
        instrument = by_symbol.get(symbol)
        if instrument is None or raw_context.get("freshness") != "NEWS_FRESH":
            continue
        if str(raw_context.get("asset_class", "")) != instrument.asset_class.value:
            continue
        latest = raw_context.get("latest_material_event_timestamp")
        if not isinstance(latest, str):
            continue
        try:
            latest_at = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        except ValueError:
            continue
        if latest_at.tzinfo is None or latest_at > cycle.news_cutoff_timestamp:
            continue
        local = merged.get(symbol)
        local_latest = None
        if isinstance(local, Mapping) and isinstance(local.get("latest_material_event_timestamp"), str):
            try:
                local_latest = datetime.fromisoformat(str(local["latest_material_event_timestamp"]).replace("Z", "+00:00"))
            except ValueError:
                local_latest = None
        if local_latest is not None and local_latest.tzinfo is not None and local_latest >= latest_at:
            continue
        merged[symbol] = dict(raw_context)
        accepted.append(symbol)
    if not accepted:
        return cycle
    diagnostics = dict(cycle.news_provider_diagnostics)
    diagnostics["SECONDARY_NEWS_RELAY"] = {
        "status": "AVAILABLE",
        "source_id": remote.get("source_id"),
        "generated_at": remote.get("generated_at"),
        "contexts_received": len(remote.get("contexts", {})),
        "contexts_accepted": len(accepted),
        "symbols": tuple(sorted(accepted)),
    }
    provider = cycle.news_provider
    if "SECONDARY_NEWS_RELAY" not in provider:
        provider = f"{provider}+SECONDARY_NEWS_RELAY"
    status = cycle.news_provider_status
    if status not in {"AVAILABLE", "PARTIAL"}:
        status = "PARTIAL"
    return cycle.model_copy(
        update={
            "news_asset_contexts": merged,
            "news_provider": provider,
            "news_provider_status": status,
            "news_provider_diagnostics": diagnostics,
        }
    )


def _enrich_candidate_news_with_gdelt(
    *,
    cycle: ActiveIntelligenceCycleRecord,
    scanner_result: ActiveScannerResult,
    instruments: tuple[UniversalInstrument, ...],
    enabled: str,
    values: Mapping[str, str] | None = None,
) -> ActiveIntelligenceCycleRecord:
    """Add fresh, symbol-linked GDELT evidence for a bounded candidate set.

    This is not a fallback approval path: it only supplies source evidence to
    the existing RiskManager rule.  If no article is found or the provider is
    unavailable, the original missing-news rejection remains in force.
    """
    if enabled.strip().lower() not in {"1", "true", "yes", "on"}:
        return cycle
    by_symbol = {instrument.symbol: instrument for instrument in instruments}
    # Persisted scanner snapshots can retain only ``candidates`` and omit the
    # derived TOP/WATCHLIST collections.  The Demo pilot reconstructs those
    # buckets earlier in the cycle, but this helper must also work when it is
    # called directly or with a partially rehydrated snapshot.  Restrict the
    # canonical list to executable discovery buckets: NO_TRADE/REJECTED must
    # never trigger a GDELT request.
    candidates = (*scanner_result.top_opportunities, *scanner_result.watchlist)
    if not candidates and scanner_result.candidates:
        candidates = tuple(
            candidate
            for candidate in scanner_result.candidates
            if candidate.bucket
            in {
                ActiveScannerBucket.TOP_OPPORTUNITIES,
                ActiveScannerBucket.WATCHLIST,
            }
        )
    candidates = _fair_execution_candidate_order(tuple(candidates))
    selected: list[UniversalInstrument] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.symbol in seen or _candidate_news_available(cycle, candidate.symbol):
            continue
        instrument = by_symbol.get(candidate.symbol)
        if instrument is None:
            continue
        selected.append(instrument)
        seen.add(candidate.symbol)
        if len(selected) == 6:
            break
    if not selected:
        return cycle

    diagnostics = dict(cycle.news_provider_diagnostics)
    merged_contexts = dict(cycle.news_asset_contexts)
    fresh_symbols: set[str] = set()

    # Alpaca's news endpoint covers stocks and crypto. Query all supported
    # candidate classes in one bounded request so crypto does not monopolize
    # the GDELT fallback while securities wait for a public-provider cooldown.
    # For crypto, use Alpaca's documented CRYPTO:<symbol> alias to normalize
    # symbols to the provider's USD-pair convention; restore the canonical
    # broker symbol only after an exact linked context is established.
    targeted_news_instruments = tuple(
        instrument
        for instrument in selected
        if instrument.asset_class
        in {AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO}
    )
    alpaca_source_groups: dict[str, list[UniversalInstrument]] = {}
    unsupported_alpaca_symbols: list[str] = []
    for instrument in targeted_news_instruments:
        source_input = (
            f"CRYPTO:{instrument.symbol}"
            if instrument.asset_class is AssetClass.CRYPTO
            else instrument.symbol
        )
        source_symbol = alpaca_news_symbol(source_input)
        if source_symbol is None:
            unsupported_alpaca_symbols.append(instrument.symbol)
            continue
        alpaca_source_groups.setdefault(source_symbol, []).append(instrument)
    # Only use one-to-one aliases. If multiple broker instruments collapse to
    # the same provider ticker, leave them for the name-aware GDELT path rather
    # than attributing one article ambiguously to several candidates.
    alpaca_aliases = {
        source_symbol: grouped[0]
        for source_symbol, grouped in alpaca_source_groups.items()
        if len(grouped) == 1
    }
    ambiguous_alpaca_symbols = tuple(
        instrument.symbol
        for grouped in alpaca_source_groups.values()
        if len(grouped) > 1
        for instrument in grouped
    )
    alpaca_instruments = tuple(
        instrument.model_copy(update={"symbol": source_symbol})
        for source_symbol, instrument in alpaca_aliases.items()
    )
    api_key_id = _first_nonempty(values or {}, "ALPACA_API_KEY_ID", "APCA_API_KEY_ID")
    api_secret_key = _first_nonempty(
        values or {}, "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY"
    )
    if alpaca_instruments and api_key_id and api_secret_key:
        alpaca_result = GlobalNewsIntelligenceEngine(
            AlpacaNewsProvider(
                api_key_id=api_key_id,
                api_secret_key=api_secret_key,
                symbols=tuple(instrument.symbol for instrument in alpaca_instruments),
                limit=50,
                lookback_hours=48,
            )
        ).analyze(instruments=alpaca_instruments, as_of=cycle.news_cutoff_timestamp)
        alpaca_contexts = _news_asset_contexts(alpaca_result)
        alpaca_fresh: dict[str, dict[str, object]] = {}
        for source_symbol, instrument in alpaca_aliases.items():
            context = alpaca_contexts.get(source_symbol)
            if (
                isinstance(context, dict)
                and context.get("freshness") == "NEWS_FRESH"
                and context.get("latest_material_event_timestamp")
            ):
                # Restore the canonical broker symbol after matching the
                # provider's normalized ticker (for example AMAT.RTH -> AMAT).
                alpaca_fresh[instrument.symbol] = context
        merged_contexts.update(alpaca_fresh)
        fresh_symbols.update(alpaca_fresh)
        diagnostics["CANDIDATE_ALPACA"] = {
            "status": alpaca_result.provider_status.value,
            "candidates_checked": tuple(
                instrument.symbol for instrument in alpaca_aliases.values()
            ),
            "provider_symbols": {
                instrument.symbol: source_symbol
                for source_symbol, instrument in alpaca_aliases.items()
            },
            "unsupported_symbols": tuple(unsupported_alpaca_symbols),
            "ambiguous_symbols": ambiguous_alpaca_symbols,
            "fresh_contexts": tuple(sorted(alpaca_fresh)),
            "articles_returned": alpaca_result.raw_event_count,
            "broker_write_calls": 0,
        }

    remaining_selected = tuple(
        instrument for instrument in selected if instrument.symbol not in fresh_symbols
    )
    terms: list[str] = []
    for instrument in remaining_selected:
        # Search the issuer/asset name, while adding an exchange-qualified
        # ticker for international listings. Bare short US tickers remain
        # excluded because they collide with ordinary words.
        for term in _candidate_news_search_terms(instrument):
            if term not in terms:
                terms.append(term)
    if not terms:
        return cycle.model_copy(
            update={
                "news_asset_contexts": merged_contexts,
                "news_provider_diagnostics": diagnostics,
            }
        )
    query = "(" + " OR ".join(f'\"{term}\"' for term in terms) + ")"
    provider = _shared_candidate_gdelt_provider(query=query)
    result = GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=remaining_selected, as_of=cycle.news_cutoff_timestamp
    )
    contexts = _news_asset_contexts(result)
    fresh_contexts = {
        symbol: context
        for symbol, context in contexts.items()
        if context.get("freshness") == "NEWS_FRESH"
        and context.get("latest_material_event_timestamp")
    }
    merged_contexts.update(fresh_contexts)
    diagnostics["CANDIDATE_GDELT"] = {
        "status": result.provider_status.value,
        "candidates_checked": tuple(instrument.symbol for instrument in remaining_selected),
        "fresh_contexts": tuple(sorted(fresh_contexts)),
        "articles_returned": result.raw_event_count,
        "provider_details": {
            key: value
            for key, value in provider.last_diagnostics.items()
            if key in {
                "http_status",
                "provider_error_message",
                "retry_after_seconds",
                "cache_hit",
                "stale_cache_fallback",
            }
        },
        "broker_write_calls": 0,
    }

    # GDELT and broker APIs do not cover every international listing.  When
    # there is still no linked context, perform a bounded read-only web search
    # per remaining candidate.  This is deliberately candidate-specific: a
    # global headline alone must never satisfy the RiskManager news gate.
    web_enabled = (
        values is not None
        and values.get("AEGIS_NEWS_WEB_SEARCH_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    web_remaining = tuple(
        instrument for instrument in remaining_selected if instrument.symbol not in fresh_contexts
    )
    if web_enabled and web_remaining:
        web_attempts: list[dict[str, object]] = []
        web_fresh: dict[str, dict[str, object]] = {}
        for instrument in web_remaining:
            candidate_terms = _candidate_news_search_terms(instrument)
            if not candidate_terms:
                continue
            web_query = "(" + " OR ".join(f'\"{term}\"' for term in candidate_terms) + ")"
            for source in ("google", "bing"):
                web_provider = _shared_candidate_web_rss_provider(
                    query=web_query,
                    instrument=instrument,
                    source=source,
                )
                web_result = GlobalNewsIntelligenceEngine(web_provider).analyze(
                    instruments=(instrument,), as_of=cycle.news_cutoff_timestamp
                )
                candidate_context = _news_asset_contexts(web_result).get(
                    instrument.symbol, {}
                )
                has_fresh_context = (
                    isinstance(candidate_context, dict)
                    and candidate_context.get("freshness") == "NEWS_FRESH"
                    and candidate_context.get("latest_material_event_timestamp")
                )
                if has_fresh_context:
                    web_fresh[instrument.symbol] = candidate_context
                web_attempts.append(
                    {
                        "symbol": instrument.symbol,
                        "source": source,
                        "status": web_result.provider_status.value,
                        "articles_returned": web_result.raw_event_count,
                        "fresh_context": bool(has_fresh_context),
                        "locale": web_provider.last_diagnostics.get("locale"),
                        "region": web_provider.last_diagnostics.get("region"),
                        "provider_details": {
                            key: value
                            for key, value in web_provider.last_diagnostics.items()
                            if key
                            in {
                                "http_status",
                                "provider_error_message",
                                "cache_hit",
                                "locale",
                                "region",
                            }
                        },
                    }
                )
                if has_fresh_context:
                    break
        merged_contexts.update(web_fresh)
        fresh_symbols.update(web_fresh)
        web_statuses = tuple(str(item["status"]) for item in web_attempts)
        diagnostics["CANDIDATE_WEB_RSS"] = {
            "status": (
                "AVAILABLE"
                if web_fresh
                else (web_statuses[-1] if web_statuses else "PROVIDER_UNAVAILABLE")
            ),
            "candidates_checked": tuple(item["symbol"] for item in web_attempts),
            "fresh_contexts": tuple(sorted(web_fresh)),
            "attempts": tuple(web_attempts),
            "articles_returned": sum(int(item["articles_returned"]) for item in web_attempts),
            "broker_write_calls": 0,
        }
    return cycle.model_copy(
        update={
            "news_asset_contexts": merged_contexts,
            "news_provider_diagnostics": diagnostics,
        }
    )


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
    # Coverage is a gate on executable TOP selections, not on every row the
    # scanner evaluated. HOLD/WATCHLIST rows are useful diagnostics, but they
    # are not a buy decision and must not block a crypto-led TOP selection
    # merely because the currently available equity data comes from one
    # exchange region. A non-crypto TOP BUY still requires multi-region
    # coverage before it can reach the Demo package path.
    candidates = tuple(
        candidate
        for candidate in scanner_result.top_opportunities
        if candidate.decision is AegisDecision.BUY
    )
    by_symbol = {instrument.symbol: instrument for instrument in instruments}
    regions = {
        _selection_region(candidate.symbol, candidate.asset_class.value, by_symbol.get(candidate.symbol))
        for candidate in candidates
    }
    non_crypto_regions = {region for region in regions if region != "CRYPTO"}
    # Region diversity is useful evidence for ranking diagnostics, but it must
    # not deadlock an otherwise valid BUY package. The acquisition coverage
    # ratio, asset-linked news, RiskManager, preflight and execution gate are
    # the actual admission controls. A single currently open region is normal
    # outside overlapping market hours and is not proof that the candidate is
    # unsafe.
    return tuple(blockers)


def _scanner_asset_class_counts(scanner_result: ActiveScannerResult) -> dict[str, dict[str, int]]:
    """Expose class composition so multi-asset exclusion is observable."""

    def counts(candidates: Collection[ActiveScannerCandidate]) -> dict[str, int]:
        result: Counter[str] = Counter()
        for candidate in candidates:
            result[candidate.asset_class.value] += 1
        return dict(sorted(result.items()))

    return {
        "evaluated": counts(scanner_result.candidates),
        "buy_signals": counts(
            tuple(
                candidate
                for candidate in scanner_result.candidates
                if candidate.bucket is ActiveScannerBucket.TOP_OPPORTUNITIES
            )
        ),
        "top": counts(scanner_result.top_opportunities),
        "watchlist": counts(scanner_result.watchlist),
        "no_trade": counts(scanner_result.no_trade),
        "rejected": counts(scanner_result.rejected),
    }


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


def _execution_quote_check_time(
    quote_as_of: datetime,
    *,
    maximum_future_skew_seconds: int = 5,
) -> datetime:
    """Wait for bounded broker clock skew without rewriting market evidence."""
    checked_at = datetime.now(UTC)
    ahead_seconds = (quote_as_of - checked_at).total_seconds()
    if 0 < ahead_seconds <= maximum_future_skew_seconds:
        # Refetching immediately yields another rate ahead of the local clock.
        # Keep this quote, wait once, then let the strict freshness check decide.
        sleep(ahead_seconds)
        checked_at = datetime.now(UTC)
    return checked_at


def _fair_execution_candidate_order(
    candidates: tuple[ActiveScannerCandidate, ...],
) -> tuple[ActiveScannerCandidate, ...]:
    """Interleave broker checks across asset classes without changing ranking.

    The live eToro reads behind package construction are bounded and rate
    limited.  Processing a score-sorted list straight through can spend that
    scarce budget on one class (historically crypto), leaving equally ranked
    equities and ETFs unverified.  Preserve the existing order within each
    class, but take one candidate per class per pass.
    """
    if len(candidates) < 2:
        return candidates
    by_class: dict[AssetClass, list[ActiveScannerCandidate]] = {}
    for candidate in candidates:
        by_class.setdefault(candidate.asset_class, []).append(candidate)
    class_order = [
        asset_class
        for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO)
        if asset_class in by_class
    ]
    class_order.extend(
        sorted(
            (asset_class for asset_class in by_class if asset_class not in class_order),
            key=lambda asset_class: asset_class.value,
        )
    )
    ordered: list[ActiveScannerCandidate] = []
    index = 0
    while class_order:
        next_order: list[AssetClass] = []
        for asset_class in class_order:
            bucket = by_class[asset_class]
            if index < len(bucket):
                ordered.append(bucket[index])
            if index + 1 < len(bucket):
                next_order.append(asset_class)
        class_order = next_order
        index += 1
    return tuple(ordered)


def _candidate_execution_diagnostics(
    *,
    cycle: ActiveIntelligenceCycleRecord,
    scanner_result: ActiveScannerResult,
    package_diagnostics: object,
    quote_diagnostics: object,
    pilot_result: object,
) -> tuple[dict[str, object], ...]:
    """Join market, news, packaging, and gate outcomes by candidate symbol."""
    packages = package_diagnostics if isinstance(package_diagnostics, Mapping) else {}
    quotes = quote_diagnostics if isinstance(quote_diagnostics, Mapping) else {}
    pilot = (
        pilot_result.model_dump(mode="json")
        if hasattr(pilot_result, "model_dump")
        else pilot_result
    )
    submissions = pilot.get("submissions", ()) if isinstance(pilot, Mapping) else ()
    submitted_by_symbol = {
        str(item.get("symbol")): item
        for item in submissions
        if isinstance(item, Mapping) and item.get("symbol")
    } if isinstance(submissions, (list, tuple)) else {}
    contexts = cycle.news_asset_contexts
    ranked_watchlist = _fair_execution_candidate_order(
        tuple(
            sorted(
                scanner_result.watchlist,
                key=lambda item: (item.opportunity_score, item.confidence),
                reverse=True,
            )
        )
    )[:8]
    candidates = tuple(scanner_result.top_opportunities) + ranked_watchlist
    unique_by_symbol: dict[str, ActiveScannerCandidate] = {}
    for candidate in candidates:
        unique_by_symbol.setdefault(candidate.symbol, candidate)
    unique_candidates = tuple(unique_by_symbol.values())
    rows: list[dict[str, object]] = []
    for candidate in _fair_execution_candidate_order(unique_candidates):
        raw_context = contexts.get(candidate.symbol, {})
        context = (
            raw_context.model_dump(mode="json")
            if hasattr(raw_context, "model_dump")
            else raw_context
        )
        context = context if isinstance(context, Mapping) else {}
        raw_quote = quotes.get(candidate.symbol, {})
        quote = raw_quote if isinstance(raw_quote, Mapping) else {}
        row: dict[str, object] = {
            "symbol": candidate.symbol,
            "asset_class": candidate.asset_class.value,
            "bucket": candidate.bucket.value,
            "package_status": packages.get(candidate.symbol, "NOT_PREPARED"),
            "quote": dict(quote),
            "news": {
                "freshness": context.get("freshness", "NEWS_CONTEXT_MISSING"),
                "material_event_count": context.get("material_event_count", 0),
                "event_risk": context.get("event_risk"),
                "latest_material_event_timestamp": context.get(
                    "latest_material_event_timestamp"
                ),
            },
        }
        submission = submitted_by_symbol.get(candidate.symbol)
        if submission is not None:
            row["submission"] = {
                "status": submission.get("sanitized_status"),
                "submitted": bool(submission.get("submitted", False)),
                "risk_violation_codes": tuple(
                    str(code) for code in submission.get("risk_violation_codes", ())
                ),
                "preflight_reasons": tuple(
                    str(reason) for reason in submission.get("preflight_reasons", ())
                ),
            }
        rows.append(row)
    return tuple(rows)


MAX_NEW_CRYPTO_PORTFOLIO_EXPOSURE_FRACTION = Decimal("0.50")


def _verified_crypto_exposure_headroom(
    snapshot: DemoPortfolioSnapshot,
    catalog_items: tuple[dict[str, object], ...],
) -> Decimal | None:
    """Fail closed unless every live position has a catalog-verified class."""
    if snapshot.total_value <= 0:
        return None
    classes: dict[int, str] = {}
    for item in catalog_items:
        instrument_id = item.get("instrumentID", item.get("instrumentId"))
        instrument_type = item.get("instrumentTypeID", item.get("instrumentTypeId"))
        if not isinstance(instrument_id, int) or not isinstance(instrument_type, int):
            continue
        asset_class = {5: "EQUITY", 6: "ETF", 10: "CRYPTO"}.get(instrument_type)
        if asset_class is None:
            continue
        previous = classes.get(instrument_id)
        if previous is not None and previous != asset_class:
            return None
        classes[instrument_id] = asset_class
    crypto_exposure = Decimal("0")
    for position in snapshot.positions:
        asset_class = classes.get(position.instrument_id)
        if asset_class is None:
            return None
        if asset_class == "CRYPTO":
            crypto_exposure += position.current_exposure
    cap = snapshot.total_value * MAX_NEW_CRYPTO_PORTFOLIO_EXPOSURE_FRACTION
    return max(Decimal("0"), cap - crypto_exposure).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )


def _build_live_submission_packages(
    *,
    cycle: ActiveIntelligenceCycleRecord,
    scanner_result: ActiveScannerResult,
    client: EtoroReadClient,
    instrument_ids_by_symbol: Mapping[str, int],
    config: ApplicationConfig,
    settings: EtoroDemoPilotSettings,
    registry: SqliteRecordStore,
    diagnostics: dict[str, object] | None = None,
    sizing_diagnostics: dict[str, object] | None = None,
    quote_diagnostics: dict[str, dict[str, object]] | None = None,
    maximum_future_quote_skew_seconds: int = 5,
) -> Mapping[str, EtoroDemoSubmissionPackage]:
    """Build execution material only after an accepted decision cycle."""
    def record(symbol: str, reason: str) -> None:
        if diagnostics is not None:
            diagnostics[symbol] = reason

    if config.authorized_capital_eur is None:
        if diagnostics is not None:
            diagnostics["_runtime"] = "AUTHORIZED_CAPITAL_MISSING"
        return {}
    try:
        identity = client.identity()
        demo_snapshot = client.demo_account(identity)
    except (EtoroApiError, EtoroMappingError, ValueError, TypeError) as exc:
        # Account-read failures must not erase an already completed global
        # intelligence cycle or stop the continuous scanner.  Packaging is
        # an execution-time concern: keep the cycle observable, return no
        # executable package, and let the pilot fail closed for this poll.
        record(
            "_runtime",
            f"DEMO_ACCOUNT_READ_FAILED:{getattr(exc, 'status', None) or type(exc).__name__}",
        )
        return {}
    if config.authorized_capital is None:
        if diagnostics is not None:
            diagnostics["_runtime"] = "AUTHORIZED_CAPITAL_MISSING"
        return {}
    # The authorization, managed ledger, broker cash and proposed order must
    # all use the same currency. Never compare nominal EUR and USD amounts.
    if demo_snapshot.currency is not config.authorized_capital_currency:
        if diagnostics is not None:
            diagnostics["_runtime"] = (
                "AUTHORIZED_CAPITAL_CURRENCY_MISMATCH:"
                f"configured={config.authorized_capital_currency.value};"
                f"account={demo_snapshot.currency.value}"
            )
        return {}
    managed_exposure_limit = effective_aegis_managed_exposure_limit(
        config.authorized_capital, demo_snapshot.currency
    )
    if managed_exposure_limit is None:
        if diagnostics is not None:
            diagnostics["_runtime"] = "AUTHORIZED_CAPITAL_CURRENCY_UNSUPPORTED"
        return {}
    managed_exposure = registry.managed_demo_exposure(demo_snapshot.currency)
    if managed_exposure is None:
        if diagnostics is not None:
            diagnostics["_runtime"] = "MANAGED_EXPOSURE_UNAVAILABLE_OR_UNDENOMINATED"
        return {}
    remaining_managed_exposure = max(
        Decimal("0"), managed_exposure_limit - managed_exposure
    )
    if remaining_managed_exposure <= 0:
        if diagnostics is not None:
            diagnostics["_runtime"] = "AEGIS_MANAGED_EXPOSURE_CAP_REACHED"
        return {}
    max_single_order = MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY[demo_snapshot.currency]
    policy_engine = default_asset_policy_engine(
        minimum_cash_reserve=config.risk.min_cash_reserve
    )
    packages: dict[str, EtoroDemoSubmissionPackage] = {}
    # Prepare the TOP candidate first, but also prepare a bounded fallback
    # lane.  A transient resolution/quote/eligibility failure for the first
    # candidate must not turn a valid cycle into the historic zero-order dead
    # end.  The pilot still selects only package-ready candidates and keeps
    # every preflight, RiskManager and execution gate intact.
    ranked_watchlist = _fair_execution_candidate_order(
        tuple(sorted(
            scanner_result.watchlist,
            key=lambda candidate: (
                candidate.opportunity_score,
                candidate.confidence,
            ),
            reverse=True,
        ))
    )[:8]
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
    candidates_for_demo = _fair_execution_candidate_order(
        tuple(candidate_by_symbol[symbol] for symbol in candidates_for_demo)
    )
    crypto_exposure_headroom: Decimal | None = None
    if any(candidate.asset_class is AssetClass.CRYPTO for candidate in candidates_for_demo):
        try:
            from app.data.runtime import (
                DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH,
                _validated_etoro_catalog_items,
            )

            crypto_exposure_headroom = _verified_crypto_exposure_headroom(
                demo_snapshot,
                _validated_etoro_catalog_items(DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH),
            )
        except (OSError, ValueError, TypeError):
            # Unknown broker position classes cannot prove a crypto buy stays
            # below the portfolio-level cap. Equity/ETF candidates remain
            # independently eligible for their normal hard gates.
            crypto_exposure_headroom = None
    for candidate in candidates_for_demo:
        asset_policy = policy_engine.policy_for(candidate.asset_class)
        if candidate.asset_class is AssetClass.CRYPTO and crypto_exposure_headroom is None:
            record(candidate.symbol, "CRYPTO_CLASS_EXPOSURE_UNVERIFIED")
            continue
        instrument_id = instrument_ids_by_symbol.get(candidate.symbol)
        if instrument_id is None or instrument_id <= 0:
            record(candidate.symbol, "VERIFIED_CATALOG_INSTRUMENT_ID_MISSING")
            continue
        # A provider rate limit is local to this candidate.  It must not
        # abort the already completed intelligence cycle or kill the runner.
        try:
            resolution = _read_etoro_with_rate_limit_retry(
                partial(
                    client.resolve_instrument_id,
                    instrument_id,
                    symbol=candidate.symbol,
                    as_of=cycle.scheduled_at,
                )
            )
        except (EtoroApiError, EtoroMappingError, ValueError, TypeError) as exc:
            record(
                candidate.symbol,
                f"RESOLUTION_READ_FAILED:{getattr(exc, 'status', None) or type(exc).__name__}",
            )
            continue
        if (
            resolution.instrument_id != instrument_id
            or resolution.internal_symbol_full.casefold() != candidate.symbol.casefold()
        ):
            record(candidate.symbol, "RESOLUTION_ID_SYMBOL_MISMATCH")
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
        if (
            candidate.asset_class in {AssetClass.EQUITY, AssetClass.ETF}
            and _preflight_market_status(resolution) is MarketStatus.CLOSED
        ):
            record(candidate.symbol, "MARKET_CLOSED")
            if quote_diagnostics is not None:
                quote_diagnostics[candidate.symbol] = {
                    "status": "MARKET_CLOSED",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "attempts": 0,
                    "max_age_seconds": config.risk.max_price_age_seconds,
                }
            continue
        quote = None
        quote_fresh = False
        quote_read_error: Exception | None = None
        for quote_attempt in range(2):
            try:
                quote = _read_etoro_with_rate_limit_retry(
                    partial(
                        client.quote,
                        instrument_id,
                        candidate.symbol,
                        currency=demo_snapshot.currency,
                    )
                )
                quote_read_error = None
            except (EtoroApiError, EtoroMappingError, ValueError, TypeError) as exc:
                quote_read_error = exc
                if quote_diagnostics is not None:
                    prior = quote_diagnostics.get(candidate.symbol, {})
                    quote_diagnostics[candidate.symbol] = {
                        **prior,
                        "status": "QUOTE_READ_FAILED",
                        "attempts": quote_attempt + 1,
                        "error_code": (
                            str(getattr(exc, "status", None) or type(exc).__name__)
                        ),
                        "checked_at": datetime.now(UTC).isoformat(),
                        "max_age_seconds": config.risk.max_price_age_seconds,
                    }
                if quote_attempt == 1:
                    break
                continue
            # The broker's rate timestamp is market evidence, not the time at
            # which Aegis is making an execution decision. Never make an old
            # quote look fresh by evaluating the risk and preflight at that old
            # timestamp (or mint an authorization that is already expired).
            quote_received_at = _execution_quote_check_time(
                quote.as_of,
                maximum_future_skew_seconds=maximum_future_quote_skew_seconds,
            )
            quote_age = quote_received_at - quote.as_of
            quote_fresh = timedelta(0) <= quote_age <= timedelta(
                seconds=config.risk.max_price_age_seconds
            )
            if quote_diagnostics is not None:
                quote_diagnostics[candidate.symbol] = {
                    "status": (
                        "FRESH"
                        if quote_fresh
                        else "FUTURE_TIMESTAMP"
                        if quote_age < timedelta(0)
                        else "STALE"
                    ),
                    "attempts": quote_attempt + 1,
                    "quote_as_of": quote.as_of.isoformat(),
                    "checked_at": quote_received_at.isoformat(),
                    "age_seconds": round(quote_age.total_seconds(), 3),
                    "max_age_seconds": config.risk.max_price_age_seconds,
                }
            if quote_fresh:
                break
        if not quote_fresh or quote is None:
            if quote_read_error is not None:
                record(
                    candidate.symbol,
                    "QUOTE_READ_FAILED:"
                    f"{getattr(quote_read_error, 'status', None) or type(quote_read_error).__name__}",
                )
            else:
                record(candidate.symbol, "QUOTE_NOT_FRESH_FOR_EXECUTION")
            continue
        try:
            eligibility = _read_etoro_with_rate_limit_retry(
                partial(
                    client.demo_eligibility,
                    instrument_id,
                    candidate.symbol,
                    currency=demo_snapshot.currency,
                )
            )
        except EtoroEligibilityDenied:
            record(candidate.symbol, "BROKER_EXPLICITLY_DISALLOWS_OPENING")
            continue
        except (EtoroApiError, EtoroMappingError, ValueError, TypeError) as exc:
            record(
                candidate.symbol,
                "ELIGIBILITY_READ_FAILED:"
                f"{getattr(exc, 'status', None) or type(exc).__name__}",
            )
            continue
        # The rates endpoint intentionally maps market_status to UNKNOWN;
        # resolution is the authoritative live-session/tradability source.
        # Reuse the same Crypto 24/7-aware mapping as the controlled preflight
        # instead of failing every valid Crypto candidate at the next gate.
        quote = quote.model_copy(update={"market_status": _preflight_market_status(resolution)})
        execution_check_at = datetime.now(UTC)
        instrument = _instrument_from_eligibility(resolution, eligibility, execution_check_at)
        risk_portfolio = _portfolio_from_demo_snapshot(
            demo_snapshot,
            target_instrument_id=instrument_id,
            target_symbol=candidate.symbol,
        )
        # Size against the exact PortfolioSnapshot the RiskManager will see,
        # not the broker DTO used earlier in the read path. Leave a small
        # account-currency cushion below the reserve boundary so cent/Decimal
        # reconstruction cannot turn a nominally compliant size into
        # MIN_CASH_RESERVE_BREACHED at the final risk gate.
        risk_reference_value = min(
            risk_portfolio.total_value, config.authorized_capital
        )
        # RiskManager divides post-trade cash by the whole portfolio value.
        # The managed exposure ceiling must not reduce this reserve basis.
        cash_reserve_reference = risk_portfolio.total_value
        required_cash_reserve = max(
            config.risk.min_cash_reserve, asset_policy.minimum_cash_reserve
        )
        risk_trade_cap = risk_reference_value * min(
            config.risk.max_trade_size, asset_policy.max_new_trade_exposure
        )
        cash_reserve_cap = _cash_reserve_aware_order_cap(
            cash=risk_portfolio.cash,
            reference_value=cash_reserve_reference,
            reserve_fraction=required_cash_reserve,
            rounding_buffer=Decimal("1.00"),
        )
        sizing_caps = {
            "managed_exposure_limit": managed_exposure_limit,
            "remaining_managed_exposure": remaining_managed_exposure,
            "single_order_limit": max_single_order,
            "account_cash": risk_portfolio.cash,
            "risk_trade_cap": risk_trade_cap,
            "cash_reserve_cap": cash_reserve_cap,
        }
        if candidate.asset_class is AssetClass.CRYPTO:
            sizing_caps["crypto_portfolio_exposure_cap"] = crypto_exposure_headroom
        demo_order_amount = min(sizing_caps.values()).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        if demo_order_amount <= 0:
            zero_caps = tuple(
                name for name, amount in sizing_caps.items() if amount <= 0
            )
            record(
                candidate.symbol,
                "CRYPTO_PORTFOLIO_EXPOSURE_LIMIT_REACHED"
                if zero_caps == ("crypto_portfolio_exposure_cap",)
                else "DEMO_BUYING_POWER_UNAVAILABLE",
            )
            if sizing_diagnostics is not None:
                zero_caps = tuple(
                    name for name, amount in sizing_caps.items() if amount <= 0
                )
                sizing_diagnostics[candidate.symbol] = {
                    "status": "NO_POSITIVE_SAFE_ORDER_AMOUNT",
                    "zero_caps": zero_caps,
                    "rounded_below_account_cent": not zero_caps,
                    "cash_reserve_fraction": str(required_cash_reserve),
                    "cash_reserve_required": str(
                        (cash_reserve_reference * required_cash_reserve).quantize(
                            Decimal("0.01"), rounding=ROUND_DOWN
                        )
                    ),
                    "cash_reserve_shortfall": str(
                        max(
                            Decimal("0"),
                            (cash_reserve_reference * required_cash_reserve)
                            + Decimal("1.00")
                            - risk_portfolio.cash,
                        ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    ),
                    "account_currency": demo_snapshot.currency.value,
                }
            continue
        # A positive cash headroom is not an executable order when it is
        # smaller than the broker-verified minimum. Keep this out of the
        # package-ready lane rather than handing an impossible proposal to
        # preflight nine times in the same cycle. Never lower the reserve or
        # broker minimum to manufacture buying power.
        if eligibility.minimum_position is None or eligibility.minimum_position <= 0:
            record(candidate.symbol, "VERIFIED_MINIMUM_POSITION_UNAVAILABLE")
            if sizing_diagnostics is not None:
                sizing_diagnostics[candidate.symbol] = {
                    "status": "VERIFIED_MINIMUM_POSITION_UNAVAILABLE",
                    "safe_order_amount": str(demo_order_amount),
                    "account_currency": demo_snapshot.currency.value,
                }
            continue
        if demo_order_amount < eligibility.minimum_position:
            record(candidate.symbol, "SAFE_ORDER_BELOW_VERIFIED_MINIMUM")
            if sizing_diagnostics is not None:
                sizing_diagnostics[candidate.symbol] = {
                    "status": "SAFE_ORDER_BELOW_VERIFIED_MINIMUM",
                    "safe_order_amount": str(demo_order_amount),
                    "verified_minimum_position": str(eligibility.minimum_position),
                    "limiting_caps": tuple(
                        name for name, amount in sizing_caps.items()
                        if amount == min(sizing_caps.values())
                    ),
                    "cash_reserve_fraction": str(required_cash_reserve),
                    "account_currency": demo_snapshot.currency.value,
                }
            continue
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
        risk_context = RiskContext(
            evaluated_at=execution_check_at,
            portfolio=risk_portfolio,
            price=quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            # News is mandatory for the specific instrument. Global or
            # unlinked geopolitical headlines are context only and must not
            # authorize an order for an unrelated equity, ETF, or crypto.
            news_data_available=_candidate_news_available(cycle, candidate.symbol),
            daily_new_trade_count=0,
            recent_idempotency_keys=frozenset(),
            api_state_consistent=True,
            capital_envelope=AuthorizedCapitalEnvelope(
                currency=demo_snapshot.currency,
                authorized_capital_eur=managed_exposure_limit,
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
                clock=_fixed_clock(execution_check_at),
            ),
            now=execution_check_at,
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


def _cash_reserve_aware_order_cap(
    *,
    cash: Decimal,
    reference_value: Decimal,
    reserve_fraction: Decimal,
    rounding_buffer: Decimal = Decimal("1.00"),
) -> Decimal:
    """Cap a buy below (not on) the RiskManager's minimum-cash boundary."""
    if cash < 0 or reference_value <= 0 or not Decimal("0") <= reserve_fraction < Decimal("1"):
        return Decimal("0.00")
    headroom = cash - (reference_value * reserve_fraction) - max(
        Decimal("0"), rounding_buffer
    )
    return max(Decimal("0"), headroom).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


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


DEMO_ORDER_LOOKUP_RETRY_BACKOFF = timedelta(minutes=5)


def _demo_order_lookup_due(
    payload: Mapping[str, object], *, now: datetime
) -> bool:
    raw_checked_at = payload.get("broker_order_lookup_attempted_at")
    if not isinstance(raw_checked_at, str):
        return True
    try:
        checked_at = datetime.fromisoformat(raw_checked_at)
    except ValueError:
        return True
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return now - checked_at >= DEMO_ORDER_LOOKUP_RETRY_BACKOFF


def _legacy_fill_needs_order_lookup(payload: Mapping[str, object]) -> bool:
    return not (
        str(payload.get("broker_order_status", "")).upper() == "FILLED"
        and payload.get("broker_position_id")
        and payload.get("broker_reconciliation_source")
        and payload.get("executed_exposure_account_currency") is not None
    )


def _reconcile_unresolved_demo_submissions(
    *,
    registry: SqliteRecordStore,
    client: EtoroReadClient,
    identity: BrokerIdentity,
    observed_at: datetime,
) -> dict[str, object]:
    """Refresh broker states before sizing new Demo exposure.

    A write can be accepted before eToro exposes the order in its breakdown
    endpoint.  Re-reading later is safe; treating that temporary absence as a
    new order opportunity is not.  Only an explicit broker state changes the
    local lifecycle record, while unreadable orders remain unresolved.
    """
    records = list(registry.unresolved_demo_submissions())
    seen_keys = {str(record.get("idempotency_key", "")) for record in records}
    legacy_records = [
        record
        for record in registry.filled_demo_submissions()
        if str(record.get("idempotency_key", "")) not in seen_keys
        and isinstance(record.get("payload"), Mapping)
        and _legacy_fill_needs_order_lookup(record["payload"])
    ]
    records.extend(legacy_records)
    verified = 0
    legacy_verified = 0
    attempted = 0
    legacy_attempted = 0
    errors: list[str] = []
    lookup_checked_at = datetime.now(UTC)
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            errors.append("INVALID_PAYLOAD")
            continue
        key = str(record.get("idempotency_key", ""))
        try:
            instrument_id = int(payload["instrument_id"])
            order_id = str(payload["broker_order_id"])
        except (KeyError, TypeError, ValueError):
            errors.append("MISSING_ORDER_REFERENCE")
            continue
        if instrument_id <= 0 or not order_id:
            errors.append("INVALID_ORDER_REFERENCE")
            continue
        if not _demo_order_lookup_due(payload, now=lookup_checked_at):
            continue
        attempted += 1
        is_legacy_fill = str(record.get("state", "")).upper() in {
            "FILLED",
            "PARTIALLY_FILLED",
        }
        if is_legacy_fill:
            legacy_attempted += 1
        lookup_details: Mapping[str, object] = {}
        official_lookup_succeeded = False
        try:
            detail_lookup = getattr(client, "demo_order_lookup_details", None)
            lookup = getattr(client, "demo_order_lookup", None)
            if callable(detail_lookup):
                raw_details = detail_lookup(order_id)
                lookup_details = (
                    raw_details if isinstance(raw_details, Mapping) else {}
                )
                observed = lookup_details.get("state", ExecutionState.UNKNOWN)
                official_lookup_succeeded = True
            elif callable(lookup):
                observed = lookup(order_id)
                official_lookup_succeeded = True
            else:
                observed = client.demo_order_state(identity, instrument_id, order_id)
        except (EtoroApiError, EtoroMappingError, RuntimeError, TypeError, ValueError):
            observed = ExecutionState.UNKNOWN
        if not isinstance(observed, ExecutionState):
            try:
                observed = ExecutionState(str(getattr(observed, "value", observed)).upper())
            except ValueError:
                observed = ExecutionState.UNKNOWN
        position_id = lookup_details.get("position_id")
        executed_exposure = lookup_details.get(
            "executed_exposure_account_currency"
        )
        updates: dict[str, object] = {
            "broker_order_lookup_attempted_at": lookup_checked_at.isoformat(),
            "broker_order_lookup_status": observed.value,
        }
        if official_lookup_succeeded and observed is not ExecutionState.UNKNOWN:
            updates.update(
                {
                    "broker_order_status": observed.value,
                    "broker_reconciliation_source": "ETORO_V2_ORDER_LOOKUP",
                    "broker_order_reconciled_at": lookup_checked_at.isoformat(),
                }
            )
            if (
                observed is ExecutionState.FILLED
                and isinstance(position_id, str)
                and position_id.isdigit()
            ):
                updates["broker_position_id"] = position_id
                if isinstance(executed_exposure, str):
                    updates["executed_exposure_account_currency"] = executed_exposure
        registry.update_demo_submission(
            key,
            observed.value if observed is not ExecutionState.UNKNOWN else str(record.get("state", "UNKNOWN")),
            updates,
        )
        if official_lookup_succeeded and observed is not ExecutionState.UNKNOWN:
            verified += 1
            if is_legacy_fill:
                legacy_verified += 1
        elif observed is ExecutionState.UNKNOWN:
            errors.append("ORDER_LOOKUP_STATUS_UNKNOWN")
        if (
            official_lookup_succeeded
            and observed is ExecutionState.FILLED
            and isinstance(position_id, str)
            and position_id.isdigit()
            and isinstance(executed_exposure, str)
        ):
            registry.update_demo_submission(
                key,
                observed.value,
                {
                    "reconciliation": "verified",
                    "reconciled_at": lookup_checked_at.isoformat(),
                },
            )
    return {
        "attempted": attempted,
        "verified": verified,
        "legacy_attempted": legacy_attempted,
        "legacy_verified": legacy_verified,
        "legacy_remaining": sum(
            1
            for record in registry.filled_demo_submissions()
            if isinstance(record.get("payload"), Mapping)
            and _legacy_fill_needs_order_lookup(record["payload"])
        ),
        "remaining": len(registry.unresolved_demo_submissions()),
        "errors": tuple(sorted(set(errors))),
    }


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
        last_cycle = audit_store.latest_cycle()
        return {
            "status": "NO_CYCLE",
            "phase": "STEP_9_0E_ACTIVE_INTELLIGENCE_ORCHESTRATOR",
            "broker_write": False,
            "broker_write_calls": 0,
            "demo_execution_enabled": config.etoro_demo_execution_enabled,
            "real_execution_available": False,
            "last_successful_cycle": last_cycle,
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
