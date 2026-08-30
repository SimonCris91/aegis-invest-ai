"""Provider-neutral news normalization with injection-resistant text handling."""

from datetime import datetime, timedelta
from decimal import Decimal

from app.agent.safety import sanitize_external_text
from app.data.models import (
    DataProviderStatus,
    EventCategory,
    HistoricalDataQualityStatus,
    NewsProviderResult,
    NormalizedNewsItem,
)
from app.domain.universe import UniversalInstrument


class NullNewsProvider:
    provider_name = "none"

    def fetch_news(self, instrument: UniversalInstrument, *, as_of: datetime) -> NewsProviderResult:
        return NewsProviderResult(
            provider=self.provider_name,
            instrument=instrument,
            as_of=as_of,
            items=(),
            status=DataProviderStatus.DATA_INSUFFICIENT,
            quality=HistoricalDataQualityStatus.INSUFFICIENT,
            reasons=("NEWS_NOT_CONFIGURED",),
        )


class InMemoryNewsProvider:
    provider_name = "memory-news"

    def __init__(self, items: tuple[NormalizedNewsItem, ...]) -> None:
        self._items = items

    def fetch_news(self, instrument: UniversalInstrument, *, as_of: datetime) -> NewsProviderResult:
        relevant = _dedupe(
            tuple(
                item.model_copy(
                    update={
                        "headline": sanitize_external_text(item.headline, maximum_length=280),
                        "source": sanitize_external_text(item.source, maximum_length=80),
                    }
                )
                for item in self._items
                if item.instrument.key == instrument.key
                and timedelta(0) <= as_of - item.published_at <= timedelta(days=7)
                and item.relevance >= Decimal("0.30")
            )
        )
        return NewsProviderResult(
            provider=self.provider_name,
            instrument=instrument,
            as_of=as_of,
            items=relevant,
            status=DataProviderStatus.SUCCESS if relevant else DataProviderStatus.DATA_INSUFFICIENT,
            quality=(
                HistoricalDataQualityStatus.PARTIAL
                if relevant
                else HistoricalDataQualityStatus.INSUFFICIENT
            ),
            reasons=() if relevant else ("no relevant fresh news",),
        )


def normalized_news_item(
    *,
    instrument: UniversalInstrument,
    headline: str,
    source: str,
    published_at: datetime,
    event_type: EventCategory = EventCategory.UNKNOWN,
    sentiment_score: Decimal | None = None,
    sentiment_confidence: Decimal | None = None,
    relevance: Decimal = Decimal("0.50"),
    confidence: Decimal = Decimal("0.50"),
    url_or_reference: str | None = None,
) -> NormalizedNewsItem:
    return NormalizedNewsItem(
        instrument=instrument,
        headline=sanitize_external_text(headline, maximum_length=280),
        source=sanitize_external_text(source, maximum_length=80),
        published_at=published_at,
        event_type=event_type,
        sentiment_score=sentiment_score,
        sentiment_confidence=sentiment_confidence,
        relevance=relevance,
        confidence=confidence,
        url_or_reference=url_or_reference,
        data_quality=HistoricalDataQualityStatus.PARTIAL,
    )


def _dedupe(items: tuple[NormalizedNewsItem, ...]) -> tuple[NormalizedNewsItem, ...]:
    seen: set[str] = set()
    unique: list[NormalizedNewsItem] = []
    for item in sorted(items, key=lambda value: value.published_at, reverse=True):
        if item.dedupe_key in seen:
            continue
        seen.add(item.dedupe_key)
        unique.append(item)
    return tuple(unique)
