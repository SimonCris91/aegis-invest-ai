"""Market regime classification from normalized features."""

from datetime import datetime
from decimal import Decimal

from app.domain.versions import REGIME_ENGINE_VERSION
from app.intelligence.models import (
    FeatureName,
    FeatureQuality,
    MarketRegimeAssessment,
    MultiTimeframeFeatureSet,
    RegimeLabel,
)


class MarketRegimeEngine:
    def __init__(self, *, engine_version: str = REGIME_ENGINE_VERSION) -> None:
        self._engine_version = engine_version

    @property
    def engine_version(self) -> str:
        return self._engine_version

    def assess(
        self, features: MultiTimeframeFeatureSet, *, as_of: datetime
    ) -> MarketRegimeAssessment:
        trend_score = _first_normalized(
            features,
            (
                FeatureName.PRICE_VS_MOVING_AVERAGE,
                FeatureName.MOVING_AVERAGE_SLOPE,
                FeatureName.TREND_PERSISTENCE,
            ),
        )
        volatility = features.first_value(FeatureName.REALIZED_VOLATILITY)
        drawdown = features.first_value(FeatureName.DRAWDOWN)
        supporting: list[str] = []
        conflicting: list[str] = []

        trend = _trend_label(trend_score)
        if trend is RegimeLabel.UNKNOWN:
            conflicting.append("trend evidence is unavailable")
        elif trend in {RegimeLabel.UPTREND, RegimeLabel.STRONG_UPTREND}:
            supporting.append("trend features are positive")
        elif trend in {RegimeLabel.DOWNTREND, RegimeLabel.STRONG_DOWNTREND}:
            conflicting.append("trend features are negative")
        else:
            supporting.append("trend features suggest a range")

        volatility_label = RegimeLabel.UNKNOWN
        if volatility is not None:
            if volatility >= Decimal("0.10"):
                volatility_label = RegimeLabel.HIGH_VOLATILITY
                conflicting.append("realized volatility is elevated")
            elif volatility <= Decimal("0.02"):
                volatility_label = RegimeLabel.LOW_VOLATILITY
                supporting.append("realized volatility is contained")
            else:
                volatility_label = RegimeLabel.TRANSITION
        else:
            conflicting.append("volatility evidence is unavailable")

        risk_environment = RegimeLabel.UNKNOWN
        if trend in {RegimeLabel.UPTREND, RegimeLabel.STRONG_UPTREND} and volatility_label in {
            RegimeLabel.LOW_VOLATILITY,
            RegimeLabel.TRANSITION,
        }:
            risk_environment = RegimeLabel.RISK_ON
            supporting.append("trend and volatility are compatible with risk-on conditions")
        elif (
            trend in {RegimeLabel.DOWNTREND, RegimeLabel.STRONG_DOWNTREND}
            or volatility_label is RegimeLabel.HIGH_VOLATILITY
            or (drawdown is not None and drawdown >= Decimal("0.15"))
        ):
            risk_environment = RegimeLabel.RISK_OFF
            conflicting.append("market risk conditions are defensive")
        elif trend is RegimeLabel.RANGE:
            risk_environment = RegimeLabel.TRANSITION
        else:
            conflicting.append("risk environment evidence is incomplete")

        confidence = _confidence(features.quality, trend, volatility_label, risk_environment)
        return MarketRegimeAssessment(
            instrument=features.instrument,
            as_of=as_of,
            trend=trend,
            volatility=volatility_label,
            risk_environment=risk_environment,
            composite=(trend, volatility_label, risk_environment),
            confidence=confidence,
            supporting_factors=tuple(supporting),
            conflicting_factors=tuple(conflicting),
            data_quality=features.quality,
            engine_version=self._engine_version,
        )


def _first_normalized(
    features: MultiTimeframeFeatureSet, names: tuple[FeatureName, ...]
) -> Decimal | None:
    values: list[Decimal] = []
    for name in names:
        for feature_set in features.feature_sets:
            value = feature_set.normalized(name)
            if value is not None:
                values.append(value)
                break
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _trend_label(score: Decimal | None) -> RegimeLabel:
    if score is None:
        return RegimeLabel.UNKNOWN
    if score >= Decimal("0.45"):
        return RegimeLabel.STRONG_UPTREND
    if score >= Decimal("0.15"):
        return RegimeLabel.UPTREND
    if score <= Decimal("-0.45"):
        return RegimeLabel.STRONG_DOWNTREND
    if score <= Decimal("-0.15"):
        return RegimeLabel.DOWNTREND
    return RegimeLabel.RANGE


def _confidence(
    quality: FeatureQuality,
    trend: RegimeLabel,
    volatility: RegimeLabel,
    risk_environment: RegimeLabel,
) -> Decimal:
    base = {
        FeatureQuality.GOOD: Decimal("0.80"),
        FeatureQuality.PARTIAL: Decimal("0.55"),
        FeatureQuality.DATA_INSUFFICIENT: Decimal("0.20"),
        FeatureQuality.UNKNOWN: Decimal("0.10"),
        FeatureQuality.STALE: Decimal("0.10"),
        FeatureQuality.CONFLICTING: Decimal("0.15"),
    }[quality]
    missing = sum(
        1 for label in (trend, volatility, risk_environment) if label is RegimeLabel.UNKNOWN
    )
    return max(Decimal("0"), base - Decimal("0.15") * Decimal(missing))
