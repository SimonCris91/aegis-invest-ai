"""Strategy ensemble with agreement, conflict, and quality-aware confidence."""

from datetime import datetime
from decimal import Decimal

from app.domain.versions import STRATEGY_ENSEMBLE_VERSION
from app.intelligence.models import (
    FeatureQuality,
    StrategyDirection,
    StrategyEnsembleResult,
    StrategyEvaluationContext,
    StrategySignal,
)
from app.intelligence.ports import StrategySignalProvider

_POSITIVE = {
    StrategyDirection.STRONG_BUY: Decimal("1.00"),
    StrategyDirection.BUY: Decimal("0.70"),
    StrategyDirection.WATCH: Decimal("0.25"),
    StrategyDirection.HOLD: Decimal("0"),
    StrategyDirection.REDUCE: Decimal("-0.65"),
    StrategyDirection.AVOID: Decimal("-1.00"),
}


class StrategyEnsemble:
    def __init__(
        self,
        providers: tuple[StrategySignalProvider, ...],
        *,
        ensemble_version: str = STRATEGY_ENSEMBLE_VERSION,
    ) -> None:
        if not providers:
            raise ValueError("at least one strategy signal provider is required")
        self._providers = providers
        self._ensemble_version = ensemble_version

    @property
    def ensemble_version(self) -> str:
        return self._ensemble_version

    def evaluate(
        self, context: StrategyEvaluationContext, *, as_of: datetime
    ) -> StrategyEnsembleResult:
        signals = tuple(provider.evaluate(context) for provider in self._providers)
        weighted_sum = Decimal("0")
        weight_total = Decimal("0")
        factors: list[str] = []
        risks: list[str] = []
        for signal in signals:
            weight = context.profile.weight_for(signal.strategy_id)
            if weight <= 0:
                continue
            signed_strength = _POSITIVE[signal.direction] * signal.strength
            weighted_sum += signed_strength * weight * signal.confidence
            weight_total += weight * max(signal.confidence, Decimal("0.01"))
            factors.extend(signal.supporting_factors[:2])
            risks.extend(signal.risk_factors[:2])
        weighted_score = Decimal("0") if weight_total == 0 else weighted_sum / weight_total
        agreement = _agreement(signals)
        conflict = Decimal("1") - agreement
        direction = _direction(weighted_score, signals)
        base_confidence = _average_confidence(signals)
        confidence = base_confidence * (Decimal("0.50") + agreement / Decimal("2"))
        confidence *= _quality_multiplier(context.features.quality)
        confidence *= max(context.regime.confidence, Decimal("0.20"))
        if _has_strong_disagreement(signals):
            risks.append("strategy disagreement is material")
            confidence *= Decimal("0.70")
        return StrategyEnsembleResult(
            instrument=context.candidate.instrument,
            as_of=as_of,
            direction=direction,
            strength=min(Decimal("1"), abs(weighted_score)).quantize(Decimal("0.01")),
            confidence=max(Decimal("0"), min(Decimal("1"), confidence)).quantize(Decimal("0.01")),
            agreement=agreement,
            conflict=conflict,
            weighted_score=max(Decimal("-1"), min(Decimal("1"), weighted_score)).quantize(
                Decimal("0.01")
            ),
            signals=signals,
            supporting_factors=tuple(dict.fromkeys(factors)),
            risk_factors=tuple(dict.fromkeys(risks)),
            data_quality=context.features.quality,
            ensemble_version=self._ensemble_version,
        )


def _direction(weighted_score: Decimal, signals: tuple[StrategySignal, ...]) -> StrategyDirection:
    defensive_avoid = any(
        signal.strategy_id == "defensive" and signal.direction is StrategyDirection.AVOID
        for signal in signals
    )
    if defensive_avoid and weighted_score < Decimal("0.50"):
        return StrategyDirection.AVOID
    if weighted_score >= Decimal("0.65"):
        return StrategyDirection.STRONG_BUY
    if weighted_score >= Decimal("0.35"):
        return StrategyDirection.BUY
    if weighted_score >= Decimal("0.15"):
        return StrategyDirection.WATCH
    if weighted_score <= Decimal("-0.50"):
        return StrategyDirection.AVOID
    if weighted_score <= Decimal("-0.25"):
        return StrategyDirection.REDUCE
    return StrategyDirection.HOLD


def _agreement(signals: tuple[StrategySignal, ...]) -> Decimal:
    if len(signals) < 2:
        return Decimal("1")
    signs = tuple(_sign(signal.direction) for signal in signals)
    majority = max((signs.count(-1), signs.count(0), signs.count(1)))
    return (Decimal(majority) / Decimal(len(signals))).quantize(Decimal("0.01"))


def _sign(direction: StrategyDirection) -> int:
    if direction in {StrategyDirection.STRONG_BUY, StrategyDirection.BUY, StrategyDirection.WATCH}:
        return 1
    if direction in {StrategyDirection.REDUCE, StrategyDirection.AVOID}:
        return -1
    return 0


def _average_confidence(signals: tuple[StrategySignal, ...]) -> Decimal:
    if not signals:
        return Decimal("0")
    return sum((signal.confidence for signal in signals), Decimal("0")) / Decimal(len(signals))


def _quality_multiplier(quality: FeatureQuality) -> Decimal:
    return {
        FeatureQuality.GOOD: Decimal("1"),
        FeatureQuality.PARTIAL: Decimal("0.70"),
        FeatureQuality.DATA_INSUFFICIENT: Decimal("0.25"),
        FeatureQuality.UNKNOWN: Decimal("0.20"),
        FeatureQuality.STALE: Decimal("0.20"),
        FeatureQuality.CONFLICTING: Decimal("0.30"),
    }[quality]


def _has_strong_disagreement(signals: tuple[StrategySignal, ...]) -> bool:
    positive = any(
        signal.direction in {StrategyDirection.STRONG_BUY, StrategyDirection.BUY}
        for signal in signals
    )
    negative = any(
        signal.direction in {StrategyDirection.REDUCE, StrategyDirection.AVOID}
        for signal in signals
    )
    return positive and negative
