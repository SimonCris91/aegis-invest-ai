"""Broker-neutral active market scanner foundation."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.data.historical.cache import HistoricalDataCache
from app.data.quality import HistoricalDataQualityAnalyzer
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.market import MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import (
    CandidateState,
    DataQualityStatus,
    OpportunityCandidate,
    OpportunityFeatures,
    UniversalInstrument,
)
from app.domain.versions import RANKING_VERSION, SCANNER_VERSION
from app.intelligence.models import (
    AegisDecision,
    FeatureQuality,
    MarketBar,
    NewsSignal,
    NewsSignalStatus,
    TimeFrame,
)
from app.intelligence.service import AegisOpportunityIntelligenceEngine
from app.policies.defaults import DEFAULT_POLICY_VERSION

ACTIVE_SCANNER_VERSION = "active-market-scanner-v3-multiasset-fair"

_CRYPTO_QUOTE_ASSETS = tuple(
    sorted(
        {
            "USDT",
            "USDC",
            "USD",
            "EUR",
            "GBP",
            "AUD",
            "CAD",
            "CHF",
            "JPY",
            "NZD",
            "HKD",
            "SGD",
            "CNH",
            "CNY",
            "BRL",
            "MXN",
            "TRY",
            "ZAR",
            "SEK",
            "NOK",
            "DKK",
            "PLN",
            "THB",
            "BTC",
            "ETH",
            "BNB",
            "SOL",
            "XRP",
            "ADA",
            "DOT",
        },
        key=len,
        reverse=True,
    )
)


class ActiveScannerBucket(StrEnum):
    TOP_OPPORTUNITIES = "TOP_OPPORTUNITIES"
    WATCHLIST = "WATCHLIST"
    NO_TRADE = "NO_TRADE"
    REJECTED = "REJECTED"


class TimeframeReadinessStatus(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    BLOCKED = "BLOCKED"


class IntradayFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    DELAYED = "DELAYED"
    STALE = "STALE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MARKET_CLOSED = "MARKET_CLOSED"


class ScannerEntryEligibilityReason(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    STALE = "STALE"
    FUTURE_BAR = "FUTURE_BAR"
    INCOMPLETE_BAR = "INCOMPLETE_BAR"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    INVALID_DATA = "INVALID_DATA"
    MIXED_TIMESTAMP_UNSAFE = "MIXED_TIMESTAMP_UNSAFE"


class ActiveScannerCandidate(FrozenDomainModel):
    symbol: str = Field(min_length=1)
    full_asset_name: str
    asset_class: AssetClass
    timestamp: datetime
    timeframe: TimeFrame
    current_market_state: MarketStatus
    opportunity_score: Decimal = Field(ge=0, le=100)
    confidence: Decimal = Field(ge=0, le=1)
    regime: tuple[str, ...]
    decision: AegisDecision
    bucket: ActiveScannerBucket
    major_positive_factors: tuple[str, ...] = ()
    major_negative_factors: tuple[str, ...] = ()
    data_quality_state: FeatureQuality
    risk_flags: tuple[str, ...] = ()
    current_position_state: str
    freshness: str
    provider_provenance: tuple[str, ...]
    affordable_fractionally: bool
    proposed_capital_allocation: Decimal = Field(ge=0)
    remaining_simulated_cash: Decimal = Field(ge=0)
    existing_exposure: Decimal = Field(ge=0)
    diversification_concentration_impact: str
    rejection_reasons: tuple[str, ...] = ()
    rank: int | None = Field(default=None, ge=1)
    news_context: dict[str, object] | None = None
    news_sentiment: str = "NEWS_SOURCE_UNAVAILABLE"
    news_relevance: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    material_event_count: int = Field(default=0, ge=0)
    headline_event_summaries: tuple[str, ...] = ()
    news_risk_flags: tuple[str, ...] = ()

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "active scanner timestamp")


class ActiveScannerResult(FrozenDomainModel):
    scanner_version: str = ACTIVE_SCANNER_VERSION
    as_of: datetime
    timeframe: TimeFrame
    simulated_capital: Decimal = Field(gt=0)
    candidates: tuple[ActiveScannerCandidate, ...]
    top_opportunities: tuple[ActiveScannerCandidate, ...]
    watchlist: tuple[ActiveScannerCandidate, ...]
    no_trade: tuple[ActiveScannerCandidate, ...]
    rejected: tuple[ActiveScannerCandidate, ...]
    duplicate_decisions_prevented: int = Field(ge=0)
    existing_positions_monitored: int = Field(ge=0)
    broker_write_calls: int = Field(default=0, ge=0, le=0)
    demo_execution_enabled: bool = False
    real_execution_available: bool = False

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "active scanner result timestamp")


class ScannerObservation(FrozenDomainModel):
    symbol: str = Field(min_length=1)
    full_name: str = Field(min_length=1)
    asset_class: AssetClass
    scan_cycle_timestamp: datetime
    bar_timestamp: datetime
    timeframe: TimeFrame
    current_price: Decimal = Field(gt=0)
    opportunity_score: Decimal = Field(ge=0, le=100)
    confidence: Decimal = Field(ge=0, le=1)
    regime: tuple[str, ...]
    action_state: str = Field(min_length=1)
    data_quality: str = Field(min_length=1)
    provider_provenance: tuple[str, ...]
    freshness_state: str = Field(min_length=1)
    market_session_state: str = Field(min_length=1)
    risk_flags: tuple[str, ...] = ()
    existing_position_state: str = Field(min_length=1)
    eligible_for_entry_comparison: bool
    eligibility_reason_code: ScannerEntryEligibilityReason
    duplicate_evaluation_key: str = Field(min_length=1)
    current_market_state: MarketStatus

    @field_validator("scan_cycle_timestamp", "bar_timestamp")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "scanner observation timestamp")


class ActiveScannerCrossAssetSnapshot(FrozenDomainModel):
    scan_cycle_timestamp: datetime
    entry_candidates: tuple[ScannerObservation, ...]
    entry_exclusions: tuple[ScannerObservation, ...]
    positions_to_manage: tuple[ScannerObservation, ...]
    duplicate_evaluations_prevented: int = Field(ge=0)
    broker_write_calls: int = Field(default=0, ge=0, le=0)

    @field_validator("scan_cycle_timestamp")
    @classmethod
    def scan_cycle_timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "scanner cycle timestamp")


def _fair_asset_class_order(
    candidates: tuple[ActiveScannerCandidate, ...],
) -> tuple[ActiveScannerCandidate, ...]:
    """Interleave score-ranked candidates so one class cannot fill a bounded lane."""
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


class ActiveMarketScanner:
    """Scans cached market data and ranks opportunities without execution access."""

    def __init__(
        self,
        *,
        intelligence_engine: AegisOpportunityIntelligenceEngine | None = None,
        quality_analyzer: HistoricalDataQualityAnalyzer | None = None,
        minimum_bars: int = 60,
        top_n: int = 5,
    ) -> None:
        if minimum_bars <= 0:
            raise ValueError("minimum_bars must be positive")
        if top_n <= 0:
            raise ValueError("top_n must be positive")
        self._intelligence = intelligence_engine or AegisOpportunityIntelligenceEngine(
            confidence_profile="V2_B_GUARDED"
        )
        self._quality = quality_analyzer or HistoricalDataQualityAnalyzer(minimum_bars=minimum_bars)
        self._minimum_bars = minimum_bars
        self._top_n = top_n

    def scan(
        self,
        *,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: dict[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        as_of: datetime,
        timeframe: TimeFrame,
        simulated_capital: Decimal = Decimal("200"),
        news_context_by_symbol: Mapping[str, object] | None = None,
    ) -> ActiveScannerResult:
        seen: set[tuple[str, str, str, str]] = set()
        duplicate_decisions_prevented = 0
        raw: list[ActiveScannerCandidate] = []
        for instrument in instruments:
            bars = tuple(
                bar
                for bar in bars_by_symbol.get(instrument.symbol, ())
                if bar.timestamp <= as_of and bar.timeframe is timeframe
            )
            if bars:
                key = (
                    instrument.symbol,
                    timeframe.value,
                    as_of.isoformat(),
                    bars[-1].timestamp.isoformat(),
                )
                if key in seen:
                    duplicate_decisions_prevented += 1
                    continue
                seen.add(key)
            raw.append(
                self._candidate(
                    instrument=instrument,
                    bars=bars,
                    portfolio=portfolio,
                    as_of=as_of,
                    timeframe=timeframe,
                    simulated_capital=simulated_capital,
                    news_context=(
                        None
                        if news_context_by_symbol is None
                        else news_context_by_symbol.get(instrument.symbol)
                    ),
                )
            )
        ordered = sorted(
            raw,
            key=lambda item: (
                item.bucket == ActiveScannerBucket.TOP_OPPORTUNITIES,
                item.opportunity_score,
                item.confidence,
                item.symbol,
            ),
            reverse=True,
        )
        ranked = tuple(
            item.model_copy(update={"rank": index}) for index, item in enumerate(ordered, start=1)
        )
        top_candidates = _deduplicate_crypto_quote_pairs(
            tuple(item for item in ranked if item.bucket is ActiveScannerBucket.TOP_OPPORTUNITIES)
        )
        top_candidates = _fair_asset_class_order(top_candidates)[: self._top_n]
        watchlist_candidates = _deduplicate_crypto_quote_pairs(
            tuple(item for item in ranked if item.bucket is ActiveScannerBucket.WATCHLIST)
        )
        watchlist_candidates = _fair_asset_class_order(watchlist_candidates)
        return ActiveScannerResult(
            as_of=as_of,
            timeframe=timeframe,
            simulated_capital=simulated_capital,
            candidates=ranked,
            top_opportunities=top_candidates,
            watchlist=watchlist_candidates,
            no_trade=tuple(item for item in ranked if item.bucket is ActiveScannerBucket.NO_TRADE),
            rejected=tuple(item for item in ranked if item.bucket is ActiveScannerBucket.REJECTED),
            duplicate_decisions_prevented=duplicate_decisions_prevented,
            existing_positions_monitored=sum(
                1
                for instrument in instruments
                if portfolio.positions_for(_instrument_id(instrument))
            ),
        )


    def build_observation_snapshot(
        self,
        *,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        scan_cycle_timestamp: datetime,
        timeframe: TimeFrame,
        simulated_capital: Decimal = Decimal("200"),
        news_context_by_symbol: Mapping[str, object] | None = None,
        precomputed_result: ActiveScannerResult | None = None,
    ) -> ActiveScannerCrossAssetSnapshot:
        scanner_result = precomputed_result or self.scan(
            instruments=instruments,
            bars_by_symbol=dict(bars_by_symbol),
            portfolio=portfolio,
            as_of=scan_cycle_timestamp,
            timeframe=timeframe,
            simulated_capital=simulated_capital,
            news_context_by_symbol=news_context_by_symbol,
        )
        observations = tuple(
            _observation_from_candidate(
                candidate=candidate,
                bars=tuple(
                    bar
                    for bar in bars_by_symbol.get(candidate.symbol, ())
                    if bar.timeframe is timeframe
                ),
                scan_cycle_timestamp=scan_cycle_timestamp,
            )
            for candidate in scanner_result.candidates
        )
        entry_candidates = tuple(
            observation
            for observation in observations
            if observation.eligible_for_entry_comparison
            and observation.eligibility_reason_code is ScannerEntryEligibilityReason.ELIGIBLE
        )
        entry_exclusions = tuple(
            observation
            for observation in observations
            if not (
                observation.eligible_for_entry_comparison
                and observation.eligibility_reason_code is ScannerEntryEligibilityReason.ELIGIBLE
            )
        )
        positions_to_manage = tuple(
            observation
            for observation in observations
            if observation.existing_position_state != "NO_POSITION"
        )
        return ActiveScannerCrossAssetSnapshot(
            scan_cycle_timestamp=scan_cycle_timestamp,
            entry_candidates=entry_candidates,
            entry_exclusions=entry_exclusions,
            positions_to_manage=positions_to_manage,
            duplicate_evaluations_prevented=scanner_result.duplicate_decisions_prevented,
            broker_write_calls=scanner_result.broker_write_calls,
        )

    def _candidate(
        self,
        *,
        instrument: UniversalInstrument,
        bars: tuple[MarketBar, ...],
        portfolio: PortfolioSnapshot,
        as_of: datetime,
        timeframe: TimeFrame,
        simulated_capital: Decimal,
        news_context: object | None = None,
    ) -> ActiveScannerCandidate:
        if len(bars) < self._minimum_bars:
            return _rejected_candidate(
                instrument=instrument,
                as_of=as_of,
                timeframe=timeframe,
                reason="INSUFFICIENT_CACHED_BARS",
                simulated_capital=simulated_capital,
                portfolio=portfolio,
                minimum_bars=self._minimum_bars,
                provider_provenance=_provider_provenance(bars),
                news_context=news_context,
            )
        quality = self._quality.evaluate(
            provider=_primary_provider(bars),
            instrument=instrument,
            timeframe=timeframe,
            bars=bars,
            as_of=as_of,
            expected_currency=instrument.currency,
        )
        freshness_status = classify_intraday_freshness(
            instrument=instrument,
            timeframe=timeframe,
            bars=bars,
            as_of=as_of,
            minimum_bars=self._minimum_bars,
        )
        if freshness_status is IntradayFreshnessStatus.MARKET_CLOSED:
            return _no_trade_market_closed_candidate(
                instrument=instrument,
                bars=bars,
                as_of=as_of,
                timeframe=timeframe,
                simulated_capital=simulated_capital,
                portfolio=portfolio,
                provider_provenance=_provider_provenance(bars),
                news_context=news_context,
            )
        if freshness_status in {
            IntradayFreshnessStatus.STALE,
            IntradayFreshnessStatus.INSUFFICIENT_HISTORY,
            IntradayFreshnessStatus.PROVIDER_UNAVAILABLE,
        }:
            return _rejected_candidate(
                instrument=instrument,
                as_of=as_of,
                timeframe=timeframe,
                reason=freshness_status.value,
                simulated_capital=simulated_capital,
                portfolio=portfolio,
                minimum_bars=self._minimum_bars,
                provider_provenance=_provider_provenance(bars),
                news_context=news_context,
            )
        if quality.status.value in {"INSUFFICIENT", "CONFLICTING"}:
            return _rejected_candidate(
                instrument=instrument,
                as_of=as_of,
                timeframe=timeframe,
                reason=f"DATA_QUALITY_{quality.status.value}",
                simulated_capital=simulated_capital,
                portfolio=portfolio,
                minimum_bars=self._minimum_bars,
                provider_provenance=_provider_provenance(bars),
            )
        latest = bars[-1]
        previous = bars[-2]
        quote = MarketQuote(
            instrument_id=_instrument_id(instrument),
            symbol=instrument.symbol,
            price=latest.close,
            as_of=latest.timestamp,
            currency=latest.currency,
            source=latest.source,
            previous_close=previous.close,
            market_status=_market_status(instrument),
        )
        candidate = OpportunityCandidate(
            candidate_id=f"active-{instrument.symbol}-{latest.timestamp.isoformat()}",
            broker=instrument.broker,
            instrument=instrument.model_copy(
                update={
                    "last_price": latest.close,
                    "price_timestamp": latest.timestamp,
                    "market_status": quote.market_status,
                }
            ),
            asset_class=instrument.asset_class,
            market_status=quote.market_status,
            quote=quote,
            broker_eligibility=instrument.broker_eligibility,
            policy_allowed=instrument.asset_class
            in {AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO},
            policy_version=DEFAULT_POLICY_VERSION,
            candidate_state=CandidateState.OPEN_AND_ALLOWED,
            data_quality=DataQualityStatus.GOOD,
            candidate_score=Decimal("0"),
            features=OpportunityFeatures(
                mid_price=latest.close,
                short_term_momentum=(latest.close - previous.close) / previous.close,
                current_portfolio_weight=portfolio.weight_for(_instrument_id(instrument)),
                volume=latest.volume,
            ),
            confidence=Decimal("0"),
            scanner_version=SCANNER_VERSION,
            ranking_version=RANKING_VERSION,
            timestamp=latest.timestamp,
        )
        analysis = self._intelligence.analyze_candidate(
            candidate=candidate,
            portfolio=portfolio,
            bars_by_timeframe={timeframe: bars},
            as_of=latest.timestamp,
            required_timeframes=(timeframe,),
            # The global news engine already produced the per-asset context;
            # feed it into the decision model instead of attaching it only
            # after the scanner has classified the candidate.
            news_signal=_news_signal_from_context(
                instrument=instrument,
                context=news_context,
                as_of=latest.timestamp,
            ),
        )
        proposed = _proposed_allocation(portfolio=portfolio, instrument=instrument)
        bucket = _bucket_for(analysis.decision, analysis.opportunity_score.confidence)
        return ActiveScannerCandidate(
            symbol=instrument.symbol,
            full_asset_name=instrument.display_name or instrument.symbol,
            asset_class=instrument.asset_class,
            timestamp=latest.timestamp,
            timeframe=timeframe,
            current_market_state=quote.market_status,
            opportunity_score=analysis.opportunity_score.overall_score,
            confidence=analysis.opportunity_score.confidence,
            regime=tuple(label.value for label in analysis.regime.composite),
            decision=analysis.decision,
            bucket=bucket,
            major_positive_factors=analysis.opportunity_score.components[:5],
            major_negative_factors=analysis.reasons[:5],
            data_quality_state=analysis.features.quality,
            risk_flags=tuple(analysis.ensemble.risk_factors),
            current_position_state=_position_state(instrument, portfolio),
            freshness=freshness_status.value,
            provider_provenance=_provider_provenance(bars),
            affordable_fractionally=proposed <= simulated_capital and latest.close > 0,
            proposed_capital_allocation=proposed,
            remaining_simulated_cash=max(Decimal("0"), simulated_capital - proposed),
            existing_exposure=portfolio.market_value_for(_instrument_id(instrument)),
            diversification_concentration_impact=analysis.portfolio_fit.status.value,
            rejection_reasons=()
            if bucket is not ActiveScannerBucket.REJECTED
            else analysis.reasons,
            **_news_candidate_fields(news_context),
        )


def _deduplicate_crypto_quote_pairs(
    candidates: tuple[ActiveScannerCandidate, ...],
) -> tuple[ActiveScannerCandidate, ...]:
    """Keep the strongest quoted market for each crypto in decision lanes.

    Every market remains in ``candidates`` for auditability; the TOP and
    WATCHLIST lanes avoid presenting BTCUSD, BTCEUR, etc. as separate assets.
    """
    selected: list[ActiveScannerCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.asset_class is not AssetClass.CRYPTO:
            selected.append(candidate)
            continue
        symbol = candidate.symbol.upper().removeprefix("CRYPTO:")
        base_symbol = symbol.split("/", 1)[0].split(":", 1)[0]
        if base_symbol == symbol:
            base_symbol = next(
                (
                    symbol[: -len(quote_asset)]
                    for quote_asset in _CRYPTO_QUOTE_ASSETS
                    if symbol.endswith(quote_asset) and len(symbol) > len(quote_asset)
                ),
                symbol,
            )
        if base_symbol == symbol and "/" in candidate.full_asset_name:
            base_name = candidate.full_asset_name.split("/", 1)[0]
            normalized_name = re.sub(r"[^a-z0-9]+", "", base_name.casefold())
            base_symbol = normalized_name or symbol
        key = (candidate.asset_class.value, base_symbol)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
    return tuple(selected)


def attach_observational_news_context(
    result: ActiveScannerResult,
    news_context_by_symbol: Mapping[str, object],
) -> ActiveScannerResult:
    """Attach news metadata without recalculating market ranking or confidence."""

    def enrich(
        items: tuple[ActiveScannerCandidate, ...],
    ) -> tuple[ActiveScannerCandidate, ...]:
        return tuple(
            item.model_copy(update=_news_candidate_fields(news_context_by_symbol.get(item.symbol)))
            for item in items
        )

    return result.model_copy(
        update={
            "candidates": enrich(result.candidates),
            "top_opportunities": enrich(result.top_opportunities),
            "watchlist": enrich(result.watchlist),
            "no_trade": enrich(result.no_trade),
            "rejected": enrich(result.rejected),
        }
    )


def scan_cached_active_market(
    *,
    cache: HistoricalDataCache,
    instruments: tuple[UniversalInstrument, ...],
    as_of: datetime,
    timeframe: TimeFrame = TimeFrame.ONE_DAY,
    simulated_capital: Decimal = Decimal("200"),
    confidence_profile: str = "V2_B_GUARDED",
    minimum_bars: int = 60,
) -> ActiveScannerResult:
    engine = AegisOpportunityIntelligenceEngine(confidence_profile=confidence_profile)
    scanner = ActiveMarketScanner(intelligence_engine=engine, minimum_bars=minimum_bars)
    bars_by_symbol = {
        instrument.symbol: cache.get_bars(
            provider="alpaca",
            instrument_key=(instrument.broker, instrument.broker_instrument_id),
            timeframe=timeframe,
            as_of=as_of,
            limit=240,
            instrument_factory=instrument.model_dump(mode="json"),
        )
        for instrument in instruments
    }
    return scanner.scan(
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        portfolio=PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=simulated_capital),
        as_of=as_of,
        timeframe=timeframe,
        simulated_capital=simulated_capital,
    )


def active_scanner_inventory(
    *,
    cache: HistoricalDataCache,
    canonical_symbols_by_class: dict[AssetClass, tuple[str, ...]],
) -> dict[str, object]:
    summaries = cache.inventory_summary()
    represented = tuple(sorted({str(row["symbol"]) for row in summaries}))
    provider_timeframes = Counter(
        (str(row["provider"]), str(row["timeframe"])) for row in summaries
    )
    cached_symbols = set(represented)
    requested = {symbol for symbols in canonical_symbols_by_class.values() for symbol in symbols}
    return {
        "cache_path": "work/market-data-cache.sqlite3",
        "symbols_currently_cached": represented,
        "requested_canonical_symbols": tuple(sorted(requested)),
        "missing_from_cache": tuple(sorted(requested - cached_symbols)),
        "asset_classes_represented": tuple(
            asset_class.value
            for asset_class, symbols in canonical_symbols_by_class.items()
            if any(symbol in cached_symbols for symbol in symbols)
        ),
        "historical_providers": tuple(sorted({str(row["provider"]) for row in summaries})),
        "latest_read_only_market_data": (
            "eToro read-only when credentials/network are configured",
            "Alpaca cache snapshots for offline scanner examples",
        ),
        "provider_timeframes": tuple(
            {
                "provider": provider,
                "timeframe": timeframe,
                "series": count,
            }
            for (provider, timeframe), count in sorted(provider_timeframes.items())
        ),
        "cache_inventory": summaries,
    }


def timeframe_readiness_matrix(cache: HistoricalDataCache) -> tuple[dict[str, object], ...]:
    summaries = cache.inventory_summary()
    rows: list[dict[str, object]] = []
    for timeframe in (
        TimeFrame.ONE_DAY,
        TimeFrame.FOUR_HOUR,
        TimeFrame.ONE_HOUR,
        TimeFrame.INTRADAY,
    ):
        entries = tuple(row for row in summaries if row["timeframe"] == timeframe.value)
        providers = tuple(sorted({str(row["provider"]) for row in entries}))
        status = _timeframe_status(timeframe, entries)
        rows.append(
            {
                "timeframe": timeframe.value,
                "status": status.value,
                "cached_series": len(entries),
                "providers": providers,
                "minimum_work": _timeframe_minimum_work(timeframe, status),
            }
        )
    return tuple(rows)


def one_hour_lookback_requirement() -> dict[str, object]:
    return {
        "features_used": (
            "return",
            "rolling_return_5",
            "short_term_momentum_3",
            "medium_term_momentum_10",
            "long_term_momentum_20",
            "sma_20",
            "ema_20",
            "moving_average_slope_25",
            "price_vs_ma_20",
            "rsi_14",
            "macd_35",
            "atr_14",
            "realized_volatility_21",
            "rolling_stddev_20",
            "drawdown_full_visible_window",
            "recent_high_low_20",
            "breakout_21",
            "range_position_20",
            "mean_reversion_z_20",
            "volume_change_6",
            "relative_volume_20",
            "trend_persistence_21",
        ),
        "longest_causal_lookback_bars": 35,
        "warm_up_requirement_bars": 35,
        "minimum_bars_required_for_active_scanner": 60,
        "preferred_safe_buffer_bars": 120,
        "rationale": (
            "MACD requires the longest implemented causal feature window; scanner uses "
            "60 bars and acquisition targets 120 bars to absorb session gaps."
        ),
    }


def provider_one_hour_capability_matrix() -> tuple[dict[str, object], ...]:
    return (
        {
            "provider": "alpaca",
            "EQUITY": "SUPPORTED",
            "ETF": "SUPPORTED",
            "CRYPTO": "SUPPORTED",
            "reason": "Alpaca adapter maps TimeFrame.ONE_HOUR to 1Hour historical bars.",
        },
        {
            "provider": "etoro",
            "EQUITY": "SUPPORTED",
            "ETF": "SUPPORTED",
            "CRYPTO": "SUPPORTED",
            "reason": "eToro historical adapter maps TimeFrame.ONE_HOUR to OneHour candles.",
        },
        {
            "provider": "polygon",
            "EQUITY": "NOT_IMPLEMENTED",
            "ETF": "NOT_IMPLEMENTED",
            "CRYPTO": "NOT_IMPLEMENTED",
            "reason": "repository Polygon/Massive adapter currently exposes only 1D and 4H.",
        },
    )


def classify_intraday_freshness(
    *,
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
    bars: tuple[MarketBar, ...],
    as_of: datetime,
    minimum_bars: int,
) -> IntradayFreshnessStatus:
    if len(bars) < minimum_bars:
        return IntradayFreshnessStatus.INSUFFICIENT_HISTORY
    latest = max(bar.timestamp for bar in bars)
    if latest > as_of:
        return IntradayFreshnessStatus.PROVIDER_UNAVAILABLE
    if timeframe is not TimeFrame.ONE_HOUR:
        age = as_of - latest
        if age.total_seconds() <= 0:
            return IntradayFreshnessStatus.FRESH
        return IntradayFreshnessStatus.FRESH if age.days <= 2 else IntradayFreshnessStatus.STALE
    market_closed = _market_closed_for_instrument(instrument=instrument, as_of=as_of)
    if (
        instrument.asset_class
        in {
            AssetClass.EQUITY,
            AssetClass.ETF,
        }
        and not market_closed
    ):
        expected_latest = _expected_completed_one_hour_bar_timestamp(as_of)
        if latest < expected_latest:
            return IntradayFreshnessStatus.STALE
    if instrument.asset_class is AssetClass.CRYPTO:
        hours = (as_of - latest).total_seconds() / 3600
        if hours <= 2:
            return IntradayFreshnessStatus.FRESH
        if hours <= 6:
            return IntradayFreshnessStatus.DELAYED
        return IntradayFreshnessStatus.STALE
    if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF} and market_closed:
        return IntradayFreshnessStatus.MARKET_CLOSED
    hours = (as_of - latest).total_seconds() / 3600
    if hours <= 2:
        return IntradayFreshnessStatus.FRESH
    if hours <= 6:
        return IntradayFreshnessStatus.DELAYED
    return IntradayFreshnessStatus.STALE


def _market_closed_for_instrument(*, instrument: UniversalInstrument, as_of: datetime) -> bool:
    """Prefer authoritative per-instrument session state over the UTC fallback."""
    authoritative_open = {
        "session-state:OPEN_TRADABLE",
        "session-state:OPEN_NOT_TRADABLE",
    }
    if authoritative_open.intersection(instrument.tags):
        return False
    if "session-state:CLOSED" in instrument.tags:
        return True
    # A missing session observation is no more evidence of closure than an
    # explicit UNKNOWN. The old 13:00-21:00 UTC fallback was US-specific and
    # silently discarded fresh European equities/ETFs every morning. Keep
    # the global weekend safeguard; on weekdays use bar freshness for the
    # scanner and require a broker-verified open/tradable state at preflight.
    return as_of.weekday() >= 5


def _expected_completed_one_hour_bar_timestamp(as_of: datetime) -> datetime:
    return as_of.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def _timeframe_status(
    timeframe: TimeFrame, entries: tuple[dict[str, object], ...]
) -> TimeframeReadinessStatus:
    if timeframe is TimeFrame.ONE_DAY and len(entries) >= 20:
        return TimeframeReadinessStatus.READY
    if timeframe is TimeFrame.FOUR_HOUR and entries:
        return TimeframeReadinessStatus.PARTIAL
    if timeframe in {TimeFrame.ONE_HOUR, TimeFrame.INTRADAY}:
        return TimeframeReadinessStatus.NOT_IMPLEMENTED
    return TimeframeReadinessStatus.BLOCKED


def _timeframe_minimum_work(timeframe: TimeFrame, status: TimeframeReadinessStatus) -> str:
    if status is TimeframeReadinessStatus.READY:
        return "usable from existing cache"
    if timeframe is TimeFrame.FOUR_HOUR:
        return "complete recent continuous Alpaca 4H cache for target universe"
    if timeframe is TimeFrame.ONE_HOUR:
        return "implement and validate 1H provider acquisition/cache coverage"
    return "define lower-timeframe provider support and freshness rules"


def _rejected_candidate(
    *,
    instrument: UniversalInstrument,
    as_of: datetime,
    timeframe: TimeFrame,
    reason: str,
    simulated_capital: Decimal,
    portfolio: PortfolioSnapshot,
    minimum_bars: int,
    provider_provenance: tuple[str, ...],
    news_context: object | None = None,
) -> ActiveScannerCandidate:
    return ActiveScannerCandidate(
        symbol=instrument.symbol,
        full_asset_name=instrument.display_name or instrument.symbol,
        asset_class=instrument.asset_class,
        timestamp=as_of,
        timeframe=timeframe,
        current_market_state=MarketStatus.UNKNOWN,
        opportunity_score=Decimal("0"),
        confidence=Decimal("0"),
        regime=("UNKNOWN",),
        decision=AegisDecision.HOLD,
        bucket=ActiveScannerBucket.REJECTED,
        major_negative_factors=(reason, f"requires at least {minimum_bars} bars"),
        data_quality_state=FeatureQuality.DATA_INSUFFICIENT,
        risk_flags=("fail closed on incomplete data",),
        current_position_state=_position_state(instrument, portfolio),
        freshness="UNKNOWN",
        provider_provenance=provider_provenance,
        affordable_fractionally=False,
        proposed_capital_allocation=Decimal("0"),
        remaining_simulated_cash=simulated_capital,
        existing_exposure=portfolio.market_value_for(_instrument_id(instrument)),
        diversification_concentration_impact="UNKNOWN",
        rejection_reasons=(reason,),
        **_news_candidate_fields(news_context),
    )


def _no_trade_market_closed_candidate(
    *,
    instrument: UniversalInstrument,
    bars: tuple[MarketBar, ...],
    as_of: datetime,
    timeframe: TimeFrame,
    simulated_capital: Decimal,
    portfolio: PortfolioSnapshot,
    provider_provenance: tuple[str, ...],
    news_context: object | None = None,
) -> ActiveScannerCandidate:
    latest = bars[-1] if bars else None
    return ActiveScannerCandidate(
        symbol=instrument.symbol,
        full_asset_name=instrument.display_name or instrument.symbol,
        asset_class=instrument.asset_class,
        timestamp=latest.timestamp if latest is not None else as_of,
        timeframe=timeframe,
        current_market_state=MarketStatus.CLOSED,
        opportunity_score=Decimal("0"),
        confidence=Decimal("0"),
        regime=("UNKNOWN",),
        decision=AegisDecision.HOLD,
        bucket=ActiveScannerBucket.NO_TRADE,
        major_negative_factors=("MARKET_CLOSED",),
        data_quality_state=FeatureQuality.PARTIAL,
        risk_flags=("market closed; no execution decision",),
        current_position_state=_position_state(instrument, portfolio),
        freshness=IntradayFreshnessStatus.MARKET_CLOSED.value,
        provider_provenance=provider_provenance,
        affordable_fractionally=False,
        proposed_capital_allocation=Decimal("0"),
        remaining_simulated_cash=simulated_capital,
        existing_exposure=portfolio.market_value_for(_instrument_id(instrument)),
        diversification_concentration_impact="MARKET_CLOSED",
        rejection_reasons=("MARKET_CLOSED",),
        **_news_candidate_fields(news_context),
    )


def _news_candidate_fields(news_context: object | None) -> dict[str, object]:
    if news_context is None:
        return {}
    if isinstance(news_context, BaseModel):
        payload = news_context.model_dump(mode="json")
    elif isinstance(news_context, dict):
        payload = news_context
    else:
        return {"news_context": {"status": "UNSUPPORTED_NEWS_CONTEXT"}}
    return {
        "news_context": payload,
        "news_sentiment": str(payload.get("aggregate_sentiment", "NEWS_SOURCE_UNAVAILABLE")),
        "news_relevance": Decimal(str(payload.get("aggregate_relevance", "0"))),
        "material_event_count": int(payload.get("material_event_count", 0)),
        "headline_event_summaries": tuple(str(item) for item in payload.get("event_summaries", ())),
        "news_risk_flags": tuple(str(item) for item in payload.get("news_risk_flags", ())),
    }


def _bucket_for(decision: AegisDecision, confidence: Decimal) -> ActiveScannerBucket:
    if decision is AegisDecision.BUY:
        return ActiveScannerBucket.TOP_OPPORTUNITIES
    if decision is AegisDecision.HOLD and confidence >= Decimal("0.45"):
        return ActiveScannerBucket.WATCHLIST
    if decision in {AegisDecision.HOLD, AegisDecision.IGNORE}:
        return ActiveScannerBucket.NO_TRADE
    return ActiveScannerBucket.REJECTED


def _news_signal_from_context(
    *,
    instrument: UniversalInstrument,
    context: object | None,
    as_of: datetime,
) -> NewsSignal | None:
    """Translate global per-asset context into the scanner's typed news signal.

    Only asset-linked news reaches the strategy score, weighted by event
    impact and source confidence. Returning ``None`` is reserved for an absent
    context; a present but empty context remains data-insufficient and cannot
    silently become positive evidence.
    """
    if context is None:
        return None
    if hasattr(context, "model_dump"):
        payload = context.model_dump(mode="python")
    elif isinstance(context, Mapping):
        payload = dict(context)
    else:
        return None

    raw_sentiment = str(payload.get("aggregate_sentiment", "NEUTRAL")).upper()
    sentiment = (
        Decimal("1")
        if raw_sentiment == "POSITIVE"
        else Decimal("-1")
        if raw_sentiment == "NEGATIVE"
        else Decimal("0")
    )
    unique_events = max(0, int(payload.get("unique_event_count", 0) or 0))
    event_risk = Decimal(str(payload.get("event_risk", "0") or "0"))
    source_reliability = Decimal(
        str(payload.get("aggregate_source_reliability", "0") or "0")
    )
    aggregate_impact = Decimal(str(payload.get("aggregate_impact", "0") or "0"))
    freshness = str(payload.get("freshness", "NEWS_SOURCE_UNAVAILABLE"))
    if unique_events <= 0:
        status = (
            NewsSignalStatus.NEWS_NOT_CONFIGURED
            if freshness == "NEWS_SOURCE_UNAVAILABLE"
            else NewsSignalStatus.DATA_INSUFFICIENT
        )
        confidence = Decimal("0")
    else:
        status = NewsSignalStatus.AVAILABLE
        # Ticker relevance alone is not source credibility. A low-quality or
        # single-provider article must not receive the same scoring weight as
        # corroborated, primary reporting.
        confidence = max(
            Decimal("0"),
            min(
                Decimal("1"),
                Decimal(str(payload.get("aggregate_confidence", "0") or "0")),
            ),
        )
    return NewsSignal(
        instrument=instrument,
        timestamp=as_of,
        status=status,
        sentiment=sentiment if unique_events else None,
        impact=max(Decimal("0"), min(Decimal("1"), max(aggregate_impact, event_risk)))
        if unique_events
        else None,
        confidence=confidence,
        source_quality=max(Decimal("0"), min(Decimal("1"), source_reliability)),
        event_risks=(),
    )


def _instrument_id(instrument: UniversalInstrument) -> int:
    if instrument.numeric_instrument_id is not None:
        return instrument.numeric_instrument_id
    digest = hashlib.sha256(instrument.key.encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % 2_000_000_000 + 1


def _market_status(instrument: UniversalInstrument) -> MarketStatus:
    if instrument.asset_class is AssetClass.CRYPTO:
        return MarketStatus.CONTINUOUS_24_7
    return instrument.market_status


def _position_state(instrument: UniversalInstrument, portfolio: PortfolioSnapshot) -> str:
    positions = portfolio.positions_for(_instrument_id(instrument))
    if not positions:
        return "NO_POSITION"
    value = sum((position.market_value for position in positions), Decimal("0"))
    return f"LONG:{value.quantize(Decimal('0.01'))}"


def _provider_provenance(bars: tuple[MarketBar, ...]) -> tuple[str, ...]:
    return tuple(sorted({bar.source for bar in bars})) or ("NO_CACHED_PROVIDER",)


def _primary_provider(bars: tuple[MarketBar, ...]) -> str:
    return _provider_provenance(bars)[0]


def _proposed_allocation(
    *, portfolio: PortfolioSnapshot, instrument: UniversalInstrument
) -> Decimal:
    minimum = instrument.minimum_order_value or Decimal("1")
    policy_amount = (portfolio.total_value * Decimal("0.05")).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    return max(minimum, policy_amount)


def _observation_from_candidate(
    *,
    candidate: ActiveScannerCandidate,
    bars: tuple[MarketBar, ...],
    scan_cycle_timestamp: datetime,
) -> ScannerObservation:
    causal_bars = tuple(bar for bar in bars if bar.timestamp <= scan_cycle_timestamp)
    future_bars = tuple(bar for bar in bars if bar.timestamp > scan_cycle_timestamp)
    latest_bar = max(causal_bars, key=lambda item: item.timestamp, default=None)
    if latest_bar is not None:
        bar_timestamp = latest_bar.timestamp
        current_price = latest_bar.close
    else:
        bar_timestamp = scan_cycle_timestamp
        current_price = Decimal("1")
    if latest_bar is None and future_bars:
        eligibility_reason = ScannerEntryEligibilityReason.FUTURE_BAR
    elif latest_bar is None:
        eligibility_reason = ScannerEntryEligibilityReason.INSUFFICIENT_HISTORY
    elif future_bars:
        eligibility_reason = ScannerEntryEligibilityReason.FUTURE_BAR
    elif candidate.freshness == IntradayFreshnessStatus.MARKET_CLOSED.value:
        eligibility_reason = ScannerEntryEligibilityReason.ELIGIBLE
    elif candidate.freshness == IntradayFreshnessStatus.STALE.value:
        eligibility_reason = ScannerEntryEligibilityReason.STALE
    elif candidate.freshness == IntradayFreshnessStatus.INSUFFICIENT_HISTORY.value:
        eligibility_reason = ScannerEntryEligibilityReason.INSUFFICIENT_HISTORY
    elif candidate.freshness == IntradayFreshnessStatus.PROVIDER_UNAVAILABLE.value:
        eligibility_reason = ScannerEntryEligibilityReason.INVALID_DATA
    elif candidate.current_market_state in {MarketStatus.CLOSED}:
        eligibility_reason = ScannerEntryEligibilityReason.ELIGIBLE
    else:
        eligibility_reason = ScannerEntryEligibilityReason.ELIGIBLE
    if candidate.timeframe is TimeFrame.ONE_HOUR and len(candidate.provider_provenance) == 0:
        eligibility_reason = ScannerEntryEligibilityReason.INVALID_DATA
    if len(candidate.provider_provenance) > 1 and candidate.freshness not in {
        IntradayFreshnessStatus.MARKET_CLOSED.value,
        IntradayFreshnessStatus.FRESH.value,
        IntradayFreshnessStatus.DELAYED.value,
    }:
        eligibility_reason = ScannerEntryEligibilityReason.MIXED_TIMESTAMP_UNSAFE
    eligible = eligibility_reason is ScannerEntryEligibilityReason.ELIGIBLE
    if candidate.freshness == IntradayFreshnessStatus.MARKET_CLOSED.value:
        eligible = True
    return ScannerObservation(
        symbol=candidate.symbol,
        full_name=candidate.full_asset_name,
        asset_class=candidate.asset_class,
        scan_cycle_timestamp=scan_cycle_timestamp,
        bar_timestamp=bar_timestamp,
        timeframe=candidate.timeframe,
        current_price=current_price,
        opportunity_score=candidate.opportunity_score,
        confidence=candidate.confidence,
        regime=candidate.regime,
        action_state=candidate.decision.value,
        data_quality=candidate.data_quality_state.value,
        provider_provenance=candidate.provider_provenance,
        freshness_state=candidate.freshness,
        market_session_state=candidate.current_market_state.value,
        risk_flags=candidate.risk_flags,
        existing_position_state=candidate.current_position_state,
        eligible_for_entry_comparison=eligible,
        eligibility_reason_code=eligibility_reason,
        duplicate_evaluation_key=_observation_key(candidate, scan_cycle_timestamp, bar_timestamp),
        current_market_state=candidate.current_market_state,
    )


def _observation_key(
    candidate: ActiveScannerCandidate, scan_cycle_timestamp: datetime, bar_timestamp: datetime
) -> str:
    return "|".join(
        (
            candidate.symbol,
            candidate.timeframe.value,
            scan_cycle_timestamp.isoformat(),
            bar_timestamp.isoformat(),
        )
    )
