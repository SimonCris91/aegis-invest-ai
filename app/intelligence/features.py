"""Deterministic multi-timeframe market feature engine."""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal

from app.domain.versions import FEATURE_ENGINE_VERSION
from app.intelligence.models import (
    FeatureName,
    FeatureQuality,
    FeatureSet,
    FeatureValue,
    MarketBar,
    MultiTimeframeFeatureSet,
    TimeFrame,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_CORE_FEATURES = frozenset(
    {
        FeatureName.SHORT_TERM_MOMENTUM,
        FeatureName.MEDIUM_TERM_MOMENTUM,
        FeatureName.LONG_TERM_MOMENTUM,
        FeatureName.SMA,
        FeatureName.EMA,
        FeatureName.MOVING_AVERAGE_SLOPE,
        FeatureName.PRICE_VS_MOVING_AVERAGE,
        FeatureName.RSI,
        FeatureName.ATR,
        FeatureName.REALIZED_VOLATILITY,
        FeatureName.ROLLING_STANDARD_DEVIATION,
        FeatureName.DRAWDOWN,
        FeatureName.DISTANCE_FROM_RECENT_HIGH,
        FeatureName.DISTANCE_FROM_RECENT_LOW,
        FeatureName.BREAKOUT_STRENGTH,
        FeatureName.RANGE_POSITION,
        FeatureName.MEAN_REVERSION_Z_SCORE,
        FeatureName.TREND_PERSISTENCE,
    }
)


class MarketFeatureEngine:
    """Calculates explainable features without fabricating unavailable inputs."""

    def __init__(self, *, engine_version: str = FEATURE_ENGINE_VERSION) -> None:
        self._engine_version = engine_version

    @property
    def engine_version(self) -> str:
        return self._engine_version

    def analyze(
        self,
        *,
        bars_by_timeframe: dict[TimeFrame, tuple[MarketBar, ...]],
        required_timeframes: tuple[TimeFrame, ...],
        as_of: datetime,
    ) -> MultiTimeframeFeatureSet:
        if not bars_by_timeframe:
            raise ValueError("at least one timeframe key is required")
        first_series = next(iter(bars_by_timeframe.values()))
        if not first_series:
            raise ValueError("at least one non-empty bar series is required")
        instrument = first_series[0].instrument
        feature_sets: list[FeatureSet] = []
        missing: list[TimeFrame] = []
        for timeframe in required_timeframes:
            bars = bars_by_timeframe.get(timeframe, ())
            if not bars:
                missing.append(timeframe)
                continue
            feature_sets.append(self.analyze_timeframe(bars=bars, timeframe=timeframe, as_of=as_of))
        quality = _aggregate_quality(tuple(item.quality for item in feature_sets), bool(missing))
        return MultiTimeframeFeatureSet(
            instrument=instrument,
            as_of=as_of,
            feature_sets=tuple(feature_sets),
            missing_timeframes=tuple(missing),
            quality=quality,
            engine_version=self._engine_version,
        )

    def analyze_timeframe(
        self, *, bars: tuple[MarketBar, ...], timeframe: TimeFrame, as_of: datetime
    ) -> FeatureSet:
        if not bars:
            raise ValueError("bars cannot be empty for a concrete timeframe")
        ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
        instrument = ordered[-1].instrument
        closes = tuple(bar.close for bar in ordered)
        highs = tuple(bar.high for bar in ordered)
        lows = tuple(bar.low for bar in ordered)
        volumes = tuple(bar.volume for bar in ordered)
        timestamp = ordered[-1].timestamp
        source = ordered[-1].source
        features = (
            self._return(closes, timestamp=timestamp, source=source),
            self._momentum(
                FeatureName.ROLLING_RETURN,
                closes,
                lookback=5,
                timestamp=timestamp,
                source=source,
            ),
            self._momentum(
                FeatureName.SHORT_TERM_MOMENTUM,
                closes,
                lookback=3,
                timestamp=timestamp,
                source=source,
            ),
            self._momentum(
                FeatureName.MEDIUM_TERM_MOMENTUM,
                closes,
                lookback=10,
                timestamp=timestamp,
                source=source,
            ),
            self._momentum(
                FeatureName.LONG_TERM_MOMENTUM,
                closes,
                lookback=20,
                timestamp=timestamp,
                source=source,
            ),
            self._sma(closes, timestamp=timestamp, source=source),
            self._ema_feature(closes, timestamp=timestamp, source=source),
            self._ma_slope(closes, timestamp=timestamp, source=source),
            self._price_vs_ma(closes, timestamp=timestamp, source=source),
            self._rsi(closes, timestamp=timestamp, source=source),
            self._macd(closes, FeatureName.MACD, timestamp=timestamp, source=source),
            self._macd(closes, FeatureName.MACD_SIGNAL, timestamp=timestamp, source=source),
            self._macd(closes, FeatureName.MACD_HISTOGRAM, timestamp=timestamp, source=source),
            self._atr(ordered, timestamp=timestamp, source=source),
            self._realized_volatility(closes, timestamp=timestamp, source=source),
            self._rolling_stddev(closes, timestamp=timestamp, source=source),
            self._drawdown(closes, timestamp=timestamp, source=source),
            self._distance_from_high(closes, highs, timestamp=timestamp, source=source),
            self._distance_from_low(closes, lows, timestamp=timestamp, source=source),
            self._breakout_strength(closes, highs, timestamp=timestamp, source=source),
            self._range_position(closes, highs, lows, timestamp=timestamp, source=source),
            self._mean_reversion_z_score(closes, timestamp=timestamp, source=source),
            self._volume_change(volumes, timestamp=timestamp, source=source),
            self._relative_volume(volumes, timestamp=timestamp, source=source),
            self._spread_feature(ordered[-1], timestamp=timestamp, source=source),
            self._liquidity_proxy(ordered[-1], timestamp=timestamp, source=source),
            self._trend_persistence(closes, timestamp=timestamp, source=source),
        )
        quality = _timeframe_quality(features)
        return FeatureSet(
            instrument=instrument,
            timeframe=timeframe,
            as_of=as_of,
            features=features,
            quality=quality,
            source=self._engine_version,
        )

    def _return(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        if len(closes) < 2:
            return _insufficient(FeatureName.RETURN, timestamp=timestamp, source=source)
        value = _pct_change(closes[-1], closes[0])
        return _feature(
            FeatureName.RETURN, value, _normalize_ratio(value), timestamp, len(closes), source
        )

    def _momentum(
        self,
        name: FeatureName,
        closes: tuple[Decimal, ...],
        *,
        lookback: int,
        timestamp: datetime,
        source: str,
    ) -> FeatureValue:
        if len(closes) <= lookback:
            return _insufficient(name, timestamp=timestamp, lookback=lookback, source=source)
        value = _pct_change(closes[-1], closes[-1 - lookback])
        return _feature(name, value, _normalize_ratio(value), timestamp, lookback, source)

    def _sma(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 20
        if len(closes) < lookback:
            return _insufficient(
                FeatureName.SMA, timestamp=timestamp, lookback=lookback, source=source
            )
        value = _mean(closes[-lookback:])
        return _feature(
            FeatureName.SMA,
            value,
            _normalize_ratio(_pct_change(closes[-1], value)),
            timestamp,
            lookback,
            source,
        )

    def _ema_feature(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 20
        if len(closes) < lookback:
            return _insufficient(
                FeatureName.EMA, timestamp=timestamp, lookback=lookback, source=source
            )
        value = _ema(closes[-lookback:], lookback)
        return _feature(
            FeatureName.EMA,
            value,
            _normalize_ratio(_pct_change(closes[-1], value)),
            timestamp,
            lookback,
            source,
        )

    def _ma_slope(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 20
        if len(closes) < lookback + 5:
            return _insufficient(
                FeatureName.MOVING_AVERAGE_SLOPE,
                timestamp=timestamp,
                lookback=lookback + 5,
                source=source,
            )
        latest = _mean(closes[-lookback:])
        earlier = _mean(closes[-lookback - 5 : -5])
        value = _pct_change(latest, earlier)
        return _feature(
            FeatureName.MOVING_AVERAGE_SLOPE,
            value,
            _normalize_ratio(value),
            timestamp,
            lookback + 5,
            source,
        )

    def _price_vs_ma(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 20
        if len(closes) < lookback:
            return _insufficient(
                FeatureName.PRICE_VS_MOVING_AVERAGE,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        value = _pct_change(closes[-1], _mean(closes[-lookback:]))
        return _feature(
            FeatureName.PRICE_VS_MOVING_AVERAGE,
            value,
            _normalize_ratio(value),
            timestamp,
            lookback,
            source,
        )

    def _rsi(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 14
        if len(closes) <= lookback:
            return _insufficient(
                FeatureName.RSI, timestamp=timestamp, lookback=lookback, source=source
            )
        deltas = tuple(closes[index] - closes[index - 1] for index in range(1, len(closes)))
        recent = deltas[-lookback:]
        gains = tuple(max(delta, _ZERO) for delta in recent)
        losses = tuple(abs(min(delta, _ZERO)) for delta in recent)
        avg_gain = _mean(gains)
        avg_loss = _mean(losses)
        if avg_loss == 0:
            value = Decimal("100")
        else:
            rs = avg_gain / avg_loss
            value = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
        normalized = ((value - Decimal("50")) / Decimal("50")).max(Decimal("-1")).min(Decimal("1"))
        return _feature(FeatureName.RSI, value, normalized, timestamp, lookback, source)

    def _macd(
        self,
        closes: tuple[Decimal, ...],
        name: FeatureName,
        *,
        timestamp: datetime,
        source: str,
    ) -> FeatureValue:
        if len(closes) < 35:
            return _insufficient(name, timestamp=timestamp, lookback=35, source=source)
        macd_line_values: list[Decimal] = []
        for end_index in range(26, len(closes) + 1):
            window = closes[:end_index]
            macd_line_values.append(_ema(window[-12:], 12) - _ema(window[-26:], 26))
        macd = macd_line_values[-1]
        signal = _ema(tuple(macd_line_values[-9:]), 9)
        histogram = macd - signal
        value = {
            FeatureName.MACD: macd,
            FeatureName.MACD_SIGNAL: signal,
            FeatureName.MACD_HISTOGRAM: histogram,
        }[name]
        return _feature(name, value, _normalize_ratio(value / closes[-1]), timestamp, 35, source)

    def _atr(
        self, bars: tuple[MarketBar, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 14
        if len(bars) <= lookback:
            return _insufficient(
                FeatureName.ATR, timestamp=timestamp, lookback=lookback, source=source
            )
        true_ranges: list[Decimal] = []
        for previous, current in zip(bars[-lookback - 1 : -1], bars[-lookback:], strict=True):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        value = _mean(tuple(true_ranges))
        normalized = _normalize_ratio(value / bars[-1].close)
        return _feature(FeatureName.ATR, value, normalized, timestamp, lookback, source)

    def _realized_volatility(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        returns = _returns(closes)
        lookback = 20
        if len(returns) < lookback:
            return _insufficient(
                FeatureName.REALIZED_VOLATILITY,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        value = _stddev(returns[-lookback:])
        return _feature(
            FeatureName.REALIZED_VOLATILITY,
            value,
            _normalize_ratio(value),
            timestamp,
            lookback,
            source,
        )

    def _rolling_stddev(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 20
        if len(closes) < lookback:
            return _insufficient(
                FeatureName.ROLLING_STANDARD_DEVIATION,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        value = _stddev(closes[-lookback:])
        return _feature(
            FeatureName.ROLLING_STANDARD_DEVIATION,
            value,
            _normalize_ratio(value / closes[-1]),
            timestamp,
            lookback,
            source,
        )

    def _drawdown(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        if len(closes) < 2:
            return _insufficient(FeatureName.DRAWDOWN, timestamp=timestamp, source=source)
        peak = max(closes)
        value = (peak - closes[-1]) / peak if peak > 0 else _ZERO
        return _feature(
            FeatureName.DRAWDOWN, value, -_normalize_ratio(value), timestamp, len(closes), source
        )

    def _distance_from_high(
        self,
        closes: tuple[Decimal, ...],
        highs: tuple[Decimal, ...],
        *,
        timestamp: datetime,
        source: str,
    ) -> FeatureValue:
        lookback = 20
        if len(highs) < lookback:
            return _insufficient(
                FeatureName.DISTANCE_FROM_RECENT_HIGH,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        high = max(highs[-lookback:])
        value = _pct_change(closes[-1], high)
        return _feature(
            FeatureName.DISTANCE_FROM_RECENT_HIGH,
            value,
            _normalize_ratio(value),
            timestamp,
            lookback,
            source,
        )

    def _distance_from_low(
        self,
        closes: tuple[Decimal, ...],
        lows: tuple[Decimal, ...],
        *,
        timestamp: datetime,
        source: str,
    ) -> FeatureValue:
        lookback = 20
        if len(lows) < lookback:
            return _insufficient(
                FeatureName.DISTANCE_FROM_RECENT_LOW,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        low = min(lows[-lookback:])
        value = _pct_change(closes[-1], low)
        return _feature(
            FeatureName.DISTANCE_FROM_RECENT_LOW,
            value,
            _normalize_ratio(value),
            timestamp,
            lookback,
            source,
        )

    def _breakout_strength(
        self,
        closes: tuple[Decimal, ...],
        highs: tuple[Decimal, ...],
        *,
        timestamp: datetime,
        source: str,
    ) -> FeatureValue:
        lookback = 20
        if len(highs) <= lookback:
            return _insufficient(
                FeatureName.BREAKOUT_STRENGTH,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        prior_high = max(highs[-lookback - 1 : -1])
        value = _pct_change(closes[-1], prior_high)
        normalized = _normalize_ratio(max(value, _ZERO))
        return _feature(
            FeatureName.BREAKOUT_STRENGTH, value, normalized, timestamp, lookback, source
        )

    def _range_position(
        self,
        closes: tuple[Decimal, ...],
        highs: tuple[Decimal, ...],
        lows: tuple[Decimal, ...],
        *,
        timestamp: datetime,
        source: str,
    ) -> FeatureValue:
        lookback = 20
        if len(highs) < lookback or len(lows) < lookback:
            return _insufficient(
                FeatureName.RANGE_POSITION,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        high = max(highs[-lookback:])
        low = min(lows[-lookback:])
        if high == low:
            return _feature(
                FeatureName.RANGE_POSITION, Decimal("0.50"), _ZERO, timestamp, lookback, source
            )
        value = (closes[-1] - low) / (high - low)
        normalized = ((value - Decimal("0.50")) * Decimal("2")).max(Decimal("-1")).min(Decimal("1"))
        return _feature(FeatureName.RANGE_POSITION, value, normalized, timestamp, lookback, source)

    def _mean_reversion_z_score(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 20
        if len(closes) < lookback:
            return _insufficient(
                FeatureName.MEAN_REVERSION_Z_SCORE,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        window = closes[-lookback:]
        standard_deviation = _stddev(window)
        if standard_deviation == 0:
            value = _ZERO
        else:
            value = (closes[-1] - _mean(window)) / standard_deviation
        return _feature(
            FeatureName.MEAN_REVERSION_Z_SCORE,
            value,
            _normalize_z(value),
            timestamp,
            lookback,
            source,
        )

    def _volume_change(
        self, volumes: tuple[Decimal | None, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 5
        if len(volumes) <= lookback or any(value is None for value in volumes[-lookback - 1 :]):
            return _insufficient(
                FeatureName.VOLUME_CHANGE,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        numeric = tuple(value for value in volumes if value is not None)
        value = (
            _pct_change(numeric[-1], numeric[-1 - lookback])
            if numeric[-1 - lookback] > 0
            else _ZERO
        )
        return _feature(
            FeatureName.VOLUME_CHANGE, value, _normalize_ratio(value), timestamp, lookback, source
        )

    def _relative_volume(
        self, volumes: tuple[Decimal | None, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        lookback = 20
        if len(volumes) < lookback or any(value is None for value in volumes[-lookback:]):
            return _insufficient(
                FeatureName.RELATIVE_VOLUME,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        numeric = tuple(value for value in volumes if value is not None)
        average = _mean(numeric[-lookback:])
        value = numeric[-1] / average if average > 0 else _ZERO
        return _feature(
            FeatureName.RELATIVE_VOLUME,
            value,
            _normalize_ratio(value - _ONE),
            timestamp,
            lookback,
            source,
        )

    def _spread_feature(self, bar: MarketBar, *, timestamp: datetime, source: str) -> FeatureValue:
        if bar.instrument.bid is None or bar.instrument.ask is None:
            return _insufficient(FeatureName.SPREAD, timestamp=timestamp, source=source)
        mid = (bar.instrument.bid + bar.instrument.ask) / Decimal("2")
        if mid <= 0:
            return _insufficient(FeatureName.SPREAD, timestamp=timestamp, source=source)
        value = (bar.instrument.ask - bar.instrument.bid) / mid
        return _feature(FeatureName.SPREAD, value, -_normalize_ratio(value), timestamp, 1, source)

    def _liquidity_proxy(self, bar: MarketBar, *, timestamp: datetime, source: str) -> FeatureValue:
        if bar.volume is None:
            return _insufficient(FeatureName.LIQUIDITY_PROXY, timestamp=timestamp, source=source)
        value = bar.volume * bar.close
        normalized = min(value / Decimal("1000000"), Decimal("1"))
        return _feature(FeatureName.LIQUIDITY_PROXY, value, normalized, timestamp, 1, source)

    def _trend_persistence(
        self, closes: tuple[Decimal, ...], *, timestamp: datetime, source: str
    ) -> FeatureValue:
        returns = _returns(closes)
        lookback = 20
        if len(returns) < lookback:
            return _insufficient(
                FeatureName.TREND_PERSISTENCE,
                timestamp=timestamp,
                lookback=lookback,
                source=source,
            )
        recent = returns[-lookback:]
        positive = sum(1 for value in recent if value > 0)
        value = Decimal(positive) / Decimal(lookback)
        normalized = ((value - Decimal("0.50")) * Decimal("2")).max(Decimal("-1")).min(Decimal("1"))
        return _feature(
            FeatureName.TREND_PERSISTENCE, value, normalized, timestamp, lookback, source
        )


def _feature(
    name: FeatureName,
    value: Decimal,
    normalized: Decimal,
    timestamp: datetime,
    lookback: int,
    source: str,
) -> FeatureValue:
    return FeatureValue(
        name=name,
        value=value,
        normalized_value=normalized.max(Decimal("-1")).min(Decimal("1")),
        timestamp=timestamp,
        lookback=lookback,
        quality=FeatureQuality.GOOD,
        source=source,
    )


def _insufficient(
    name: FeatureName, *, timestamp: datetime, source: str, lookback: int | None = None
) -> FeatureValue:
    return FeatureValue(
        name=name,
        value=None,
        normalized_value=None,
        timestamp=timestamp,
        lookback=lookback,
        quality=FeatureQuality.DATA_INSUFFICIENT,
        source=source,
    )


def _pct_change(current: Decimal, previous: Decimal) -> Decimal:
    if previous <= 0:
        return _ZERO
    return (current - previous) / previous


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return _ZERO
    return sum(values, _ZERO) / Decimal(len(values))


def _returns(closes: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    if len(closes) < 2:
        return ()
    return tuple(_pct_change(closes[index], closes[index - 1]) for index in range(1, len(closes)))


def _stddev(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        return _ZERO
    mean = _mean(values)
    variance = sum(((value - mean) ** 2 for value in values), _ZERO) / Decimal(len(values) - 1)
    return Decimal(str(math.sqrt(float(variance))))


def _ema(values: tuple[Decimal, ...], period: int) -> Decimal:
    if not values:
        return _ZERO
    multiplier = Decimal("2") / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = (value - result) * multiplier + result
    return result


def _normalize_ratio(value: Decimal) -> Decimal:
    return (value * Decimal("10")).max(Decimal("-1")).min(Decimal("1"))


def _normalize_z(value: Decimal) -> Decimal:
    return (value / Decimal("3")).max(Decimal("-1")).min(Decimal("1"))


def _aggregate_quality(qualities: tuple[FeatureQuality, ...], has_missing: bool) -> FeatureQuality:
    if not qualities:
        return FeatureQuality.DATA_INSUFFICIENT
    if all(quality is FeatureQuality.GOOD for quality in qualities) and not has_missing:
        return FeatureQuality.GOOD
    if any(quality in {FeatureQuality.GOOD, FeatureQuality.PARTIAL} for quality in qualities):
        return FeatureQuality.PARTIAL
    return FeatureQuality.DATA_INSUFFICIENT


def _timeframe_quality(features: tuple[FeatureValue, ...]) -> FeatureQuality:
    if not features:
        return FeatureQuality.DATA_INSUFFICIENT
    core_features = tuple(feature for feature in features if feature.name in _CORE_FEATURES)
    if not core_features:
        return FeatureQuality.DATA_INSUFFICIENT
    if any(feature.quality is not FeatureQuality.GOOD for feature in core_features):
        return FeatureQuality.DATA_INSUFFICIENT
    if all(feature.quality is FeatureQuality.GOOD for feature in features):
        return FeatureQuality.GOOD
    return FeatureQuality.PARTIAL
