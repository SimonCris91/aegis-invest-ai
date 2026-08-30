"""Opportunity score and confidence calibration analysis."""

from collections.abc import Iterable
from decimal import Decimal

from app.validation.metrics import NOT_ENOUGH_DATA
from app.validation.models import CalibrationBucket, ReplayDecision

SCORE_BUCKETS = (
    ("90-100", Decimal("90"), Decimal("100")),
    ("80-89", Decimal("80"), Decimal("89.9999")),
    ("70-79", Decimal("70"), Decimal("79.9999")),
    ("60-69", Decimal("60"), Decimal("69.9999")),
    ("50-59", Decimal("50"), Decimal("59.9999")),
    ("40-49", Decimal("40"), Decimal("49.9999")),
    ("0-39", Decimal("0"), Decimal("39.9999")),
)

CONFIDENCE_BUCKETS = (
    ("0.0-0.2", Decimal("0"), Decimal("0.2")),
    ("0.2-0.4", Decimal("0.2"), Decimal("0.4")),
    ("0.4-0.6", Decimal("0.4"), Decimal("0.6")),
    ("0.6-0.8", Decimal("0.6"), Decimal("0.8")),
    ("0.8-1.0", Decimal("0.8"), Decimal("1")),
)


class CalibrationAnalyzer:
    def by_score(self, decisions: tuple[ReplayDecision, ...]) -> tuple[CalibrationBucket, ...]:
        return tuple(
            _bucket(label, lower, upper, _returns_for(decisions, "score", lower, upper))
            for label, lower, upper in SCORE_BUCKETS
        )

    def by_confidence(self, decisions: tuple[ReplayDecision, ...]) -> tuple[CalibrationBucket, ...]:
        return tuple(
            _bucket(label, lower, upper, _returns_for(decisions, "confidence", lower, upper))
            for label, lower, upper in CONFIDENCE_BUCKETS
        )

    def score_80_outperforms_60s(self, buckets: tuple[CalibrationBucket, ...]) -> bool:
        high = next((item for item in buckets if item.label == "80-89"), None)
        sixties = next((item for item in buckets if item.label == "60-69"), None)
        if high is None or sixties is None:
            return False
        if isinstance(high.mean_return, str) or isinstance(sixties.mean_return, str):
            return False
        return high.mean_return > sixties.mean_return


def _returns_for(
    decisions: tuple[ReplayDecision, ...], attribute: str, lower: Decimal, upper: Decimal
) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for decision in decisions:
        candidate_value = decision.score if attribute == "score" else decision.confidence
        if lower <= candidate_value <= upper and decision.forward_return is not None:
            values.append(decision.forward_return)
    return tuple(values)


def _bucket(
    label: str, lower: Decimal, upper: Decimal, returns: tuple[Decimal, ...]
) -> CalibrationBucket:
    return CalibrationBucket(
        label=label,
        lower=lower,
        upper=upper,
        sample_size=len(returns),
        mean_return=_mean(returns),
        median_return=_median(returns),
        win_rate=_win_rate(returns),
        maximum_drawdown=abs(min(returns)) if returns else NOT_ENOUGH_DATA,
        mfe=max(returns) if returns else NOT_ENOUGH_DATA,
        mae=min(returns) if returns else NOT_ENOUGH_DATA,
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal | str:
    if not values:
        return NOT_ENOUGH_DATA
    return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: tuple[Decimal, ...]) -> Decimal | str:
    if not values:
        return NOT_ENOUGH_DATA
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _win_rate(values: Iterable[Decimal]) -> Decimal | str:
    collected = tuple(values)
    if not collected:
        return NOT_ENOUGH_DATA
    return Decimal(sum(1 for value in collected if value > 0)) / Decimal(len(collected))
