"""Offline historical market data providers for tests and local research."""

from datetime import datetime

from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame
from app.intelligence.ports import HistoricalMarketDataProvider


class InMemoryHistoricalMarketDataProvider(HistoricalMarketDataProvider):
    def __init__(self, bars_by_key: dict[tuple[str, TimeFrame], tuple[MarketBar, ...]]) -> None:
        self._bars_by_key = bars_by_key

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]:
        bars = self._bars_by_key.get((instrument.key, timeframe), ())
        return tuple(bar for bar in bars if bar.timestamp <= as_of)[-limit:]
