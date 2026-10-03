"""Step 7.8 strategy and opportunity intelligence tests."""

import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import app.agent.service
import app.intelligence.ensemble
import app.intelligence.features
import app.intelligence.portfolio_fit
import app.intelligence.regime
import app.intelligence.scoring
import app.intelligence.service
import app.intelligence.strategies
from app.agent.context import AegisAgentContext
from app.agent.safety import build_sanitized_ai_payload
from app.agent.service import DeterministicAegisAgent
from app.config.models import ApplicationConfig
from app.domain.enums import (
    AssetClass,
    Currency,
    HoldingPeriod,
    MarketStatus,
    RecommendedAction,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.universe import (
    BrokerEligibilitySnapshot,
    CandidateState,
    DataQualityStatus,
    OpportunityCandidate,
    OpportunityFeatures,
    UniversalInstrument,
)
from app.domain.versions import RANKING_VERSION, SCANNER_VERSION
from app.intelligence.correlation import CorrelationEngine
from app.intelligence.ensemble import StrategyEnsemble
from app.intelligence.features import MarketFeatureEngine
from app.intelligence.guardrails import GroundedClaim, GroundedExplanationValidator
from app.intelligence.models import (
    AegisDecision,
    CorrelationQuality,
    CorrelationResult,
    FeatureName,
    FeatureQuality,
    MarketBar,
    MarketBarSeries,
    MultiTimeframeFeatureSet,
    PortfolioFitStatus,
    RegimeLabel,
    ScoreBand,
    StrategyDirection,
    StrategyEvaluationContext,
    StrategySignal,
    TimeFrame,
)
from app.intelligence.news import NewsSignalBuilder
from app.intelligence.portfolio_fit import PortfolioFitEngine
from app.intelligence.profiles import (
    DEFENSIVE_ID,
    MOMENTUM_ID,
    TREND_FOLLOWING_ID,
    default_asset_strategy_profiles,
    guarded_v2b_asset_strategy_profiles,
    legacy_profile_for,
    profile_for,
)
from app.intelligence.providers import InMemoryHistoricalMarketDataProvider
from app.intelligence.regime import MarketRegimeEngine
from app.intelligence.research import StrategyResearchStore
from app.intelligence.runtime import build_strategy_intelligence_report
from app.intelligence.scoring import OpportunityScoringEngine, score_band
from app.intelligence.service import AegisOpportunityIntelligenceEngine
from app.intelligence.strategies import (
    BreakoutStrategy,
    DefensiveStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    TrendFollowingStrategy,
)
from app.main.__main__ import main
from app.storage.sqlite import SecretPersistenceError, SqliteRecordStore


def _now() -> datetime:
    return datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _instrument(
    symbol: str = "TST",
    *,
    instrument_id: int = 7001,
    asset_class: AssetClass = AssetClass.EQUITY,
    status: MarketStatus = MarketStatus.OPEN,
    bid: Decimal | None = Decimal("99.90"),
    ask: Decimal | None = Decimal("100.10"),
    last_price: Decimal = Decimal("100"),
) -> UniversalInstrument:
    now = _now()
    eligibility = BrokerEligibilitySnapshot(
        broker="fake",
        broker_instrument_id=str(instrument_id),
        symbol=symbol,
        checked_at=now,
        currency=Currency.USD,
        verified=True,
        allow_open=True,
        allow_close=True,
        minimum_order_value=Decimal("5"),
        settlement_type=SettlementType.REAL,
        leverage_configs=(1,),
    )
    return UniversalInstrument(
        broker="fake",
        broker_instrument_id=str(instrument_id),
        symbol=symbol,
        display_name=f"{symbol} Test",
        asset_class=asset_class,
        currency=Currency.USD,
        market_status=status,
        tradeable=True,
        buy_allowed=True,
        sell_allowed=True,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("5"),
        bid=bid,
        ask=ask,
        last_price=last_price,
        price_timestamp=now,
        metadata_timestamp=now,
        broker_eligibility=eligibility,
    )


def _bars(
    instrument: UniversalInstrument,
    *,
    count: int = 40,
    timeframe: TimeFrame = TimeFrame.ONE_DAY,
    pattern: str = "up",
    volume: bool = True,
) -> tuple[MarketBar, ...]:
    now = _now()
    bars: list[MarketBar] = []
    for index in range(count):
        if pattern == "down":
            close = Decimal("120") - Decimal(index) * Decimal("0.70")
        elif pattern == "range":
            close = Decimal("100") + Decimal(index % 4 - 2) * Decimal("0.50")
        elif pattern == "volatile":
            close = Decimal("100") + Decimal(((-1) ** index) * (index % 7 + 1))
        else:
            close = Decimal("90") + Decimal(index) * Decimal("0.70")
        open_price = close - Decimal("0.20")
        bar_volume = (Decimal("100000") + Decimal(index) * Decimal("2000")) if volume else None
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=now - timedelta(days=count - index),
                timeframe=timeframe,
                open=open_price,
                high=max(open_price, close) + Decimal("0.30"),
                low=min(open_price, close) - Decimal("0.30"),
                close=close,
                volume=bar_volume,
                currency=Currency.USD,
                source="test-bars",
            )
        )
    return tuple(bars)


def _quote(instrument: UniversalInstrument) -> MarketQuote:
    instrument_id = instrument.numeric_instrument_id
    assert instrument_id is not None
    return MarketQuote(
        instrument_id=instrument_id,
        symbol=instrument.symbol,
        price=instrument.last_price or Decimal("100"),
        previous_close=Decimal("99"),
        bid=instrument.bid,
        ask=instrument.ask,
        as_of=_now(),
        currency=Currency.USD,
        source="test",
        market_status=instrument.market_status,
    )


def _portfolio(
    *,
    cash: Decimal = Decimal("900"),
    position: Position | None = None,
    reported_total: Decimal = Decimal("1000"),
    peak_value: Decimal = Decimal("1000"),
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.USD,
        cash=cash,
        positions=() if position is None else (position,),
        reported_total_value=reported_total,
        peak_value=peak_value,
    )


def _candidate(
    instrument: UniversalInstrument | None = None,
    *,
    state: CandidateState = CandidateState.OPEN_AND_ALLOWED,
    quality: DataQualityStatus = DataQualityStatus.GOOD,
    portfolio_weight: Decimal = Decimal("0"),
) -> OpportunityCandidate:
    effective = instrument or _instrument()
    return OpportunityCandidate(
        candidate_id=f"candidate-{effective.symbol}",
        broker=effective.broker,
        instrument=effective,
        asset_class=effective.asset_class,
        market_status=effective.market_status,
        quote=_quote(effective),
        broker_eligibility=effective.broker_eligibility,
        policy_allowed=state is CandidateState.OPEN_AND_ALLOWED,
        policy_version="asset-policy-v1",
        candidate_state=state,
        data_quality=quality,
        candidate_score=Decimal("70"),
        opportunity_factors=("ranked",),
        risk_factors=("market risk",),
        rejection_reasons=(),
        features=OpportunityFeatures(
            mid_price=effective.last_price,
            spread=Decimal("0.20"),
            spread_percentage=Decimal("0.002"),
            short_term_momentum=Decimal("0.01"),
            current_portfolio_weight=portfolio_weight,
        ),
        confidence=Decimal("0.75"),
        rank=1,
        scanner_version=SCANNER_VERSION,
        ranking_version=RANKING_VERSION,
        timestamp=_now(),
    )


def test_market_bar_validates_ohlc_and_series_consistency() -> None:
    instrument = _instrument()
    with pytest.raises(ValueError):
        MarketBar(
            instrument=instrument,
            timestamp=_now(),
            timeframe=TimeFrame.ONE_DAY,
            open=Decimal("10"),
            high=Decimal("9"),
            low=Decimal("8"),
            close=Decimal("10"),
            currency=Currency.USD,
            source="bad",
        )

    good = _bars(instrument, count=2)
    with pytest.raises(ValueError):
        MarketBarSeries(
            instrument=instrument,
            timeframe=TimeFrame.ONE_HOUR,
            bars=good,
            as_of=_now(),
            quality=FeatureQuality.GOOD,
        )


def test_feature_engine_computes_explainable_indicators_and_missing_timeframes() -> None:
    instrument = _instrument()
    features = MarketFeatureEngine().analyze(
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument)},
        required_timeframes=(TimeFrame.ONE_DAY, TimeFrame.ONE_WEEK),
        as_of=_now(),
    )
    daily = features.for_timeframe(TimeFrame.ONE_DAY)
    assert daily is not None

    assert features.missing_timeframes == (TimeFrame.ONE_WEEK,)
    assert features.quality is FeatureQuality.PARTIAL
    assert daily.get(FeatureName.SMA) is not None
    assert daily.get(FeatureName.EMA) is not None
    assert daily.get(FeatureName.MACD_HISTOGRAM) is not None
    assert daily.get(FeatureName.ATR) is not None
    assert daily.get(FeatureName.RELATIVE_VOLUME) is not None
    assert daily.value(FeatureName.TREND_PERSISTENCE) is not None
    assert daily.normalized(FeatureName.SPREAD) is not None


def test_feature_engine_marks_insufficient_history_and_does_not_fabricate_volume() -> None:
    instrument = _instrument(bid=None, ask=None)
    features = MarketFeatureEngine().analyze(
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument, count=5, volume=False)},
        required_timeframes=(TimeFrame.ONE_DAY,),
        as_of=_now(),
    )
    daily = features.feature_sets[0]
    medium_momentum = daily.get(FeatureName.MEDIUM_TERM_MOMENTUM)
    relative_volume = daily.get(FeatureName.RELATIVE_VOLUME)
    spread = daily.get(FeatureName.SPREAD)
    assert medium_momentum is not None
    assert relative_volume is not None
    assert spread is not None

    assert medium_momentum.quality is FeatureQuality.DATA_INSUFFICIENT
    assert relative_volume.value is None
    assert spread.quality is FeatureQuality.DATA_INSUFFICIENT
    assert daily.quality is FeatureQuality.DATA_INSUFFICIENT
    assert features.quality is FeatureQuality.DATA_INSUFFICIENT


def test_feature_engine_keeps_viable_core_features_partial_when_optional_inputs_lag() -> None:
    instrument = _instrument(bid=None, ask=None)
    features = MarketFeatureEngine().analyze(
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument, count=30, volume=False)},
        required_timeframes=(TimeFrame.ONE_DAY,),
        as_of=_now(),
    )
    daily = features.feature_sets[0]
    macd = daily.get(FeatureName.MACD)
    spread = daily.get(FeatureName.SPREAD)

    assert macd is not None
    assert spread is not None
    assert macd.quality is FeatureQuality.DATA_INSUFFICIENT
    assert spread.quality is FeatureQuality.DATA_INSUFFICIENT
    assert daily.value(FeatureName.TREND_PERSISTENCE) is not None
    assert daily.quality is FeatureQuality.PARTIAL
    assert features.quality is FeatureQuality.PARTIAL


def test_feature_engine_rejects_empty_input_fail_closed() -> None:
    with pytest.raises(ValueError):
        MarketFeatureEngine().analyze(
            bars_by_timeframe={},
            required_timeframes=(TimeFrame.ONE_DAY,),
            as_of=_now(),
        )
    with pytest.raises(ValueError):
        MarketFeatureEngine().analyze(
            bars_by_timeframe={TimeFrame.ONE_DAY: ()},
            required_timeframes=(TimeFrame.ONE_DAY,),
            as_of=_now(),
        )


def test_regime_engine_classifies_uptrend_downtrend_high_vol_and_unknown() -> None:
    up = _features_for("up")
    down = _features_for("down")
    volatile = _features_for("volatile")

    assert MarketRegimeEngine().assess(up, as_of=_now()).trend in {
        RegimeLabel.UPTREND,
        RegimeLabel.STRONG_UPTREND,
    }
    assert MarketRegimeEngine().assess(down, as_of=_now()).trend in {
        RegimeLabel.DOWNTREND,
        RegimeLabel.STRONG_DOWNTREND,
    }
    assert MarketRegimeEngine().assess(volatile, as_of=_now()).volatility in {
        RegimeLabel.HIGH_VOLATILITY,
        RegimeLabel.TRANSITION,
    }

    short = MarketFeatureEngine().analyze(
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(_instrument(), count=3)},
        required_timeframes=(TimeFrame.ONE_DAY,),
        as_of=_now(),
    )
    assert MarketRegimeEngine().assess(short, as_of=_now()).confidence < Decimal("0.50")


def test_baseline_strategies_emit_explainable_signals() -> None:
    context = _strategy_context(pattern="up")

    assert TrendFollowingStrategy().evaluate(context).direction in {
        StrategyDirection.BUY,
        StrategyDirection.STRONG_BUY,
    }
    assert MomentumStrategy().evaluate(context).direction in {
        StrategyDirection.WATCH,
        StrategyDirection.BUY,
    }
    assert BreakoutStrategy().evaluate(context).risk_factors == ()
    assert DefensiveStrategy().evaluate(context).direction is StrategyDirection.WATCH


def test_strategies_hold_or_defend_when_data_or_context_is_weak() -> None:
    range_context = _strategy_context(pattern="range", portfolio_weight=Decimal("0.10"))
    short_context = _strategy_context(pattern="up", count=5)
    volatile_context = _strategy_context(pattern="volatile")

    assert MomentumStrategy().evaluate(short_context).direction is StrategyDirection.HOLD
    assert MeanReversionStrategy().evaluate(range_context).direction in {
        StrategyDirection.HOLD,
        StrategyDirection.REDUCE,
        StrategyDirection.WATCH,
    }
    defensive = DefensiveStrategy().evaluate(volatile_context)
    assert defensive.direction in {StrategyDirection.HOLD, StrategyDirection.AVOID}
    assert defensive.risk_factors


def test_mean_reversion_does_not_blindly_average_down() -> None:
    context = _strategy_context(pattern="down", portfolio_weight=Decimal("0.12"))

    signal = MeanReversionStrategy().evaluate(context)

    assert signal.direction is not StrategyDirection.BUY
    assert any("falling price" in factor for factor in signal.risk_factors)


def test_asset_strategy_profiles_are_versioned_and_asset_specific() -> None:
    profiles = default_asset_strategy_profiles()
    equity = profile_for(AssetClass.EQUITY)
    crypto = profile_for(AssetClass.CRYPTO)
    cfd = profile_for(AssetClass.CFD)

    assert len(profiles) >= len(AssetClass)
    assert equity.profile_version
    assert crypto.minimum_confidence_for_buy == Decimal("0.45")
    assert crypto.weight_for(DEFENSIVE_ID) < equity.weight_for(DEFENSIVE_ID)
    assert not cfd.enabled


class StaticSignalProvider:
    def __init__(self, signal: StrategySignal) -> None:
        self._signal = signal

    @property
    def strategy_id(self) -> str:
        return self._signal.strategy_id

    @property
    def strategy_version(self) -> str:
        return self._signal.strategy_version

    def evaluate(self, context: StrategyEvaluationContext) -> StrategySignal:
        return self._signal


def test_strategy_ensemble_weights_agreement_and_conflict() -> None:
    context = _strategy_context(pattern="up")
    instrument = context.candidate.instrument
    buy = _signal(TREND_FOLLOWING_ID, StrategyDirection.BUY, instrument)
    avoid = _signal(DEFENSIVE_ID, StrategyDirection.AVOID, instrument)
    result = StrategyEnsemble((StaticSignalProvider(buy), StaticSignalProvider(avoid))).evaluate(
        context, as_of=_now()
    )

    assert result.conflict > Decimal("0")
    assert result.confidence < Decimal("0.50")
    assert "strategy disagreement is material" in result.risk_factors


def test_strategy_ensemble_confidence_responds_to_agreement_and_signal_confidence() -> None:
    context = _strategy_context(pattern="up")
    instrument = context.candidate.instrument
    high_buy = _signal(TREND_FOLLOWING_ID, StrategyDirection.BUY, instrument).model_copy(
        update={"confidence": Decimal("0.90")}
    )
    high_momentum = _signal(MOMENTUM_ID, StrategyDirection.BUY, instrument).model_copy(
        update={"confidence": Decimal("0.90")}
    )
    low_buy = _signal(TREND_FOLLOWING_ID, StrategyDirection.BUY, instrument).model_copy(
        update={"confidence": Decimal("0.35")}
    )
    avoid = _signal(DEFENSIVE_ID, StrategyDirection.AVOID, instrument)

    aligned = StrategyEnsemble(
        (StaticSignalProvider(high_buy), StaticSignalProvider(high_momentum))
    ).evaluate(context, as_of=_now())
    low_confidence = StrategyEnsemble(
        (StaticSignalProvider(low_buy), StaticSignalProvider(high_momentum))
    ).evaluate(context, as_of=_now())
    conflicted = StrategyEnsemble(
        (StaticSignalProvider(high_buy), StaticSignalProvider(avoid))
    ).evaluate(context, as_of=_now())

    assert aligned.confidence > low_confidence.confidence
    assert aligned.confidence > conflicted.confidence
    assert aligned.agreement > conflicted.agreement


def test_strategy_ensemble_requires_a_provider() -> None:
    with pytest.raises(ValueError):
        StrategyEnsemble(())


def test_opportunity_score_bands_and_confidence_are_separate() -> None:
    context = _strategy_context(pattern="up")
    ensemble = StrategyEnsemble(
        (
            TrendFollowingStrategy(),
            MomentumStrategy(),
            BreakoutStrategy(),
            DefensiveStrategy(),
        )
    ).evaluate(context, as_of=_now())
    assert context.portfolio_fit is not None
    score = OpportunityScoringEngine().score(
        features=context.features,
        regime=context.regime,
        ensemble=ensemble,
        portfolio_fit=context.portfolio_fit,
        as_of=_now(),
    )

    assert Decimal("0") <= score.overall_score <= Decimal("100")
    assert score.confidence <= Decimal("1")
    assert score.band is score_band(score.overall_score)
    assert score_band(Decimal("95")) is ScoreBand.EXCEPTIONAL
    assert score_band(Decimal("85")) is ScoreBand.VERY_STRONG
    assert score_band(Decimal("75")) is ScoreBand.STRONG
    assert score_band(Decimal("65")) is ScoreBand.INTERESTING
    assert score_band(Decimal("55")) is ScoreBand.NEUTRAL
    assert score_band(Decimal("45")) is ScoreBand.WEAK
    assert score_band(Decimal("20")) is ScoreBand.AVOID


def test_opportunity_confidence_responds_to_feature_quality_and_regime_confidence() -> None:
    good_context = _strategy_context(pattern="up", count=40, volume=True)
    partial_context = _strategy_context(pattern="up", count=30, volume=False)
    assert good_context.portfolio_fit is not None
    assert partial_context.portfolio_fit is not None

    good_ensemble = StrategyEnsemble((TrendFollowingStrategy(), MomentumStrategy())).evaluate(
        good_context, as_of=_now()
    )
    partial_ensemble = StrategyEnsemble((TrendFollowingStrategy(), MomentumStrategy())).evaluate(
        partial_context, as_of=_now()
    )
    good_score = OpportunityScoringEngine().score(
        features=good_context.features,
        regime=good_context.regime,
        ensemble=good_ensemble,
        portfolio_fit=good_context.portfolio_fit,
        as_of=_now(),
    )
    partial_score = OpportunityScoringEngine().score(
        features=partial_context.features,
        regime=partial_context.regime,
        ensemble=partial_ensemble,
        portfolio_fit=partial_context.portfolio_fit,
        as_of=_now(),
    )

    assert good_context.features.quality is FeatureQuality.GOOD
    assert partial_context.features.quality is FeatureQuality.PARTIAL
    assert good_score.confidence > partial_score.confidence
    assert (
        good_score.confidence
        == OpportunityScoringEngine()
        .score(
            features=good_context.features,
            regime=good_context.regime,
            ensemble=good_ensemble,
            portfolio_fit=good_context.portfolio_fit,
            as_of=_now(),
        )
        .confidence
    )


def test_portfolio_fit_scores_cash_reserve_concentration_and_diversification() -> None:
    instrument = _instrument()
    position = Position(
        position_id="p1",
        instrument_id=instrument.numeric_instrument_id or 0,
        symbol=instrument.symbol,
        settlement_type=SettlementType.REAL,
        units=Decimal("3"),
        average_entry_price=Decimal("100"),
        market_price=Decimal("100"),
    )
    fit = PortfolioFitEngine().evaluate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(cash=Decimal("700"), position=position),
        proposed_exposure=Decimal("50"),
        position_asset_classes={position.instrument_id: AssetClass.EQUITY},
    )
    blocked = PortfolioFitEngine().evaluate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(cash=Decimal("10")),
        proposed_exposure=Decimal("200"),
    )

    assert fit.status in {PortfolioFitStatus.NEUTRAL, PortfolioFitStatus.NEGATIVE}
    assert fit.projected_concentration > Decimal("0.25")
    assert blocked.status is PortfolioFitStatus.BLOCKED
    assert "available cash is insufficient" in blocked.reasons

    at_reserve = PortfolioFitEngine().evaluate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(cash=Decimal("1000")),
        proposed_exposure=Decimal("930"),
    )
    below_reserve = PortfolioFitEngine().evaluate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(cash=Decimal("1000")),
        proposed_exposure=Decimal("930.01"),
    )
    assert at_reserve.projected_cash_reserve == Decimal("0.0700")
    assert "projected cash reserve would be below policy baseline" not in at_reserve.reasons
    assert "projected cash reserve would be below policy baseline" in below_reserve.reasons


def test_correlation_engine_reports_quality_without_false_precision() -> None:
    instrument = _instrument("AAA", instrument_id=1)
    related = _instrument("BBB", instrument_id=2)
    engine = CorrelationEngine(minimum_samples=10)

    insufficient = engine.rolling_correlation(
        _bars(instrument, count=5),
        _bars(related, count=5),
        time_window=20,
    )
    correlated = engine.rolling_correlation(
        _bars(instrument, count=30, pattern="up"),
        _bars(related, count=30, pattern="up"),
        time_window=20,
    )

    assert insufficient.quality is CorrelationQuality.DATA_INSUFFICIENT
    assert insufficient.correlation is None
    assert correlated.quality is CorrelationQuality.GOOD
    assert correlated.sample_size >= 10
    assert correlated.correlation is not None


def test_historical_provider_filters_by_timeframe_time_and_limit() -> None:
    instrument = _instrument()
    bars = _bars(instrument, count=6)
    provider = InMemoryHistoricalMarketDataProvider({(instrument.key, TimeFrame.ONE_DAY): bars})

    result = provider.get_bars(
        instrument,
        TimeFrame.ONE_DAY,
        as_of=bars[-2].timestamp,
        limit=3,
    )
    missing = provider.get_bars(instrument, TimeFrame.ONE_WEEK, as_of=_now(), limit=3)

    assert result == bars[-4:-1]
    assert missing == ()


def test_correlation_engine_handles_empty_zero_variance_and_bad_configuration() -> None:
    instrument = _instrument("AAA", instrument_id=1)
    flat = tuple(
        bar.model_copy(update={"close": Decimal("100"), "open": Decimal("100")})
        for bar in _bars(instrument, count=30)
    )

    with pytest.raises(ValueError):
        CorrelationEngine(minimum_samples=1)
    empty = CorrelationEngine().rolling_correlation((), flat, time_window=20)
    zero_variance = CorrelationEngine().rolling_correlation(flat, flat, time_window=20)

    assert empty.quality is CorrelationQuality.DATA_INSUFFICIENT
    assert zero_variance.correlation == Decimal("0")


def test_portfolio_fit_handles_invalid_portfolio_and_correlation_penalties() -> None:
    candidate = _candidate(_instrument())
    engine = PortfolioFitEngine()
    invalid = engine.evaluate(
        candidate=candidate,
        portfolio=_portfolio(cash=Decimal("0"), reported_total=Decimal("0")),
        proposed_exposure=Decimal("1"),
    )
    position = Position(
        position_id="p-other",
        instrument_id=99,
        symbol="OTHER",
        settlement_type=SettlementType.REAL,
        units=Decimal("1"),
        average_entry_price=Decimal("100"),
        market_price=Decimal("100"),
    )
    high_corr = engine.evaluate(
        candidate=candidate,
        portfolio=_portfolio(position=position),
        proposed_exposure=Decimal("10"),
        correlations=(
            CorrelationResult(
                instrument_key=candidate.instrument.key,
                related_instrument_key="fake:other",
                correlation=Decimal("0.90"),
                sample_size=20,
                time_window=20,
                quality=CorrelationQuality.GOOD,
            ),
        ),
    )
    low_corr = engine.evaluate(
        candidate=candidate,
        portfolio=_portfolio(position=position),
        proposed_exposure=Decimal("10"),
        correlations=(
            CorrelationResult(
                instrument_key=candidate.instrument.key,
                related_instrument_key="fake:other",
                correlation=Decimal("0.10"),
                sample_size=20,
                time_window=20,
                quality=CorrelationQuality.GOOD,
            ),
        ),
    )

    assert invalid.status is PortfolioFitStatus.BLOCKED
    assert high_corr.diversification_score < low_corr.diversification_score


def test_news_signal_is_optional_and_never_fabricated() -> None:
    instrument = _instrument()
    builder = NewsSignalBuilder()
    missing = builder.not_configured(instrument, as_of=_now())
    unrelated = builder.from_items(instrument, items=(), as_of=_now())
    relevant = builder.from_items(
        instrument,
        items=(
            NewsItem(
                news_id="n1",
                source="fixture",
                timestamp=_now(),
                headline="Fixture headline",
                summary="Fixture summary",
                asset_relevance=(instrument.symbol,),
                sentiment=Decimal("0.50"),
                importance=Decimal("0.70"),
                confidence=Decimal("0.80"),
            ),
        ),
        as_of=_now(),
    )

    assert missing.status.value == "NEWS_NOT_CONFIGURED"
    assert unrelated.status.value == "DATA_INSUFFICIENT"
    assert relevant.sentiment == Decimal("0.50")
    assert relevant.event_risks == ()


def test_news_signal_aggregates_multiple_relevant_sources() -> None:
    instrument = _instrument()
    signal = NewsSignalBuilder().from_items(
        instrument,
        items=(
            NewsItem(
                news_id="n1",
                source="fixture-a",
                timestamp=_now(),
                headline="Fixture headline one",
                summary="Fixture summary one",
                asset_relevance=(instrument.symbol,),
                sentiment=Decimal("0.50"),
                importance=Decimal("0.60"),
                confidence=Decimal("0.80"),
            ),
            NewsItem(
                news_id="n2",
                source="fixture-b",
                timestamp=_now(),
                headline="Fixture headline two",
                summary="Fixture summary two",
                asset_relevance=(instrument.symbol,),
                sentiment=Decimal("-0.10"),
                importance=Decimal("0.90"),
                confidence=Decimal("0.60"),
            ),
        ),
        as_of=_now(),
    )

    assert signal.sentiment == Decimal("0.20")
    assert signal.impact == Decimal("0.90")
    assert signal.source_quality > Decimal("0")


def test_intelligence_engine_returns_buy_hold_and_ignore_without_execution() -> None:
    engine = AegisOpportunityIntelligenceEngine()
    buy_candidate = _candidate(_instrument())
    buy = engine.analyze_candidate(
        candidate=buy_candidate,
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(buy_candidate.instrument)},
        as_of=_now(),
    )
    weak_candidate = _candidate(_instrument("WEAK", instrument_id=7002, bid=None, ask=None))
    hold = engine.analyze_candidate(
        candidate=weak_candidate,
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(weak_candidate.instrument, count=5)},
        as_of=_now(),
    )
    disabled_candidate = _candidate(
        _instrument("CFD", instrument_id=7003, asset_class=AssetClass.CFD)
    )
    ignore = engine.analyze_candidate(
        candidate=disabled_candidate,
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(disabled_candidate.instrument)},
        as_of=_now(),
    )

    assert buy.decision in {AegisDecision.BUY, AegisDecision.HOLD}
    assert buy.data_digest
    assert hold.decision is AegisDecision.HOLD
    assert ignore.decision is AegisDecision.IGNORE


def test_confidence_profile_activation_is_explicit_and_reversible() -> None:
    instrument = _instrument()
    default_analysis = AegisOpportunityIntelligenceEngine().analyze_candidate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument)},
        as_of=_now(),
    )
    legacy_analysis = AegisOpportunityIntelligenceEngine(
        confidence_profile="V1_LEGACY"
    ).analyze_candidate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument)},
        as_of=_now(),
    )
    guarded_analysis = AegisOpportunityIntelligenceEngine(
        confidence_profile="V2_B_GUARDED"
    ).analyze_candidate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument)},
        as_of=_now(),
    )

    assert default_analysis.opportunity_score.confidence_model_version == "V1_LEGACY"
    assert legacy_analysis.opportunity_score.confidence_model_version == "V1_LEGACY"
    assert guarded_analysis.opportunity_score.confidence_model_version == "V2_B_GUARDED_V1"
    assert guarded_analysis.opportunity_score.confidence_semantics_version == (
        "SIGNAL_RELIABILITY_V2"
    )
    assert guarded_analysis.opportunity_score.confidence_threshold_provenance == (
        "EMPIRICALLY_CALIBRATED_GUARDED"
    )
    assert guarded_analysis.opportunity_score.calibration_dataset_id == "cached-etoro-step80a-1d"
    assert guarded_analysis.opportunity_score.execution_readiness_score is not None
    assert legacy_profile_for(AssetClass.EQUITY).minimum_confidence_for_buy == Decimal("0.65")
    assert next(
        profile
        for profile in guarded_v2b_asset_strategy_profiles()
        if profile.asset_class is AssetClass.EQUITY
    ).minimum_confidence_for_buy == Decimal("0.5475")


def test_recommended_action_mapping_keeps_hold_first_class() -> None:
    analysis = AegisOpportunityIntelligenceEngine().analyze_candidate(
        candidate=_candidate(_instrument()),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(_instrument(), count=5)},
        as_of=_now(),
    )

    assert (
        app.intelligence.service.recommended_action_for_analysis(analysis) is RecommendedAction.HOLD
    )
    assert (
        app.intelligence.service.recommended_action_for_analysis(
            analysis.model_copy(update={"decision": AegisDecision.REDUCE})
        )
        is RecommendedAction.REDUCE
    )
    assert (
        app.intelligence.service.recommended_action_for_analysis(
            analysis.model_copy(update={"decision": AegisDecision.BUY})
        )
        is RecommendedAction.OPEN
    )


def test_agent_consumes_intelligence_but_still_outputs_only_untrusted_proposal() -> None:
    instrument = _instrument()
    analysis = AegisOpportunityIntelligenceEngine().analyze_candidate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument)},
        as_of=_now(),
    )
    if analysis.decision is not AegisDecision.BUY:
        analysis = analysis.model_copy(update={"decision": AegisDecision.BUY})
    agent_context = AegisAgentContext(
        portfolio=_portfolio(),
        quotes=(_quote(instrument),),
        news=(),
        instruments=(_instrument_metadata(instrument),),
        candidates=(),
        intelligence_reports=(analysis,),
        analysis_timestamp=_now(),
        strategy=ApplicationConfig().strategy,
        minimum_trade_amount=Decimal("5"),
    )

    result = DeterministicAegisAgent().analyze(agent_context)
    payload = build_sanitized_ai_payload(agent_context)

    assert result.proposal is not None
    assert result.proposal.side is TradeSide.BUY
    assert result.proposal.confidence_model_version == (
        analysis.opportunity_score.confidence_model_version
    )
    assert result.proposal.confidence_semantics_version == (
        analysis.opportunity_score.confidence_semantics_version
    )
    assert result.analysis.recommended_action is RecommendedAction.OPEN
    assert payload["opportunity_intelligence"][0]["data_digest"] == analysis.data_digest
    assert "api-secret" not in json.dumps(payload)


def test_v2b_below_threshold_does_not_create_upstream_proposal() -> None:
    instrument = _instrument()
    analysis = AegisOpportunityIntelligenceEngine(
        confidence_profile="V2_B_GUARDED"
    ).analyze_candidate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument, count=5)},
        as_of=_now(),
    )
    context = AegisAgentContext(
        portfolio=_portfolio(),
        quotes=(_quote(instrument),),
        news=(),
        instruments=(_instrument_metadata(instrument),),
        candidates=(),
        intelligence_reports=(analysis,),
        analysis_timestamp=_now(),
        strategy=ApplicationConfig().strategy,
        minimum_trade_amount=Decimal("5"),
    )

    result = DeterministicAegisAgent().analyze(context)

    assert analysis.opportunity_score.confidence < Decimal("0.5475")
    assert analysis.decision is not AegisDecision.BUY
    assert result.proposal is None


def test_agent_keeps_hold_when_intelligence_is_not_buy_or_quote_missing() -> None:
    instrument = _instrument()
    analysis = AegisOpportunityIntelligenceEngine().analyze_candidate(
        candidate=_candidate(instrument),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument, count=5)},
        as_of=_now(),
    )
    context = AegisAgentContext(
        portfolio=_portfolio(),
        quotes=(),
        news=(),
        instruments=(),
        candidates=(),
        intelligence_reports=(analysis,),
        analysis_timestamp=_now(),
        strategy=ApplicationConfig().strategy,
    )

    result = DeterministicAegisAgent().analyze(context)

    assert result.proposal is None
    assert result.analysis.recommended_action is RecommendedAction.HOLD


def test_agent_rejects_buy_intelligence_when_quote_or_metadata_is_missing() -> None:
    instrument = _instrument()
    analysis = (
        AegisOpportunityIntelligenceEngine()
        .analyze_candidate(
            candidate=_candidate(instrument),
            portfolio=_portfolio(),
            bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument)},
            as_of=_now(),
        )
        .model_copy(update={"decision": AegisDecision.BUY})
    )
    no_quote_candidate = analysis.candidate.model_copy(update={"quote": None})
    no_quote_analysis = analysis.model_copy(update={"candidate": no_quote_candidate})
    context = AegisAgentContext(
        portfolio=_portfolio(),
        quotes=(),
        news=(),
        instruments=(),
        candidates=(),
        intelligence_reports=(no_quote_analysis,),
        analysis_timestamp=_now(),
        strategy=ApplicationConfig().strategy,
    )

    result = DeterministicAegisAgent().analyze(context)

    assert result.proposal is None
    assert "lacks a reference quote" in result.analysis.rationale


def test_additional_strategy_branches_are_conservative() -> None:
    trend_hold = TrendFollowingStrategy().evaluate(_strategy_context(pattern="down"))
    momentum_avoid = MomentumStrategy().evaluate(_strategy_context(pattern="down"))
    no_volume_breakout = BreakoutStrategy().evaluate(_strategy_context(pattern="up", volume=False))
    insufficient_mean_reversion = MeanReversionStrategy().evaluate(
        _strategy_context(pattern="range", count=5)
    )

    assert trend_hold.direction in {StrategyDirection.HOLD, StrategyDirection.WATCH}
    assert momentum_avoid.direction in {StrategyDirection.AVOID, StrategyDirection.HOLD}
    assert "volume confirmation is unavailable" in no_volume_breakout.risk_factors
    assert insufficient_mean_reversion.direction is StrategyDirection.HOLD


def test_scoring_penalizes_risk_off_blocked_portfolio_and_defensive_signals() -> None:
    context = _strategy_context(pattern="volatile")
    blocked_fit = PortfolioFitEngine().evaluate(
        candidate=context.candidate,
        portfolio=_portfolio(cash=Decimal("1")),
        proposed_exposure=Decimal("500"),
    )
    defensive = _signal(DEFENSIVE_ID, StrategyDirection.AVOID, context.candidate.instrument)
    ensemble = StrategyEnsemble((StaticSignalProvider(defensive),)).evaluate(context, as_of=_now())
    score = OpportunityScoringEngine().score(
        features=context.features,
        regime=context.regime,
        ensemble=ensemble,
        portfolio_fit=blocked_fit,
        as_of=_now(),
    )

    assert score.risk_penalty > Decimal("0")
    assert score.risk_adjusted_score < Decimal("50")
    assert score.overall_score < Decimal("60")


def test_grounded_explanation_guard_rejects_fabricated_claim_references() -> None:
    validator = GroundedExplanationValidator()
    accepted = validator.validate(
        claims=(GroundedClaim(text="Momentum was positive", source_fact_id="feature:momentum"),),
        allowed_fact_ids=frozenset({"feature:momentum"}),
    )
    rejected = validator.validate(
        claims=(
            GroundedClaim(
                text="Earnings surprise was positive",
                source_fact_id="news:earnings",
            ),
        ),
        allowed_fact_ids=frozenset({"feature:momentum"}),
    )

    assert accepted.accepted
    assert not rejected.accepted
    assert rejected.rejected_claims


def test_research_store_persists_restart_safe_and_rejects_secret_shaped_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.sqlite3"
    analysis = AegisOpportunityIntelligenceEngine().analyze_candidate(
        candidate=_candidate(_instrument()),
        portfolio=_portfolio(),
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(_instrument())},
        as_of=_now(),
    )
    store = StrategyResearchStore(SqliteRecordStore(path))
    record_id = store.record_analysis(analysis)

    restarted = StrategyResearchStore(SqliteRecordStore(path))
    records = restarted.list_records()

    assert record_id == 1
    assert records[0]["instrument"] == analysis.candidate.instrument.symbol
    assert "feature_digest" in records[0]
    serialized = json.dumps(records, sort_keys=True)
    assert "x-api-key" not in serialized
    with pytest.raises(SecretPersistenceError):
        restarted.append_sanitized_record({"api_key": "never-persist"})


def test_runtime_and_cli_are_offline_read_only_and_persistent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StrategyResearchStore(SqliteRecordStore(tmp_path / "strategy.sqlite3"))
    payload = build_strategy_intelligence_report(ApplicationConfig(), store=store, clock=_now)
    monkeypatch.chdir(tmp_path)

    assert payload["status"] == "OFFLINE_VERIFIED"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert store.list_records()

    assert main(("strategy-intelligence",), values={}) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["status"] == "OFFLINE_VERIFIED"
    assert cli_payload["broker_write_calls"] == 0


def test_runtime_blocks_if_demo_execution_is_enabled() -> None:
    payload = build_strategy_intelligence_report(
        ApplicationConfig(
            operating_mode="ETORO_DEMO",
            etoro_api_enabled=True,
            etoro_demo_execution_enabled=True,
        ),
        clock=_now,
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "DEMO_EXECUTION_ENABLED"
    assert payload["broker_write_calls"] == 0


def test_intelligence_modules_do_not_depend_on_etoro_or_execution_capabilities() -> None:
    for module in (
        app.intelligence.features,
        app.intelligence.regime,
        app.intelligence.strategies,
        app.intelligence.ensemble,
        app.intelligence.scoring,
        app.intelligence.portfolio_fit,
        app.intelligence.service,
    ):
        source = inspect.getsource(module)
        assert "app.brokers.etoro" not in source
        assert "submit_demo" not in source
        assert "post_once" not in source
        assert "market-open-orders" not in source
        assert "ETORO_API_KEY" not in source
        assert "ETORO_USER_KEY" not in source


def _features_for(pattern: str, *, count: int = 40) -> MultiTimeframeFeatureSet:
    instrument = _instrument()
    return MarketFeatureEngine().analyze(
        bars_by_timeframe={TimeFrame.ONE_DAY: _bars(instrument, count=count, pattern=pattern)},
        required_timeframes=(TimeFrame.ONE_DAY,),
        as_of=_now(),
    )


def _strategy_context(
    *,
    pattern: str = "up",
    count: int = 40,
    portfolio_weight: Decimal = Decimal("0"),
    volume: bool = True,
) -> StrategyEvaluationContext:
    instrument = _instrument()
    candidate = _candidate(instrument, portfolio_weight=portfolio_weight)
    features = MarketFeatureEngine().analyze(
        bars_by_timeframe={
            TimeFrame.ONE_DAY: _bars(
                instrument,
                count=count,
                pattern=pattern,
                volume=volume,
            )
        },
        required_timeframes=(TimeFrame.ONE_DAY,),
        as_of=_now(),
    )
    regime = MarketRegimeEngine().assess(features, as_of=_now())
    portfolio_fit = PortfolioFitEngine().evaluate(
        candidate=candidate,
        portfolio=_portfolio(),
        proposed_exposure=Decimal("50"),
    )
    return StrategyEvaluationContext(
        candidate=candidate,
        features=features,
        regime=regime,
        portfolio_fit=portfolio_fit,
        profile=profile_for(AssetClass.EQUITY),
    )


def _signal(
    strategy_id: str, direction: StrategyDirection, instrument: UniversalInstrument
) -> StrategySignal:
    return StrategySignal(
        strategy_id=strategy_id,
        strategy_version="test-v1",
        instrument=instrument,
        direction=direction,
        strength=Decimal("0.80"),
        confidence=Decimal("0.80"),
        time_horizon=TimeFrame.ONE_DAY,
        supporting_factors=("test factor",),
        risk_factors=(),
        invalidation_conditions=("test invalidation",),
        data_quality=FeatureQuality.GOOD,
    )


def _instrument_metadata(instrument: UniversalInstrument) -> InstrumentMetadata:
    instrument_id = instrument.numeric_instrument_id
    assert instrument_id is not None
    return InstrumentMetadata(
        instrument_id=instrument_id,
        symbol=instrument.symbol,
        asset_class=instrument.asset_class,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("5"),
        metadata_as_of=_now(),
        source="test",
    )


def _proposal() -> TradeProposal:
    return TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000778"),
        idempotency_key="step78-proposal-key",
        created_at=_now(),
        instrument_id=7001,
        symbol="TST",
        asset_class=AssetClass.EQUITY,
        side=TradeSide.BUY,
        intent=TradeIntent.OPEN,
        amount=Decimal("5"),
        currency=Currency.USD,
        target_weight=Decimal("0.10"),
        current_weight=Decimal("0"),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="test",
        evidence=(),
        confidence=Decimal("0.80"),
        risk_factors=("market risk",),
        invalidation_conditions=("test invalidation",),
        expected_holding_period=HoldingPeriod.MONTHS,
    )
