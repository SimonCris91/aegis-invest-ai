"""Provider registries with fallback, cache, and failure isolation."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.data.historical.cache import HistoricalDataCache
from app.data.mapping import InstrumentMappingService
from app.data.models import (
    DataProviderError,
    DataProviderStatus,
    EventRiskAssessment,
    HistoricalDataQualityStatus,
    HistoricalDataset,
    MultiProviderHistoricalResult,
    NewsProviderResult,
    ProviderProvenance,
)
from app.data.ports import EventRiskProvider, HistoricalProviderWithName, NewsProvider
from app.data.quality import HistoricalDataQualityAnalyzer, ProviderConsistencyChecker
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame


@dataclass(frozen=True, slots=True)
class HistoricalProviderEntry:
    provider: HistoricalProviderWithName
    priority: int


class HistoricalDataProviderRegistry:
    def __init__(
        self,
        providers: Iterable[HistoricalProviderEntry],
        *,
        cache: HistoricalDataCache | None = None,
        mapping_service: InstrumentMappingService | None = None,
        quality_analyzer: HistoricalDataQualityAnalyzer | None = None,
        consistency_checker: ProviderConsistencyChecker | None = None,
    ) -> None:
        self._providers = tuple(sorted(providers, key=lambda item: item.priority))
        self._cache = cache
        self._mapping_service = mapping_service or InstrumentMappingService()
        self._quality = quality_analyzer or HistoricalDataQualityAnalyzer()
        self._consistency = consistency_checker or ProviderConsistencyChecker()

    def fetch(
        self,
        *,
        instrument: UniversalInstrument,
        timeframes: tuple[TimeFrame, ...],
        as_of: datetime,
        limit: int,
    ) -> MultiProviderHistoricalResult:
        datasets: list[HistoricalDataset] = []
        bars_by_timeframe: dict[TimeFrame, tuple[MarketBar, ...]] = {}
        statuses: dict[str, DataProviderStatus] = {}
        reasons: list[str] = []
        for timeframe in timeframes:
            dataset = self._fetch_timeframe(
                instrument=instrument,
                timeframe=timeframe,
                as_of=as_of,
                limit=limit,
                statuses=statuses,
                reasons=reasons,
            )
            if dataset is not None:
                datasets.append(dataset)
                bars_by_timeframe[timeframe] = dataset.bars
        consistency = self._consistency.compare(
            tuple((dataset.provider, dataset.bars) for dataset in datasets)
        )
        status = (
            DataProviderStatus.SUCCESS
            if bars_by_timeframe
            else DataProviderStatus.DATA_INSUFFICIENT
        )
        if consistency is HistoricalDataQualityStatus.CONFLICTING:
            status = DataProviderStatus.DATA_CONFLICT
            reasons.append("provider reference prices conflict")
        return MultiProviderHistoricalResult(
            instrument=instrument,
            as_of=as_of,
            datasets=tuple(datasets),
            bars_by_timeframe=bars_by_timeframe,
            provider_statuses=statuses,
            status=status,
            consistency_status=consistency,
            reasons=tuple(reasons),
            broker_write_calls=0,
        )

    def _fetch_timeframe(
        self,
        *,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        as_of: datetime,
        limit: int,
        statuses: dict[str, DataProviderStatus],
        reasons: list[str],
    ) -> HistoricalDataset | None:
        for entry in self._providers:
            provider = entry.provider
            if timeframe not in provider.supported_timeframes:
                continue
            try:
                bars = provider.get_bars(instrument, timeframe, as_of=as_of, limit=limit)
            except DataProviderError as exc:
                statuses[provider.provider_name] = exc.status
                reasons.append(f"{provider.provider_name}:{exc.status.value}")
                continue
            if not bars:
                statuses[provider.provider_name] = DataProviderStatus.DATA_INSUFFICIENT
                continue
            quality = self._quality.evaluate(
                provider=provider.provider_name,
                instrument=instrument,
                timeframe=timeframe,
                bars=bars,
                as_of=as_of,
                expected_currency=instrument.currency,
            )
            if quality.status is HistoricalDataQualityStatus.CONFLICTING:
                statuses[provider.provider_name] = DataProviderStatus.DATA_CONFLICT
                reasons.extend(quality.reasons)
                continue
            statuses[provider.provider_name] = DataProviderStatus.SUCCESS
            mapping = self._mapping_service.resolve(instrument, provider=provider.provider_name)
            if self._cache is not None and mapping.selected is not None:
                self._cache.upsert_bars(
                    provider=provider.provider_name,
                    bars=bars,
                    fetched_at=as_of,
                    mapping=mapping.selected,
                )
            return HistoricalDataset(
                instrument=instrument,
                timeframe=timeframe,
                bars=bars,
                provider=provider.provider_name,
                fetched_at=as_of,
                quality=quality,
                provenance=ProviderProvenance(
                    provider=provider.provider_name,
                    fetched_at=as_of,
                    source="live-provider",
                    provider_symbol=mapping.selected.provider_symbol if mapping.selected else None,
                    cached=False,
                ),
            )
        cached = self._cache_dataset(
            instrument=instrument,
            timeframe=timeframe,
            as_of=as_of,
            limit=limit,
        )
        if cached is not None:
            statuses["cache"] = DataProviderStatus.CACHE_HIT
            return cached
        reasons.append(f"{timeframe.value}:DATA_INSUFFICIENT")
        return None

    def _cache_dataset(
        self,
        *,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        as_of: datetime,
        limit: int,
    ) -> HistoricalDataset | None:
        if self._cache is None:
            return None
        for entry in self._providers:
            bars = self._cache.get_bars(
                provider=entry.provider.provider_name,
                instrument_key=(instrument.broker, instrument.broker_instrument_id),
                timeframe=timeframe,
                as_of=as_of,
                limit=limit,
                instrument_factory=instrument.model_dump(mode="json"),
            )
            if not bars:
                continue
            quality = self._quality.evaluate(
                provider=entry.provider.provider_name,
                instrument=instrument,
                timeframe=timeframe,
                bars=bars,
                as_of=as_of,
                expected_currency=instrument.currency,
            )
            if quality.status in {
                HistoricalDataQualityStatus.GOOD,
                HistoricalDataQualityStatus.PARTIAL,
            }:
                return HistoricalDataset(
                    instrument=instrument,
                    timeframe=timeframe,
                    bars=bars,
                    provider=entry.provider.provider_name,
                    fetched_at=as_of,
                    quality=quality,
                    provenance=ProviderProvenance(
                        provider=entry.provider.provider_name,
                        fetched_at=as_of,
                        source="cache",
                        cached=True,
                    ),
                )
        return None


class NewsProviderRegistry:
    def __init__(self, providers: Iterable[NewsProvider]) -> None:
        self._providers = tuple(providers)

    def fetch(self, instrument: UniversalInstrument, *, as_of: datetime) -> NewsProviderResult:
        for provider in self._providers:
            result = provider.fetch_news(instrument, as_of=as_of)
            if result.status in {DataProviderStatus.SUCCESS, DataProviderStatus.CACHE_HIT}:
                return result
        return NewsProviderResult(
            provider="none",
            instrument=instrument,
            as_of=as_of,
            items=(),
            status=DataProviderStatus.DATA_INSUFFICIENT,
            quality=HistoricalDataQualityStatus.INSUFFICIENT,
            reasons=("NEWS_UNAVAILABLE",),
        )


class EventRiskProviderRegistry:
    def __init__(self, providers: Iterable[EventRiskProvider]) -> None:
        self._providers = tuple(providers)

    def assess(self, instrument: UniversalInstrument, *, as_of: datetime) -> EventRiskAssessment:
        for provider in self._providers:
            result = provider.assess_events(instrument, as_of=as_of)
            if result.status in {DataProviderStatus.SUCCESS, DataProviderStatus.CACHE_HIT}:
                return result
        return EventRiskAssessment(
            provider="none",
            instrument=instrument,
            as_of=as_of,
            status=DataProviderStatus.DATA_INSUFFICIENT,
            quality=HistoricalDataQualityStatus.INSUFFICIENT,
            reasons=("EVENT_RISK_UNAVAILABLE",),
        )
