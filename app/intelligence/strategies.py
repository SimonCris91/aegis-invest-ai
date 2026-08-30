"""Deterministic strategy signal providers."""

from decimal import Decimal

from app.domain.versions import STRATEGY_VERSION
from app.intelligence.models import (
    FeatureName,
    FeatureQuality,
    FeatureSet,
    RegimeLabel,
    StrategyDirection,
    StrategyEvaluationContext,
    StrategySignal,
    TimeFrame,
)
from app.intelligence.profiles import (
    BREAKOUT_ID,
    DEFENSIVE_ID,
    MEAN_REVERSION_ID,
    MOMENTUM_ID,
    TREND_FOLLOWING_ID,
)


class TrendFollowingStrategy:
    strategy_id = TREND_FOLLOWING_ID
    strategy_version = f"{STRATEGY_VERSION}:trend-v1"

    def evaluate(self, context: StrategyEvaluationContext) -> StrategySignal:
        feature_set = _preferred_features(context)
        price_vs_ma = _normalized(feature_set, FeatureName.PRICE_VS_MOVING_AVERAGE)
        slope = _normalized(feature_set, FeatureName.MOVING_AVERAGE_SLOPE)
        persistence = feature_set.value(FeatureName.TREND_PERSISTENCE)
        factors: list[str] = []
        risks: list[str] = []
        confirmations = 0

        if price_vs_ma is not None and price_vs_ma > Decimal("0.05"):
            confirmations += 1
            factors.append("price is above its moving average")
        if slope is not None and slope > Decimal("0.05"):
            confirmations += 1
            factors.append("moving average slope is positive")
        if persistence is not None and persistence >= Decimal("0.55"):
            confirmations += 1
            factors.append("positive sessions are persistent")
        regime_supports_trend = context.regime.trend in {
            RegimeLabel.UPTREND,
            RegimeLabel.STRONG_UPTREND,
        }
        if regime_supports_trend:
            confirmations += 1
            factors.append("market regime supports trend following")

        if confirmations >= 4 and regime_supports_trend:
            direction = StrategyDirection.STRONG_BUY
            strength = Decimal("0.85")
        elif confirmations >= 3 and regime_supports_trend:
            direction = StrategyDirection.BUY
            strength = Decimal("0.68")
        elif confirmations >= 2:
            direction = StrategyDirection.WATCH
            strength = Decimal("0.45")
            if not regime_supports_trend:
                risks.append("market regime does not confirm a trend-following setup")
        else:
            direction = StrategyDirection.HOLD
            strength = Decimal("0.20")
            risks.append("trend confirmation is incomplete")

        confidence = _confidence_from_quality(feature_set.quality, context.regime.confidence)
        return _signal(
            context,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            direction=direction,
            strength=strength,
            confidence=confidence,
            factors=tuple(factors),
            risks=tuple(risks),
            invalidation=("price loses moving-average support",),
            horizon=feature_set.timeframe,
            quality=feature_set.quality,
        )


class MomentumStrategy:
    strategy_id = MOMENTUM_ID
    strategy_version = f"{STRATEGY_VERSION}:momentum-v1"

    def evaluate(self, context: StrategyEvaluationContext) -> StrategySignal:
        feature_set = _preferred_features(context)
        short = _normalized(feature_set, FeatureName.SHORT_TERM_MOMENTUM)
        medium = _normalized(feature_set, FeatureName.MEDIUM_TERM_MOMENTUM)
        long = _normalized(feature_set, FeatureName.LONG_TERM_MOMENTUM)
        values = tuple(value for value in (short, medium, long) if value is not None)
        factors: list[str] = []
        risks: list[str] = []
        if len(values) < 2:
            return _signal(
                context,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                direction=StrategyDirection.HOLD,
                strength=Decimal("0.10"),
                confidence=Decimal("0.15"),
                factors=(),
                risks=("multi-horizon momentum is insufficient",),
                invalidation=("momentum data remains unavailable",),
                horizon=feature_set.timeframe,
                quality=FeatureQuality.DATA_INSUFFICIENT,
            )

        average = sum(values, Decimal("0")) / Decimal(len(values))
        if short is not None and medium is not None and short < medium:
            risks.append("momentum is decelerating")
        if average > Decimal("0.35") and len(values) == 3:
            direction = StrategyDirection.BUY
            strength = Decimal("0.72")
            factors.append("momentum is positive across multiple horizons")
        elif average > Decimal("0.15"):
            direction = StrategyDirection.WATCH
            strength = Decimal("0.50")
            factors.append("momentum is constructive but not decisive")
        elif average < Decimal("-0.20"):
            direction = StrategyDirection.AVOID
            strength = Decimal("0.65")
            risks.append("momentum is negative across available horizons")
        else:
            direction = StrategyDirection.HOLD
            strength = Decimal("0.25")
            risks.append("momentum is mixed")
        confidence = _confidence_from_quality(feature_set.quality, context.regime.confidence)
        if risks:
            confidence = max(Decimal("0"), confidence - Decimal("0.10"))
        return _signal(
            context,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            direction=direction,
            strength=strength,
            confidence=confidence,
            factors=tuple(factors),
            risks=tuple(risks),
            invalidation=("multi-horizon momentum turns negative",),
            horizon=feature_set.timeframe,
            quality=feature_set.quality,
        )


class BreakoutStrategy:
    strategy_id = BREAKOUT_ID
    strategy_version = f"{STRATEGY_VERSION}:breakout-v1"

    def evaluate(self, context: StrategyEvaluationContext) -> StrategySignal:
        feature_set = _preferred_features(context)
        breakout = _normalized(feature_set, FeatureName.BREAKOUT_STRENGTH)
        range_position = feature_set.value(FeatureName.RANGE_POSITION)
        relative_volume = feature_set.value(FeatureName.RELATIVE_VOLUME)
        factors: list[str] = []
        risks: list[str] = []
        confirmations = 0
        if breakout is not None and breakout > Decimal("0.05"):
            confirmations += 1
            factors.append("price is testing or exceeding recent range resistance")
        if range_position is not None and range_position >= Decimal("0.80"):
            confirmations += 1
            factors.append("price is in the upper part of its recent range")
        if relative_volume is None:
            risks.append("volume confirmation is unavailable")
        elif relative_volume >= Decimal("1.10"):
            confirmations += 1
            factors.append("relative volume confirms participation")
        else:
            risks.append("breakout lacks volume expansion")

        if confirmations >= 3:
            direction = StrategyDirection.BUY
            strength = Decimal("0.72")
        elif confirmations >= 2:
            direction = StrategyDirection.WATCH
            strength = Decimal("0.50")
        else:
            direction = StrategyDirection.HOLD
            strength = Decimal("0.20")
            risks.append("breakout confirmation is weak")
        confidence = _confidence_from_quality(feature_set.quality, context.regime.confidence)
        if relative_volume is None:
            confidence = max(Decimal("0"), confidence - Decimal("0.15"))
        return _signal(
            context,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            direction=direction,
            strength=strength,
            confidence=confidence,
            factors=tuple(factors),
            risks=tuple(risks),
            invalidation=("breakout level fails and price returns into range",),
            horizon=feature_set.timeframe,
            quality=feature_set.quality,
        )


class MeanReversionStrategy:
    strategy_id = MEAN_REVERSION_ID
    strategy_version = f"{STRATEGY_VERSION}:mean-reversion-v1"

    def evaluate(self, context: StrategyEvaluationContext) -> StrategySignal:
        feature_set = _preferred_features(context)
        z_score = feature_set.value(FeatureName.MEAN_REVERSION_Z_SCORE)
        risks: list[str] = []
        factors: list[str] = []
        if z_score is None:
            return _signal(
                context,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                direction=StrategyDirection.HOLD,
                strength=Decimal("0.10"),
                confidence=Decimal("0.15"),
                factors=(),
                risks=("mean-reversion statistics are unavailable",),
                invalidation=("mean-reversion evidence remains unavailable",),
                horizon=feature_set.timeframe,
                quality=FeatureQuality.DATA_INSUFFICIENT,
            )
        if context.regime.trend in {RegimeLabel.DOWNTREND, RegimeLabel.STRONG_DOWNTREND}:
            risks.append("falling price is not treated as cheap in a downtrend")
        if context.candidate.features.current_portfolio_weight > Decimal("0"):
            risks.append("existing exposure prevents blind averaging down")
        if z_score <= Decimal("-1.50") and context.regime.trend is RegimeLabel.RANGE and not risks:
            direction = StrategyDirection.WATCH
            strength = Decimal("0.50")
            factors.append("range regime and oversold z-score warrant observation")
        elif z_score >= Decimal("1.50"):
            direction = StrategyDirection.REDUCE
            strength = Decimal("0.55")
            risks.append("price is stretched above its recent mean")
        else:
            direction = StrategyDirection.HOLD
            strength = Decimal("0.25")
            factors.append("mean-reversion evidence is not decisive")
        return _signal(
            context,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            direction=direction,
            strength=strength,
            confidence=_confidence_from_quality(feature_set.quality, context.regime.confidence),
            factors=tuple(factors),
            risks=tuple(risks),
            invalidation=("mean-reversion setup disappears or trend breakdown continues",),
            horizon=feature_set.timeframe,
            quality=feature_set.quality,
        )


class DefensiveStrategy:
    strategy_id = DEFENSIVE_ID
    strategy_version = f"{STRATEGY_VERSION}:defensive-v1"

    def evaluate(self, context: StrategyEvaluationContext) -> StrategySignal:
        feature_set = _preferred_features(context)
        volatility = feature_set.value(FeatureName.REALIZED_VOLATILITY)
        spread = context.candidate.features.spread_percentage
        reasons: list[str] = []
        if context.features.quality is not FeatureQuality.GOOD:
            reasons.append("data quality is incomplete")
        if context.regime.volatility is RegimeLabel.HIGH_VOLATILITY:
            reasons.append("regime volatility is high")
        if context.regime.risk_environment is RegimeLabel.RISK_OFF:
            reasons.append("risk environment is defensive")
        if volatility is not None and context.profile.strong_volatility_threshold is not None:
            if volatility >= context.profile.strong_volatility_threshold:
                reasons.append("realized volatility exceeds the asset profile threshold")
        if spread is not None and context.profile.maximum_spread_for_positive_liquidity is not None:
            if spread > context.profile.maximum_spread_for_positive_liquidity:
                reasons.append("spread is wider than the asset profile threshold")
        if context.portfolio_fit is not None and context.portfolio_fit.status.name in {
            "NEGATIVE",
            "BLOCKED",
        }:
            reasons.append("portfolio fit is not constructive")

        if len(reasons) >= 3:
            direction = StrategyDirection.AVOID
            strength = Decimal("0.85")
        elif reasons:
            direction = StrategyDirection.HOLD
            strength = Decimal("0.55")
        else:
            direction = StrategyDirection.WATCH
            strength = Decimal("0.25")
        return _signal(
            context,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            direction=direction,
            strength=strength,
            confidence=max(
                Decimal("0.30"),
                _confidence_from_quality(feature_set.quality, context.regime.confidence),
            ),
            factors=("capital preservation check completed",),
            risks=tuple(reasons),
            invalidation=("defensive conditions normalize",),
            horizon=feature_set.timeframe,
            quality=feature_set.quality,
        )


def _preferred_features(context: StrategyEvaluationContext) -> FeatureSet:
    return (
        context.features.for_timeframe(TimeFrame.ONE_DAY)
        or context.features.for_timeframe(TimeFrame.FOUR_HOUR)
        or context.features.feature_sets[0]
    )


def _normalized(feature_set: FeatureSet, name: FeatureName) -> Decimal | None:
    feature = feature_set.get(name)
    if feature is None:
        return None
    return feature.normalized_value


def _confidence_from_quality(quality: FeatureQuality, regime_confidence: Decimal) -> Decimal:
    quality_score = {
        FeatureQuality.GOOD: Decimal("0.85"),
        FeatureQuality.PARTIAL: Decimal("0.55"),
        FeatureQuality.DATA_INSUFFICIENT: Decimal("0.15"),
        FeatureQuality.UNKNOWN: Decimal("0.10"),
        FeatureQuality.STALE: Decimal("0.10"),
        FeatureQuality.CONFLICTING: Decimal("0.15"),
    }[quality]
    return ((quality_score + regime_confidence) / Decimal("2")).quantize(Decimal("0.01"))


def _signal(
    context: StrategyEvaluationContext,
    *,
    strategy_id: str,
    strategy_version: str,
    direction: StrategyDirection,
    strength: Decimal,
    confidence: Decimal,
    factors: tuple[str, ...],
    risks: tuple[str, ...],
    invalidation: tuple[str, ...],
    horizon: TimeFrame,
    quality: FeatureQuality,
) -> StrategySignal:
    return StrategySignal(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        instrument=context.candidate.instrument,
        direction=direction,
        strength=strength,
        confidence=confidence,
        time_horizon=horizon,
        supporting_factors=factors,
        risk_factors=risks,
        invalidation_conditions=invalidation,
        data_quality=quality,
    )
