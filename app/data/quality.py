"""Freshness, quality, and multi-provider consistency checks."""

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.data.models import (
    FreshnessStatus,
    HistoricalDataQualityReport,
    HistoricalDataQualityStatus,
)
from app.domain.enums import AssetClass, Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame


class FreshnessPolicy:
    def classify(
        self, *, timeframe: TimeFrame, last_timestamp: datetime | None, as_of: datetime
    ) -> FreshnessStatus:
        if last_timestamp is None:
            return FreshnessStatus.UNKNOWN
        if last_timestamp > as_of:
            return FreshnessStatus.UNKNOWN
        age = as_of - last_timestamp
        fresh, acceptable, stale = _thresholds(timeframe)
        if age <= fresh:
            return FreshnessStatus.FRESH
        if age <= acceptable:
            return FreshnessStatus.ACCEPTABLE
        if age <= stale:
            return FreshnessStatus.STALE
        return FreshnessStatus.EXPIRED


class HistoricalDataQualityAnalyzer:
    def __init__(
        self,
        *,
        freshness_policy: FreshnessPolicy | None = None,
        minimum_bars: int = 30,
        anomaly_threshold: Decimal = Decimal("0.75"),
    ) -> None:
        if minimum_bars <= 0:
            raise ValueError("minimum_bars must be positive")
        self._freshness = freshness_policy or FreshnessPolicy()
        self._minimum_bars = minimum_bars
        self._anomaly_threshold = anomaly_threshold

    def evaluate(
        self,
        *,
        provider: str,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        bars: tuple[MarketBar, ...],
        as_of: datetime,
        expected_currency: Currency | None = None,
    ) -> HistoricalDataQualityReport:
        reasons: list[str] = []
        timestamps = [bar.timestamp for bar in bars]
        duplicate_timestamps = len(timestamps) - len(set(timestamps))
        out_of_order = timestamps != sorted(timestamps)
        freshness = self._freshness.classify(
            timeframe=timeframe,
            last_timestamp=max(timestamps) if timestamps else None,
            as_of=as_of,
        )
        ohlc_inconsistencies = sum(1 for bar in bars if not _ohlc_consistent(bar))
        currency_mismatch = bool(
            expected_currency is not None and _currency_mismatch(bars, expected_currency)
        )
        anomalies = _extreme_anomalies(bars, threshold=self._anomaly_threshold)
        missing_estimate = _missing_bar_estimate(bars, timeframe, instrument=instrument)
        if len(bars) < self._minimum_bars:
            reasons.append("bar count is below minimum")
        if duplicate_timestamps:
            reasons.append("duplicate timestamps detected")
        if out_of_order:
            reasons.append("bars are out of chronological order")
        if ohlc_inconsistencies:
            reasons.append("OHLC inconsistencies detected")
        if currency_mismatch:
            reasons.append("bar currency does not match expected instrument currency")
        if anomalies:
            reasons.append("extreme price anomalies detected")
        if missing_estimate:
            reasons.append("time gaps detected")
        if freshness in {FreshnessStatus.STALE, FreshnessStatus.EXPIRED, FreshnessStatus.UNKNOWN}:
            reasons.append(f"freshness is {freshness.value}")
        status = _quality_status(
            bars=bars,
            minimum=self._minimum_bars,
            duplicate_timestamps=duplicate_timestamps,
            out_of_order=out_of_order,
            ohlc_inconsistencies=ohlc_inconsistencies,
            currency_mismatch=currency_mismatch,
            anomalies=anomalies,
            freshness=freshness,
        )
        return HistoricalDataQualityReport(
            provider=provider,
            instrument=instrument,
            timeframe=timeframe,
            status=status,
            freshness=freshness,
            bar_count=len(bars),
            expected_minimum_bars=self._minimum_bars,
            duplicate_timestamps=duplicate_timestamps,
            out_of_order=out_of_order,
            missing_bars_estimate=missing_estimate,
            ohlc_inconsistencies=ohlc_inconsistencies,
            currency_mismatch=currency_mismatch,
            extreme_anomalies=anomalies,
            provider_gaps=("time gaps detected",) if missing_estimate else (),
            reasons=tuple(reasons),
        )


class ProviderConsistencyChecker:
    def __init__(self, *, max_reference_price_disagreement: Decimal = Decimal("0.03")) -> None:
        self._threshold = max_reference_price_disagreement

    def compare(
        self, datasets: tuple[tuple[str, tuple[MarketBar, ...]], ...]
    ) -> HistoricalDataQualityStatus:
        latest_prices = tuple((provider, bars[-1].close) for provider, bars in datasets if bars)
        if len(latest_prices) < 2:
            return HistoricalDataQualityStatus.PARTIAL
        reference = latest_prices[0][1]
        if reference <= 0:
            return HistoricalDataQualityStatus.CONFLICTING
        for _, price in latest_prices[1:]:
            disagreement = abs(price - reference) / reference
            if disagreement > self._threshold:
                return HistoricalDataQualityStatus.CONFLICTING
        return HistoricalDataQualityStatus.GOOD


def _thresholds(timeframe: TimeFrame) -> tuple[timedelta, timedelta, timedelta]:
    return {
        TimeFrame.INTRADAY: (timedelta(minutes=5), timedelta(minutes=20), timedelta(hours=2)),
        TimeFrame.ONE_HOUR: (timedelta(hours=2), timedelta(hours=6), timedelta(hours=12)),
        TimeFrame.FOUR_HOUR: (timedelta(hours=6), timedelta(hours=18), timedelta(days=2)),
        TimeFrame.ONE_DAY: (timedelta(days=2), timedelta(days=4), timedelta(days=10)),
        TimeFrame.ONE_WEEK: (timedelta(days=10), timedelta(days=21), timedelta(days=45)),
    }[timeframe]


def _ohlc_consistent(bar: MarketBar) -> bool:
    return bar.low <= min(bar.open, bar.close) and bar.high >= max(bar.open, bar.close)


def _currency_mismatch(bars: tuple[MarketBar, ...], expected_currency: object) -> bool:
    return any(bar.currency is not expected_currency for bar in bars)


def _extreme_anomalies(bars: tuple[MarketBar, ...], *, threshold: Decimal) -> int:
    count = 0
    for previous, current in zip(bars, bars[1:], strict=False):
        if previous.close > 0 and abs(current.close - previous.close) / previous.close > threshold:
            count += 1
    return count


def _missing_bar_estimate(
    bars: tuple[MarketBar, ...], timeframe: TimeFrame, *, instrument: UniversalInstrument
) -> int:
    if len(bars) < 2:
        return 0
    expected = {
        TimeFrame.INTRADAY: timedelta(minutes=1),
        TimeFrame.ONE_HOUR: timedelta(hours=1),
        TimeFrame.FOUR_HOUR: timedelta(hours=4),
        TimeFrame.ONE_DAY: timedelta(days=1),
        TimeFrame.ONE_WEEK: timedelta(days=7),
    }[timeframe]
    missing = 0
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if _normal_market_calendar_gap(previous, current, timeframe, instrument.asset_class):
            continue
        gap = current.timestamp - previous.timestamp
        if gap > expected * 2:
            missing += max(1, int(gap / expected) - 1)
    return missing


def _normal_market_calendar_gap(
    previous: MarketBar,
    current: MarketBar,
    timeframe: TimeFrame,
    asset_class: AssetClass,
) -> bool:
    if asset_class not in {AssetClass.EQUITY, AssetClass.ETF}:
        return False
    if timeframe is TimeFrame.ONE_DAY:
        return _business_days_between(previous.timestamp.date(), current.timestamp.date()) == 0
    if timeframe is TimeFrame.FOUR_HOUR:
        return _business_days_between(previous.timestamp.date(), current.timestamp.date()) == 0
    return False


def _business_days_between(previous: date, current: date) -> int:
    day = previous + timedelta(days=1)
    count = 0
    while day < current:
        if not _is_us_market_closed_date(day):
            count += 1
        day += timedelta(days=1)
    return count


def _is_us_market_closed_date(day: date) -> bool:
    if day.weekday() >= 5:
        return True
    holidays = {
        date(day.year, 1, 1),
        date(day.year, 7, 4),
        date(day.year, 12, 25),
    }
    observed = set(holidays)
    for holiday in holidays:
        if holiday.weekday() == 5:
            observed.add(holiday - timedelta(days=1))
        elif holiday.weekday() == 6:
            observed.add(holiday + timedelta(days=1))
    return day in observed


def _quality_status(
    *,
    bars: tuple[MarketBar, ...],
    minimum: int,
    duplicate_timestamps: int,
    out_of_order: bool,
    ohlc_inconsistencies: int,
    currency_mismatch: bool,
    anomalies: int,
    freshness: FreshnessStatus,
) -> HistoricalDataQualityStatus:
    if not bars or len(bars) < max(2, minimum // 3):
        return HistoricalDataQualityStatus.INSUFFICIENT
    if currency_mismatch or ohlc_inconsistencies:
        return HistoricalDataQualityStatus.CONFLICTING
    if freshness in {FreshnessStatus.EXPIRED, FreshnessStatus.UNKNOWN}:
        return HistoricalDataQualityStatus.STALE
    if duplicate_timestamps or out_of_order or anomalies:
        return HistoricalDataQualityStatus.DEGRADED
    if len(bars) < minimum or freshness is FreshnessStatus.STALE:
        return HistoricalDataQualityStatus.PARTIAL
    return HistoricalDataQualityStatus.GOOD
