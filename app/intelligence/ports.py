"""Broker-neutral ports for historical, news, and strategy intelligence."""

from datetime import datetime
from typing import Protocol

from app.domain.universe import UniversalInstrument
from app.intelligence.models import (
    MarketBar,
    MultiTimeframeFeatureSet,
    NewsSignal,
    StrategyEvaluationContext,
    StrategySignal,
    TimeFrame,
)


class HistoricalMarketDataProvider(Protocol):
    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]: ...


class NewsSignalProvider(Protocol):
    def news_signal(self, instrument: UniversalInstrument, *, as_of: datetime) -> NewsSignal: ...


class StrategySignalProvider(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def strategy_version(self) -> str: ...

    def evaluate(self, context: StrategyEvaluationContext) -> StrategySignal: ...


class FeatureProvider(Protocol):
    def analyze(
        self,
        instrument: UniversalInstrument,
        *,
        bars_by_timeframe: dict[TimeFrame, tuple[MarketBar, ...]],
        as_of: datetime,
    ) -> MultiTimeframeFeatureSet: ...
