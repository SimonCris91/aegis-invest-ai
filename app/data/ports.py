"""Provider-neutral ports used by the live data layer."""

from datetime import datetime
from typing import Protocol

from app.data.models import EventRiskAssessment, NewsProviderResult
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame
from app.intelligence.ports import HistoricalMarketDataProvider


class NewsProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def fetch_news(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> NewsProviderResult: ...


class EventRiskProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def assess_events(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> EventRiskAssessment: ...


class MarketMetadataProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def supports(self, instrument: UniversalInstrument) -> bool: ...


class HistoricalProviderWithName(HistoricalMarketDataProvider, Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def supported_timeframes(self) -> tuple[TimeFrame, ...]: ...

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]: ...
