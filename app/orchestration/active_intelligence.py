"""Offline active intelligence orchestration for continuous shadow scanning."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame
from app.news.intelligence import (
    GlobalNewsIntelligenceEngine,
    NewsFeedProvider,
    NewsProviderStatus,
)
from app.scanner.active import (
    ActiveMarketScanner,
    ActiveScannerResult,
    _expected_completed_one_hour_bar_timestamp,
    _is_market_closed_as_of,
    attach_observational_news_context,
)
from app.storage.sqlite import ActiveOrchestrationClaim, SqliteRecordStore

ACTIVE_INTELLIGENCE_ORCHESTRATOR_VERSION = "active-intelligence-orchestrator-v1"
DEFAULT_ACTIVE_INTELLIGENCE_STORE_PATH = Path("work") / "active-intelligence-cycles.sqlite3"


class CycleChangeClassification(StrEnum):
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"
    MARKET_STATE_CHANGED = "MARKET_STATE_CHANGED"
    NEWS_STATE_CHANGED = "NEWS_STATE_CHANGED"
    GLOBAL_RISK_CHANGED = "GLOBAL_RISK_CHANGED"
    POSITION_STATE_CHANGED = "POSITION_STATE_CHANGED"
    MULTIPLE_CHANGES = "MULTIPLE_CHANGES"


class DataHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class DuplicateCycleError(ValueError):
    pass


class ActiveIntelligenceCycleRecord(FrozenDomainModel):
    cycle_id: str = Field(min_length=16)
    orchestrator_version: str = ACTIVE_INTELLIGENCE_ORCHESTRATOR_VERSION
    scheduled_at: datetime
    started_at: datetime
    completed_at: datetime
    market_data_timestamp: datetime | None = None
    news_cutoff_timestamp: datetime
    symbols_evaluated: tuple[str, ...]
    positions_monitored: int = Field(ge=0)
    fresh_news_events: int = Field(ge=0)
    duplicate_events_ignored: int = Field(ge=0)
    material_events: int = Field(ge=0)
    global_risk_context: dict[str, object]
    top_opportunities: tuple[str, ...]
    watchlist: tuple[str, ...]
    no_trade: tuple[str, ...]
    rejected: tuple[str, ...]
    data_health_state: DataHealthState
    decision_change_events: tuple[str, ...]
    change_classification: CycleChangeClassification
    scanner_result: dict[str, object]
    shadow_capital: Decimal = Field(gt=0)
    available_simulated_cash: Decimal = Field(ge=0)
    existing_exposure: Decimal = Field(ge=0)
    allocation_diagnostics: tuple[dict[str, object], ...]
    broker_write_calls: int = Field(default=0, ge=0, le=0)
    demo_execution_enabled: bool = False
    real_execution_available: bool = False
    scan_cycle_timestamp: datetime | None = None
    news_provider: str = "unknown"
    news_provider_status: str = NewsProviderStatus.PROVIDER_UNAVAILABLE.value
    news_scan_completed_at: datetime | None = None
    news_events_received: int = Field(default=0, ge=0)
    news_events_fresh: int = Field(default=0, ge=0)
    news_events_material: int = Field(default=0, ge=0)
    news_duplicates_ignored: int = Field(default=0, ge=0)
    news_acquisition_error_code: str | None = None
    news_acquisition_error_detail_safe: str | None = None
    news_provider_diagnostics: dict[str, object] = Field(default_factory=dict)
    news_event_digest: tuple[dict[str, object], ...] = ()
    news_asset_contexts: dict[str, dict[str, object]] = Field(default_factory=dict)

    @field_validator(
        "scheduled_at",
        "started_at",
        "completed_at",
        "news_cutoff_timestamp",
        "scan_cycle_timestamp",
        "news_scan_completed_at",
    )
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "active intelligence cycle timestamp")

    @field_validator("market_data_timestamp")
    @classmethod
    def market_timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "market data timestamp")


class ActiveIntelligenceAuditStore:
    def __init__(self, store: SqliteRecordStore) -> None:
        self._store = store

    def append_cycle(self, cycle: ActiveIntelligenceCycleRecord) -> int:
        return self._store.append("active-intelligence-cycle", cycle.model_dump(mode="json"))

    def cycles(self) -> tuple[dict[str, object], ...]:
        return self._store.list("active-intelligence-cycle")

    def accepted_active_cycles(self) -> dict[str, datetime]:
        return self._store.accepted_active_cycles()

    def watermarks(self) -> dict[tuple[str, str], datetime]:
        return self._store.active_cycle_watermarks()

    def active_claim(self, claim_name: str) -> ActiveOrchestrationClaim | None:
        return self._store.active_claim(claim_name)

    def acquire_claim(
        self,
        *,
        claim_name: str,
        owner_token: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> ActiveOrchestrationClaim | None:
        return self._store.acquire_active_claim(
            claim_name=claim_name,
            owner_token=owner_token,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )

    def release_claim(
        self,
        *,
        claim: ActiveOrchestrationClaim,
        released_at: datetime,
    ) -> bool:
        return self._store.release_active_claim(
            claim_name=claim.claim_name,
            owner_token=claim.owner_token,
            generation=claim.generation,
            released_at=released_at,
        )

    def finalize_cycle(
        self,
        *,
        claim: ActiveOrchestrationClaim,
        cycle: ActiveIntelligenceCycleRecord,
        watermarks: Mapping[tuple[str, str], datetime],
        finalized_at: datetime,
    ) -> bool:
        return self._store.finalize_active_cycle(
            claim=claim,
            cycle_id=cycle.cycle_id,
            scan_cycle_timestamp=cycle.scan_cycle_timestamp or cycle.scheduled_at,
            payload=cycle.model_dump(mode="json"),
            watermarks=watermarks,
            finalized_at=finalized_at,
        )


def default_active_intelligence_audit_store(
    path: Path | None = None,
) -> ActiveIntelligenceAuditStore:
    return ActiveIntelligenceAuditStore(
        SqliteRecordStore(path or DEFAULT_ACTIVE_INTELLIGENCE_STORE_PATH)
    )


class AegisActiveIntelligenceOrchestrator:
    """Coordinates market scanner and news intelligence without execution access."""

    def __init__(
        self,
        *,
        scanner: ActiveMarketScanner | None = None,
        news_engine: GlobalNewsIntelligenceEngine | None = None,
        cadence: timedelta = timedelta(minutes=10),
        audit_store: ActiveIntelligenceAuditStore | None = None,
    ) -> None:
        if cadence <= timedelta(0):
            raise ValueError("cadence must be positive")
        self._scanner = scanner or ActiveMarketScanner(minimum_bars=60)
        self._news = news_engine or GlobalNewsIntelligenceEngine(NewsFeedProvider())
        self._cadence = cadence
        self._store = audit_store
        self._seen_cycle_ids: set[str] = set()
        self._previous: ActiveIntelligenceCycleRecord | None = None
        self._last_scanner_result: ActiveScannerResult | None = None

    @property
    def cadence(self) -> timedelta:
        return self._cadence

    @property
    def last_scanner_result(self) -> ActiveScannerResult | None:
        return self._last_scanner_result

    def schedule(self, *, start: datetime, end: datetime) -> tuple[datetime, ...]:
        require_aware(start, "schedule start")
        require_aware(end, "schedule end")
        if end < start:
            raise ValueError("schedule end must be >= start")
        rows: list[datetime] = []
        current = start
        while current <= end:
            rows.append(current)
            current += self._cadence
        return tuple(rows)

    def run_cycle(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal = Decimal("200"),
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ActiveIntelligenceCycleRecord:
        scheduled_at = require_aware(scheduled_at, "cycle schedule timestamp")
        cycle_id = _cycle_id(
            scheduled_at=scheduled_at, instruments=instruments, timeframe=timeframe
        )
        if cycle_id in self._seen_cycle_ids:
            raise DuplicateCycleError("duplicate cycle for same schedule/symbol/timeframe")
        self._seen_cycle_ids.add(cycle_id)

        visible_bars = _visible_bars_by_symbol(
            bars_by_symbol=bars_by_symbol,
            instruments=instruments,
            as_of=scheduled_at,
            timeframe=timeframe,
        )
        scanner_result = self._scanner.scan(
            instruments=instruments,
            bars_by_symbol=visible_bars,
            portfolio=portfolio,
            as_of=scheduled_at,
            timeframe=timeframe,
            simulated_capital=shadow_capital,
        )
        news_result = self._news.analyze(instruments=instruments, as_of=scheduled_at)
        scanner_result = attach_observational_news_context(
            scanner_result, news_result.asset_contexts
        )
        self._last_scanner_result = scanner_result
        record = ActiveIntelligenceCycleRecord(
            cycle_id=cycle_id,
            scheduled_at=scheduled_at,
            started_at=started_at or scheduled_at,
            completed_at=completed_at or scheduled_at,
            market_data_timestamp=_latest_market_timestamp(visible_bars),
            news_cutoff_timestamp=scheduled_at,
            symbols_evaluated=tuple(instrument.symbol for instrument in instruments),
            positions_monitored=scanner_result.existing_positions_monitored,
            fresh_news_events=len(news_result.normalized_events),
            duplicate_events_ignored=len(news_result.normalized_events)
            - len(news_result.event_clusters),
            material_events=sum(
                1
                for cluster in news_result.event_clusters
                if cluster.canonical_event.impact_score >= Decimal("0.70")
            ),
            global_risk_context=news_result.global_risk_snapshot.model_dump(mode="json"),
            top_opportunities=tuple(item.symbol for item in scanner_result.top_opportunities),
            watchlist=tuple(item.symbol for item in scanner_result.watchlist),
            no_trade=tuple(item.symbol for item in scanner_result.no_trade),
            rejected=tuple(item.symbol for item in scanner_result.rejected),
            data_health_state=_data_health(scanner_result),
            decision_change_events=_decision_changes(
                previous=self._previous,
                current=scanner_result,
                news_result=news_result,
            ),
            change_classification=CycleChangeClassification.NO_MATERIAL_CHANGE,
            scanner_result=_scanner_payload(scanner_result),
            shadow_capital=shadow_capital,
            available_simulated_cash=portfolio.cash,
            existing_exposure=portfolio.positions_value,
            allocation_diagnostics=_allocation_diagnostics(scanner_result),
            broker_write_calls=0,
            news_provider=news_result.provider_name,
            news_provider_status=news_result.provider_status.value,
            news_scan_completed_at=completed_at or scheduled_at,
            news_events_received=len(news_result.normalized_events),
            news_events_fresh=len(news_result.normalized_events),
            news_events_material=sum(
                1
                for cluster in news_result.event_clusters
                if cluster.canonical_event.impact_score >= Decimal("0.70")
            ),
            news_duplicates_ignored=len(news_result.normalized_events)
            - len(news_result.event_clusters),
            news_acquisition_error_code=news_result.provider_error_code,
            news_acquisition_error_detail_safe=news_result.provider_error_detail_safe,
            news_provider_diagnostics=news_result.provider_diagnostics,
            news_event_digest=_news_event_digest(news_result),
            news_asset_contexts=_news_asset_contexts(news_result),
        )
        classification = _classify_change(previous=self._previous, current=record)
        record = record.model_copy(update={"change_classification": classification})
        self._previous = record
        if self._store is not None:
            self._store.append_cycle(record)
        return record

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
    ) -> ActiveIntelligenceCycleRecord | None:
        """Run one durable, causal cycle when a new completed bar exists.

        ``None`` is the deterministic NO_CYCLE result.  This method is kept
        separate from ``run_cycle`` until the runtime wiring substep.
        """
        if self._store is None:
            raise RuntimeError("causal cycles require a persistent audit store")
        if timeframe is not TimeFrame.ONE_HOUR:
            raise ValueError("causal active cycles currently require the 1H timeframe")
        cycle_as_of = require_aware(scheduled_at, "cycle schedule timestamp")
        scoped_instruments = tuple(
            instrument
            for instrument in instruments
            if asset_classes is None or instrument.asset_class in asset_classes
        )
        if not scoped_instruments:
            return None
        scoped_bars = {
            instrument.symbol: bars_by_symbol.get(instrument.symbol, ())
            for instrument in scoped_instruments
        }
        scope_key = "ALL" if asset_classes is None else "+".join(
            sorted(asset_class.value for asset_class in asset_classes)
        )
        # Preserve the existing claim name for the mixed cycle so persisted
        # leases and older runners remain compatible. Scoped cycles get their
        # own lease and can progress independently by asset class.
        claim_name = "active-intelligence:1h" if asset_classes is None else f"active-intelligence:1h:{scope_key}"
        eligible = _causal_latest_bars(
            bars_by_symbol=scoped_bars,
            instruments=scoped_instruments,
            as_of=cycle_as_of,
            timeframe=timeframe,
        )
        current_watermarks = self._store.watermarks()
        universe_reconciliation_required = (
            force_universe_reconciliation
            and asset_classes is None
            and _universe_expanded_since_last_cycle(
                self._store.cycles(),
                current_symbols={instrument.symbol for instrument in scoped_instruments},
            )
        )
        if not universe_reconciliation_required and not _has_new_bar(
            eligible, current_watermarks, timeframe
        ):
            return None

        owner_token = uuid.uuid4().hex
        claim = self._store.acquire_claim(
            claim_name=claim_name,
            owner_token=owner_token,
            acquired_at=cycle_as_of,
            expires_at=cycle_as_of + self._cadence,
        )
        if claim is None:
            return None
        try:
            current_watermarks = self._store.watermarks()
            eligible = _causal_latest_bars(
                bars_by_symbol=scoped_bars,
                instruments=scoped_instruments,
                as_of=cycle_as_of,
                timeframe=timeframe,
            )
            if not universe_reconciliation_required and not _has_new_bar(
                eligible, current_watermarks, timeframe
            ):
                self._store.release_claim(claim=claim, released_at=cycle_as_of)
                return None
            record = self._evaluate_cycle(
                scheduled_at=cycle_as_of,
                instruments=scoped_instruments,
                bars_by_symbol=scoped_bars,
                portfolio=portfolio,
                timeframe=timeframe,
                shadow_capital=shadow_capital,
                started_at=started_at,
                completed_at=completed_at,
            )
            watermarks = {
                (symbol, timeframe.value): bar.timestamp
                for symbol, bar in eligible.items()
                if current_watermarks.get((symbol, timeframe.value)) is None
                or bar.timestamp > current_watermarks[(symbol, timeframe.value)]
            }
            record = record.model_copy(update={"scan_cycle_timestamp": cycle_as_of})
            if not self._store.finalize_cycle(
                claim=claim,
                cycle=record,
                watermarks=watermarks,
                finalized_at=cycle_as_of,
            ):
                raise RuntimeError("causal cycle was not accepted")
            self._previous = record
            return record
        except BaseException:
            if self._store.active_claim(claim_name) == claim:
                self._store.release_claim(claim=claim, released_at=cycle_as_of)
            raise

    def _evaluate_cycle(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal,
        started_at: datetime | None,
        completed_at: datetime | None,
    ) -> ActiveIntelligenceCycleRecord:
        visible_bars = _visible_bars_by_symbol(
            bars_by_symbol=bars_by_symbol,
            instruments=instruments,
            as_of=scheduled_at,
            timeframe=timeframe,
        )
        news_result = self._news.analyze(instruments=instruments, as_of=scheduled_at)
        scanner_result = self._scanner.scan(
            instruments=instruments,
            bars_by_symbol=visible_bars,
            portfolio=portfolio,
            as_of=scheduled_at,
            timeframe=timeframe,
            simulated_capital=shadow_capital,
            news_context_by_symbol=news_result.asset_contexts,
        )
        self._last_scanner_result = scanner_result
        cycle_id = _cycle_id(
            scheduled_at=scheduled_at, instruments=instruments, timeframe=timeframe
        )
        record = ActiveIntelligenceCycleRecord(
            cycle_id=cycle_id,
            scheduled_at=scheduled_at,
            started_at=started_at or scheduled_at,
            completed_at=completed_at or scheduled_at,
            market_data_timestamp=_latest_market_timestamp(visible_bars),
            news_cutoff_timestamp=scheduled_at,
            symbols_evaluated=tuple(instrument.symbol for instrument in instruments),
            positions_monitored=scanner_result.existing_positions_monitored,
            fresh_news_events=len(news_result.normalized_events),
            duplicate_events_ignored=len(news_result.normalized_events)
            - len(news_result.event_clusters),
            material_events=sum(
                1
                for cluster in news_result.event_clusters
                if cluster.canonical_event.impact_score >= Decimal("0.70")
            ),
            global_risk_context=news_result.global_risk_snapshot.model_dump(mode="json"),
            top_opportunities=tuple(item.symbol for item in scanner_result.top_opportunities),
            watchlist=tuple(item.symbol for item in scanner_result.watchlist),
            no_trade=tuple(item.symbol for item in scanner_result.no_trade),
            rejected=tuple(item.symbol for item in scanner_result.rejected),
            data_health_state=_data_health(scanner_result),
            decision_change_events=_decision_changes(
                previous=self._previous, current=scanner_result, news_result=news_result
            ),
            change_classification=CycleChangeClassification.NO_MATERIAL_CHANGE,
            scanner_result=_scanner_payload(scanner_result),
            shadow_capital=shadow_capital,
            available_simulated_cash=portfolio.cash,
            existing_exposure=portfolio.positions_value,
            allocation_diagnostics=_allocation_diagnostics(scanner_result),
            broker_write_calls=0,
            scan_cycle_timestamp=scheduled_at,
            news_provider=news_result.provider_name,
            news_provider_status=news_result.provider_status.value,
            news_scan_completed_at=completed_at or scheduled_at,
            news_events_received=len(news_result.normalized_events),
            news_events_fresh=len(news_result.normalized_events),
            news_events_material=sum(
                1
                for cluster in news_result.event_clusters
                if cluster.canonical_event.impact_score >= Decimal("0.70")
            ),
            news_duplicates_ignored=len(news_result.normalized_events)
            - len(news_result.event_clusters),
            news_acquisition_error_code=news_result.provider_error_code,
            news_acquisition_error_detail_safe=news_result.provider_error_detail_safe,
            news_provider_diagnostics=news_result.provider_diagnostics,
            news_event_digest=_news_event_digest(news_result),
            news_asset_contexts=_news_asset_contexts(news_result),
        )
        return record.model_copy(
            update={
                "change_classification": _classify_change(previous=self._previous, current=record)
            }
        )


def _cycle_id(
    *,
    scheduled_at: datetime,
    instruments: tuple[UniversalInstrument, ...],
    timeframe: TimeFrame,
) -> str:
    seed = "|".join(
        (
            scheduled_at.isoformat(),
            timeframe.value,
            ",".join(instrument.symbol for instrument in instruments),
        )
    )
    return hashlib.sha256(seed.encode()).hexdigest()


def _causal_latest_bars(
    *,
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
    instruments: tuple[UniversalInstrument, ...],
    as_of: datetime,
    timeframe: TimeFrame,
) -> dict[str, MarketBar]:
    """Select the latest completed bar using scanner temporal semantics."""
    result: dict[str, MarketBar] = {}
    for instrument in instruments:
        bars = causal_completed_bars(
            bars_by_symbol=bars_by_symbol.get(instrument.symbol, ()),
            instrument=instrument,
            as_of=as_of,
            timeframe=timeframe,
        )
        if bars:
            result[instrument.symbol] = max(bars, key=lambda bar: bar.timestamp)
    return result


def causal_completed_bars(
    *,
    bars_by_symbol: tuple[MarketBar, ...],
    instrument: UniversalInstrument,
    as_of: datetime,
    timeframe: TimeFrame,
) -> tuple[MarketBar, ...]:
    """Return bars visible to A4C, excluding current/incomplete equity bars."""
    bars = tuple(
        bar for bar in bars_by_symbol if bar.timeframe is timeframe and bar.timestamp <= as_of
    )
    if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
        if not _is_market_closed_as_of(as_of):
            cutoff = _expected_completed_one_hour_bar_timestamp(as_of)
            bars = tuple(bar for bar in bars if bar.timestamp <= cutoff)
    return bars


def _has_new_bar(
    eligible: Mapping[str, MarketBar],
    watermarks: Mapping[tuple[str, str], datetime],
    timeframe: TimeFrame,
) -> bool:
    return any(
        watermarks.get((symbol, timeframe.value)) is None
        or bar.timestamp > watermarks[(symbol, timeframe.value)]
        for symbol, bar in eligible.items()
    )


def _universe_expanded_since_last_cycle(
    cycles: tuple[dict[str, object], ...],
    *,
    current_symbols: set[str],
) -> bool:
    """Detect a stale narrow cycle after a scanner-universe expansion.

    This is a one-time reconciliation trigger: it re-evaluates the current
    causal bars when the last accepted cycle covered only a strict subset of
    the now-active universe. It never invents a bar and it does not lower the
    data or execution gates; it only prevents an old eight-symbol cycle from
    remaining the dashboard's last global comparison forever.
    """
    if not cycles or not current_symbols:
        return False
    raw_symbols = cycles[-1].get("symbols_evaluated")
    if not isinstance(raw_symbols, (list, tuple, set, frozenset)):
        return False
    previous_symbols = {str(symbol) for symbol in raw_symbols if str(symbol)}
    return bool(previous_symbols) and previous_symbols < current_symbols


def _visible_bars_by_symbol(
    *,
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
    instruments: tuple[UniversalInstrument, ...],
    as_of: datetime,
    timeframe: TimeFrame,
) -> dict[str, tuple[MarketBar, ...]]:
    return {
        instrument.symbol: tuple(
            bar
            for bar in bars_by_symbol.get(instrument.symbol, ())
            if bar.timestamp <= as_of and bar.timeframe is timeframe
        )
        for instrument in instruments
    }


def _latest_market_timestamp(visible_bars: Mapping[str, tuple[MarketBar, ...]]) -> datetime | None:
    timestamps = tuple(bar.timestamp for bars in visible_bars.values() for bar in bars)
    return max(timestamps) if timestamps else None


def _data_health(scanner_result: ActiveScannerResult) -> DataHealthState:
    rejected_reasons = tuple(
        reason for item in scanner_result.rejected for reason in item.rejection_reasons
    )
    if any("PROVIDER_UNAVAILABLE" in reason for reason in rejected_reasons):
        return DataHealthState.PROVIDER_UNAVAILABLE
    if any("STALE" in reason for reason in rejected_reasons):
        return DataHealthState.STALE
    if scanner_result.rejected:
        return DataHealthState.PARTIAL
    return DataHealthState.HEALTHY


def _decision_changes(
    *,
    previous: ActiveIntelligenceCycleRecord | None,
    current: ActiveScannerResult,
    news_result: object,
) -> tuple[str, ...]:
    if previous is None:
        return ("INITIAL_CYCLE",)
    changes: list[str] = []
    previous_candidates = _candidate_bucket_map(previous.scanner_result)
    current_candidates = {item.symbol: item.bucket.value for item in current.candidates}
    for symbol, bucket in current_candidates.items():
        if previous_candidates.get(symbol) != bucket:
            changes.append(f"{symbol}:bucket:{previous_candidates.get(symbol)}->{bucket}")
    previous_events = int(previous.fresh_news_events)
    current_events = len(getattr(news_result, "normalized_events", ()))
    if current_events != previous_events:
        changes.append(f"news_events:{previous_events}->{current_events}")
    return tuple(changes) or ("NO_DECISION_CHANGE",)


def _classify_change(
    *,
    previous: ActiveIntelligenceCycleRecord | None,
    current: ActiveIntelligenceCycleRecord,
) -> CycleChangeClassification:
    if previous is None:
        return CycleChangeClassification.MULTIPLE_CHANGES
    changes = []
    if previous.market_data_timestamp != current.market_data_timestamp:
        changes.append(CycleChangeClassification.MARKET_STATE_CHANGED)
    if previous.fresh_news_events != current.fresh_news_events:
        changes.append(CycleChangeClassification.NEWS_STATE_CHANGED)
    if _comparable_global_risk(previous.global_risk_context) != _comparable_global_risk(
        current.global_risk_context
    ):
        changes.append(CycleChangeClassification.GLOBAL_RISK_CHANGED)
    if previous.positions_monitored != current.positions_monitored:
        changes.append(CycleChangeClassification.POSITION_STATE_CHANGED)
    if len(set(changes)) > 1:
        return CycleChangeClassification.MULTIPLE_CHANGES
    return changes[0] if changes else CycleChangeClassification.NO_MATERIAL_CHANGE


def _comparable_global_risk(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "as_of"}


def _candidate_bucket_map(payload: Mapping[str, object]) -> dict[str, str]:
    rows = payload.get("candidates", ())
    if not isinstance(rows, tuple | list):
        return {}
    result: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict):
            symbol = row.get("symbol")
            bucket = row.get("bucket")
            if isinstance(symbol, str) and isinstance(bucket, str):
                result[symbol] = bucket
    return result


def _scanner_payload(scanner_result: ActiveScannerResult) -> dict[str, object]:
    return {
        "as_of": scanner_result.as_of.isoformat(),
        "timeframe": scanner_result.timeframe.value,
        "candidates": tuple(
            {
                "symbol": item.symbol,
                "bucket": item.bucket.value,
                "decision": item.decision.value,
                "opportunity_score": str(item.opportunity_score),
                "confidence": str(item.confidence),
                "news_sentiment": item.news_sentiment,
                "news_relevance": str(item.news_relevance),
                "material_event_count": item.material_event_count,
                "rejection_reasons": item.rejection_reasons,
            }
            for item in scanner_result.candidates
        ),
        "broker_write_calls": scanner_result.broker_write_calls,
    }


def _news_event_digest(news_result: object, *, limit: int = 20) -> tuple[dict[str, object], ...]:
    """Persist a bounded, sanitized news view for the read-only dashboard."""
    clusters = getattr(news_result, "event_clusters", ())
    ordered = sorted(
        clusters,
        key=lambda cluster: (
            getattr(getattr(cluster, "canonical_event", None), "impact_score", Decimal("0")),
            getattr(cluster, "first_publication_time", datetime.min),
        ),
        reverse=True,
    )
    digest: list[dict[str, object]] = []
    for cluster in ordered[:limit]:
        event = cluster.canonical_event
        digest.append(
            {
                "headline": event.headline,
                "category": event.event_category.value,
                "sentiment": event.sentiment.value,
                "published_at": event.published_at.isoformat(),
                "source": event.source,
                "linked_symbols": tuple(
                    link.symbol for link in event.companies_assets_affected
                ),
                "asset_classes": tuple(
                    asset_class.value for asset_class in event.asset_classes_affected
                ),
                "impact_score": str(event.impact_score),
                "confidence": str(event.confidence),
                "corroboration_count": cluster.corroboration_count,
                "sources": tuple(cluster.sources),
            }
        )
    return tuple(digest)


def _news_asset_contexts(news_result: object) -> dict[str, dict[str, object]]:
    """Persist per-instrument news context without raw provider payloads."""
    contexts = getattr(news_result, "asset_contexts", {})
    if not isinstance(contexts, Mapping):
        return {}
    result: dict[str, dict[str, object]] = {}
    for symbol, context in contexts.items():
        payload = context.model_dump(mode="json") if hasattr(context, "model_dump") else context
        if not isinstance(payload, dict):
            continue
        result[str(symbol)] = {
            "asset_class": payload.get("asset_class"),
            "freshness": payload.get("freshness"),
            "aggregate_sentiment": payload.get("aggregate_sentiment"),
            "aggregate_relevance": payload.get("aggregate_relevance"),
            "event_risk": payload.get("event_risk"),
            "unique_event_count": payload.get("unique_event_count", 0),
            "material_event_count": payload.get("material_event_count", 0),
            "latest_material_event_timestamp": payload.get(
                "latest_material_event_timestamp"
            ),
            "event_summaries": tuple(payload.get("event_summaries", ()))[:3],
            "news_risk_flags": tuple(payload.get("news_risk_flags", ())),
            "explanation": payload.get("explanation"),
        }
    return result


def _allocation_diagnostics(
    scanner_result: ActiveScannerResult,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "symbol": item.symbol,
            "bucket": item.bucket.value,
            "affordable_fractionally": item.affordable_fractionally,
            "proposed_capital_allocation": str(item.proposed_capital_allocation),
            "remaining_simulated_cash": str(item.remaining_simulated_cash),
            "existing_exposure": str(item.existing_exposure),
        }
        for item in scanner_result.top_opportunities
    )
