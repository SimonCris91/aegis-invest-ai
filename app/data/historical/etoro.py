"""Official eToro read-only historical candles adapter."""

from datetime import datetime
from decimal import Decimal

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.data.models import DataProviderError, DataProviderStatus
from app.domain.enums import Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame

_ETORO_INTERVALS = {
    TimeFrame.INTRADAY: "OneMinute",
    TimeFrame.ONE_HOUR: "OneHour",
    TimeFrame.FOUR_HOUR: "FourHours",
    TimeFrame.ONE_DAY: "OneDay",
    TimeFrame.ONE_WEEK: "OneWeek",
}


class EtoroHistoricalMarketDataProvider:
    provider_name = "etoro"
    supported_timeframes = tuple(_ETORO_INTERVALS)

    def __init__(self, client: EtoroReadClient) -> None:
        self._client = client

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]:
        instrument_id = instrument.numeric_instrument_id
        if instrument_id is None:
            raise DataProviderError(
                "eToro candles require a numeric instrument id",
                status=DataProviderStatus.MAPPING_AMBIGUOUS,
            )
        try:
            raw = self._client.candle_history(
                instrument_id=instrument_id,
                direction="asc",
                interval=_ETORO_INTERVALS[timeframe],
                candles_count=min(limit, 1000),
            )
        except EtoroApiError as exc:
            status = (
                DataProviderStatus.RATE_LIMITED
                if exc.status == 429
                else DataProviderStatus.PROVIDER_UNAVAILABLE
            )
            raise DataProviderError("eToro candle history read failed", status=status) from exc
        bars = tuple(
            bar
            for bar in _normalize_etoro_candles(raw, instrument=instrument, timeframe=timeframe)
            if bar.timestamp <= as_of
        )
        return bars[-limit:]


def _normalize_etoro_candles(
    raw: object, *, instrument: UniversalInstrument, timeframe: TimeFrame
) -> tuple[MarketBar, ...]:
    if not isinstance(raw, dict):
        return ()
    groups = raw.get("candles")
    if not isinstance(groups, list):
        return ()
    bars: list[MarketBar] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        nested = group.get("candles")
        if not isinstance(nested, list):
            continue
        for item in nested:
            if not isinstance(item, dict):
                continue
            timestamp = item.get("fromDate")
            if not isinstance(timestamp, str):
                continue
            try:
                bars.append(
                    MarketBar(
                        instrument=instrument,
                        timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                        timeframe=timeframe,
                        open=Decimal(str(item["open"])),
                        high=Decimal(str(item["high"])),
                        low=Decimal(str(item["low"])),
                        close=Decimal(str(item["close"])),
                        volume=(
                            Decimal(str(item["volume"])) if item.get("volume") is not None else None
                        ),
                        currency=instrument.currency or Currency.USD,
                        source="etoro",
                    )
                )
            except (KeyError, ValueError):
                continue
    return tuple(sorted(bars, key=lambda bar: bar.timestamp))
