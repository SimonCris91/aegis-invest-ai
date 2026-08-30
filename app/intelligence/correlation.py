"""Rolling correlation utilities with explicit data-quality output."""

import math
from decimal import Decimal

from app.intelligence.models import CorrelationQuality, CorrelationResult, MarketBar


class CorrelationEngine:
    def __init__(self, *, minimum_samples: int = 10) -> None:
        if minimum_samples < 2:
            raise ValueError("minimum_samples must be at least 2")
        self._minimum_samples = minimum_samples

    def rolling_correlation(
        self,
        left: tuple[MarketBar, ...],
        right: tuple[MarketBar, ...],
        *,
        time_window: int,
    ) -> CorrelationResult:
        if not left or not right:
            return _insufficient("unknown", "unknown", time_window=time_window)
        left_key = left[-1].instrument.key
        right_key = right[-1].instrument.key
        left_returns = _returns(left[-time_window:])
        right_returns = _returns(right[-time_window:])
        sample_size = min(len(left_returns), len(right_returns))
        if sample_size < self._minimum_samples:
            return CorrelationResult(
                instrument_key=left_key,
                related_instrument_key=right_key,
                correlation=None,
                sample_size=sample_size,
                time_window=time_window,
                quality=CorrelationQuality.DATA_INSUFFICIENT,
            )
        correlation = _pearson(left_returns[-sample_size:], right_returns[-sample_size:])
        return CorrelationResult(
            instrument_key=left_key,
            related_instrument_key=right_key,
            correlation=correlation,
            sample_size=sample_size,
            time_window=time_window,
            quality=CorrelationQuality.GOOD,
        )


def _insufficient(left_key: str, right_key: str, *, time_window: int) -> CorrelationResult:
    return CorrelationResult(
        instrument_key=left_key,
        related_instrument_key=right_key,
        correlation=None,
        sample_size=0,
        time_window=time_window,
        quality=CorrelationQuality.DATA_INSUFFICIENT,
    )


def _returns(bars: tuple[MarketBar, ...]) -> tuple[Decimal, ...]:
    if len(bars) < 2:
        return ()
    closes = tuple(bar.close for bar in bars)
    return tuple(
        (closes[index] - closes[index - 1]) / closes[index - 1]
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    )


def _pearson(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> Decimal:
    left_mean = sum(left, Decimal("0")) / Decimal(len(left))
    right_mean = sum(right, Decimal("0")) / Decimal(len(right))
    covariance = sum(
        ((left_item - left_mean) * (right_item - right_mean))
        for left_item, right_item in zip(left, right, strict=True)
    )
    left_variance = sum((item - left_mean) ** 2 for item in left)
    right_variance = sum((item - right_mean) ** 2 for item in right)
    denominator = Decimal(str(math.sqrt(float(left_variance * right_variance))))
    if denominator == 0:
        return Decimal("0")
    return max(Decimal("-1"), min(Decimal("1"), covariance / denominator))
