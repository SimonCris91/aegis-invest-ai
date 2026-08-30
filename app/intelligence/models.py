"""Typed broker-neutral models for Aegis opportunity intelligence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass, Currency
from app.domain.universe import DataQualityStatus, OpportunityCandidate, UniversalInstrument


class TimeFrame(StrEnum):
    INTRADAY = "INTRADAY"
    ONE_HOUR = "1H"
    FOUR_HOUR = "4H"
    ONE_DAY = "1D"
    ONE_WEEK = "1W"


class FeatureQuality(StrEnum):
    GOOD = "GOOD"
    PARTIAL = "PARTIAL"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"


class FeatureName(StrEnum):
    RETURN = "return"
    ROLLING_RETURN = "rolling_return"
    SHORT_TERM_MOMENTUM = "short_term_momentum"
    MEDIUM_TERM_MOMENTUM = "medium_term_momentum"
    LONG_TERM_MOMENTUM = "long_term_momentum"
    SMA = "sma"
    EMA = "ema"
    MOVING_AVERAGE_SLOPE = "moving_average_slope"
    PRICE_VS_MOVING_AVERAGE = "price_vs_moving_average"
    RSI = "rsi"
    MACD = "macd"
    MACD_SIGNAL = "macd_signal"
    MACD_HISTOGRAM = "macd_histogram"
    ATR = "atr"
    REALIZED_VOLATILITY = "realized_volatility"
    ROLLING_STANDARD_DEVIATION = "rolling_standard_deviation"
    DRAWDOWN = "drawdown"
    DISTANCE_FROM_RECENT_HIGH = "distance_from_recent_high"
    DISTANCE_FROM_RECENT_LOW = "distance_from_recent_low"
    BREAKOUT_STRENGTH = "breakout_strength"
    RANGE_POSITION = "range_position"
    MEAN_REVERSION_Z_SCORE = "mean_reversion_z_score"
    VOLUME_CHANGE = "volume_change"
    RELATIVE_VOLUME = "relative_volume"
    SPREAD = "spread"
    LIQUIDITY_PROXY = "liquidity_proxy"
    TREND_PERSISTENCE = "trend_persistence"


class RegimeLabel(StrEnum):
    STRONG_UPTREND = "STRONG_UPTREND"
    UPTREND = "UPTREND"
    RANGE = "RANGE"
    DOWNTREND = "DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    TRANSITION = "TRANSITION"
    UNKNOWN = "UNKNOWN"


class StrategyDirection(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    WATCH = "WATCH"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    AVOID = "AVOID"


class AegisDecision(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    IGNORE = "IGNORE"


class ScoreBand(StrEnum):
    EXCEPTIONAL = "EXCEPTIONAL"
    VERY_STRONG = "VERY_STRONG"
    STRONG = "STRONG"
    INTERESTING = "INTERESTING"
    NEUTRAL = "NEUTRAL"
    WEAK = "WEAK"
    AVOID = "AVOID"


class PortfolioFitStatus(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    BLOCKED = "BLOCKED"


class CorrelationQuality(StrEnum):
    GOOD = "GOOD"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    UNKNOWN = "UNKNOWN"


class NewsSignalStatus(StrEnum):
    NEWS_NOT_CONFIGURED = "NEWS_NOT_CONFIGURED"
    AVAILABLE = "AVAILABLE"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


class EventRiskType(StrEnum):
    EARNINGS = "EARNINGS"
    MACRO_EVENT = "MACRO_EVENT"
    MAJOR_CORPORATE_EVENT = "MAJOR_CORPORATE_EVENT"
    REGULATORY_EVENT = "REGULATORY_EVENT"
    TOKEN_SPECIFIC_EVENT = "TOKEN_SPECIFIC_EVENT"
    UNKNOWN_MAJOR_EVENT = "UNKNOWN_MAJOR_EVENT"


class MarketBar(FrozenDomainModel):
    instrument: UniversalInstrument
    timestamp: datetime
    timeframe: TimeFrame
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal | None = Field(default=None, ge=0)
    currency: Currency
    source: str = Field(min_length=1)
    data_quality: FeatureQuality = FeatureQuality.GOOD

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")

    @model_validator(mode="after")
    def validate_ohlc_shape(self) -> MarketBar:
        if self.low > self.high:
            raise ValueError("bar low cannot exceed high")
        if self.high < max(self.open, self.close):
            raise ValueError("bar high must cover open and close")
        if self.low > min(self.open, self.close):
            raise ValueError("bar low must cover open and close")
        return self


class MarketBarSeries(FrozenDomainModel):
    instrument: UniversalInstrument
    timeframe: TimeFrame
    bars: tuple[MarketBar, ...]
    as_of: datetime
    quality: FeatureQuality

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @model_validator(mode="after")
    def bars_match_series(self) -> MarketBarSeries:
        for bar in self.bars:
            if bar.timeframe is not self.timeframe:
                raise ValueError("all bars must match the series timeframe")
            if bar.instrument.key != self.instrument.key:
                raise ValueError("all bars must match the series instrument")
        return self


class FeatureValue(FrozenDomainModel):
    name: FeatureName
    value: Decimal | None = None
    normalized_value: Decimal | None = Field(default=None, ge=Decimal("-1"), le=Decimal("1"))
    timestamp: datetime | None = None
    lookback: int | None = Field(default=None, gt=0)
    quality: FeatureQuality
    source: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "feature timestamp")


class FeatureSet(FrozenDomainModel):
    instrument: UniversalInstrument
    timeframe: TimeFrame
    as_of: datetime
    features: tuple[FeatureValue, ...]
    quality: FeatureQuality
    source: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "feature set timestamp")

    def get(self, name: FeatureName) -> FeatureValue | None:
        return next((feature for feature in self.features if feature.name is name), None)

    def value(self, name: FeatureName) -> Decimal | None:
        feature = self.get(name)
        return None if feature is None else feature.value

    def normalized(self, name: FeatureName) -> Decimal | None:
        feature = self.get(name)
        return None if feature is None else feature.normalized_value


class MultiTimeframeFeatureSet(FrozenDomainModel):
    instrument: UniversalInstrument
    as_of: datetime
    feature_sets: tuple[FeatureSet, ...]
    missing_timeframes: tuple[TimeFrame, ...] = ()
    quality: FeatureQuality
    engine_version: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "multi-timeframe feature timestamp")

    def for_timeframe(self, timeframe: TimeFrame) -> FeatureSet | None:
        return next((item for item in self.feature_sets if item.timeframe is timeframe), None)

    def first_value(self, name: FeatureName) -> Decimal | None:
        for feature_set in self.feature_sets:
            value = feature_set.value(name)
            if value is not None:
                return value
        return None


class MarketRegimeAssessment(FrozenDomainModel):
    instrument: UniversalInstrument
    as_of: datetime
    trend: RegimeLabel
    volatility: RegimeLabel
    risk_environment: RegimeLabel
    composite: tuple[RegimeLabel, ...]
    confidence: Decimal = Field(ge=0, le=1)
    supporting_factors: tuple[str, ...] = ()
    conflicting_factors: tuple[str, ...] = ()
    data_quality: FeatureQuality
    engine_version: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "market regime timestamp")


class StrategyWeight(FrozenDomainModel):
    strategy_id: str = Field(min_length=1)
    weight: Decimal = Field(ge=0)


class AssetStrategyProfile(FrozenDomainModel):
    asset_class: AssetClass
    profile_version: str = Field(min_length=1)
    enabled: bool = True
    minimum_score_for_buy: Decimal = Field(default=Decimal("70"), ge=0, le=100)
    minimum_confidence_for_buy: Decimal = Field(default=Decimal("0.65"), ge=0, le=1)
    confidence_model_version: str = Field(default="V1_LEGACY", min_length=1)
    confidence_semantics_version: str = Field(
        default="MIXED_SIGNAL_AND_MARKET_QUALITY_V1", min_length=1
    )
    confidence_threshold_provenance: str = Field(default="SAFETY_DEFAULT", min_length=1)
    calibration_dataset_id: str | None = Field(default=None, min_length=1)
    calibration_version: str | None = Field(default=None, min_length=1)
    calibration_timestamp: datetime | None = None
    validation_status: str = Field(default="NOT_EMPIRICALLY_CALIBRATED", min_length=1)
    evidence_warning: str | None = Field(default=None, min_length=1)
    maximum_spread_for_positive_liquidity: Decimal | None = Field(default=Decimal("0.02"), ge=0)
    volatility_caution_threshold: Decimal | None = Field(default=Decimal("0.08"), ge=0)
    strong_volatility_threshold: Decimal | None = Field(default=Decimal("0.15"), ge=0)
    strategy_weights: tuple[StrategyWeight, ...]
    regime_suitability: tuple[StrategyWeight, ...] = ()

    @model_validator(mode="after")
    def validate_unique_strategy_weights(self) -> AssetStrategyProfile:
        names = [weight.strategy_id for weight in self.strategy_weights]
        if len(names) != len(set(names)):
            raise ValueError("strategy weights must be unique")
        return self

    @field_validator("calibration_timestamp")
    @classmethod
    def calibration_timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "calibration timestamp")

    def weight_for(self, strategy_id: str) -> Decimal:
        return next(
            (
                strategy_weight.weight
                for strategy_weight in self.strategy_weights
                if strategy_weight.strategy_id == strategy_id
            ),
            Decimal("0"),
        )


class EventRiskFlag(FrozenDomainModel):
    event_type: EventRiskType
    timestamp: datetime
    severity: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    source: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "event risk timestamp")


class NewsSignal(FrozenDomainModel):
    instrument: UniversalInstrument
    timestamp: datetime
    status: NewsSignalStatus
    sentiment: Decimal | None = Field(default=None, ge=Decimal("-1"), le=Decimal("1"))
    impact: Decimal | None = Field(default=None, ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    source_quality: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    event_type: EventRiskType | None = None
    event_risks: tuple[EventRiskFlag, ...] = ()

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "news signal timestamp")


class RiskBudgetContext(FrozenDomainModel):
    portfolio_value: Decimal = Field(gt=0)
    available_cash: Decimal = Field(ge=0)
    current_drawdown: Decimal = Field(ge=0, le=1)
    asset_class_exposure: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    instrument_exposure: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    daily_new_trades_remaining: int = Field(default=0, ge=0)
    data_quality: DataQualityStatus = DataQualityStatus.PARTIAL


class CorrelationResult(FrozenDomainModel):
    instrument_key: str = Field(min_length=1)
    related_instrument_key: str = Field(min_length=1)
    correlation: Decimal | None = Field(default=None, ge=Decimal("-1"), le=Decimal("1"))
    sample_size: int = Field(ge=0)
    time_window: int = Field(gt=0)
    quality: CorrelationQuality


class PortfolioFitAssessment(FrozenDomainModel):
    instrument: UniversalInstrument
    status: PortfolioFitStatus
    score: Decimal = Field(ge=0, le=100)
    diversification_score: Decimal = Field(ge=0, le=100)
    projected_cash_reserve: Decimal = Field(ge=0, le=1)
    projected_concentration: Decimal = Field(ge=0)
    reasons: tuple[str, ...] = ()
    correlations: tuple[CorrelationResult, ...] = ()


class StrategySignal(FrozenDomainModel):
    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    instrument: UniversalInstrument
    direction: StrategyDirection
    strength: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    time_horizon: TimeFrame | None = None
    supporting_factors: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    data_quality: FeatureQuality


class StrategyEvaluationContext(FrozenDomainModel):
    candidate: OpportunityCandidate
    features: MultiTimeframeFeatureSet
    regime: MarketRegimeAssessment
    portfolio_fit: PortfolioFitAssessment | None = None
    news_signal: NewsSignal | None = None
    risk_budget: RiskBudgetContext | None = None
    profile: AssetStrategyProfile


class StrategyEnsembleResult(FrozenDomainModel):
    instrument: UniversalInstrument
    as_of: datetime
    direction: StrategyDirection
    strength: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    agreement: Decimal = Field(ge=0, le=1)
    conflict: Decimal = Field(ge=0, le=1)
    weighted_score: Decimal = Field(ge=Decimal("-1"), le=Decimal("1"))
    signals: tuple[StrategySignal, ...]
    supporting_factors: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = ()
    data_quality: FeatureQuality
    ensemble_version: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "ensemble timestamp")


class AegisOpportunityScore(FrozenDomainModel):
    instrument: UniversalInstrument
    timestamp: datetime
    overall_score: Decimal = Field(ge=0, le=100)
    band: ScoreBand
    trend_score: Decimal = Field(ge=0, le=100)
    momentum_score: Decimal = Field(ge=0, le=100)
    regime_score: Decimal = Field(ge=0, le=100)
    risk_adjusted_score: Decimal = Field(ge=0, le=100)
    liquidity_score: Decimal = Field(ge=0, le=100)
    portfolio_fit_score: Decimal = Field(ge=0, le=100)
    data_quality_score: Decimal = Field(ge=0, le=100)
    news_score: Decimal = Field(default=Decimal("50"), ge=0, le=100)
    event_risk_penalty: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    risk_penalty: Decimal = Field(ge=0, le=100)
    confidence: Decimal = Field(ge=0, le=1)
    signal_reliability_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    execution_readiness_score: Decimal | None = Field(default=None, ge=0, le=1)
    confidence_model_version: str = Field(default="V1_LEGACY", min_length=1)
    confidence_semantics_version: str = Field(
        default="MIXED_SIGNAL_AND_MARKET_QUALITY_V1", min_length=1
    )
    confidence_threshold_provenance: str = Field(default="SAFETY_DEFAULT", min_length=1)
    confidence_threshold: Decimal | None = Field(default=None, ge=0, le=1)
    calibration_dataset_id: str | None = Field(default=None, min_length=1)
    calibration_version: str | None = Field(default=None, min_length=1)
    asset_evidence_warning: str | None = Field(default=None, min_length=1)
    score_version: str = Field(min_length=1)
    components: tuple[str, ...] = ()

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "opportunity score timestamp")


class AegisOpportunityAnalysis(FrozenDomainModel):
    candidate: OpportunityCandidate
    features: MultiTimeframeFeatureSet
    regime: MarketRegimeAssessment
    strategy_signals: tuple[StrategySignal, ...]
    ensemble: StrategyEnsembleResult
    opportunity_score: AegisOpportunityScore
    portfolio_fit: PortfolioFitAssessment
    news_signal: NewsSignal
    risk_budget: RiskBudgetContext
    decision: AegisDecision
    generated_at: datetime
    strategy_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    data_digest: str = Field(min_length=16)
    reasons: tuple[str, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "analysis timestamp")
