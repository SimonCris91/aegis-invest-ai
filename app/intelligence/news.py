"""Optional news and event-risk normalization."""

from datetime import datetime
from decimal import Decimal

from app.domain.market import NewsItem
from app.domain.universe import UniversalInstrument
from app.intelligence.models import NewsSignal, NewsSignalStatus


class NewsSignalBuilder:
    def not_configured(self, instrument: UniversalInstrument, *, as_of: datetime) -> NewsSignal:
        return NewsSignal(
            instrument=instrument,
            timestamp=as_of,
            status=NewsSignalStatus.NEWS_NOT_CONFIGURED,
            sentiment=None,
            impact=None,
            confidence=Decimal("0"),
            source_quality=Decimal("0"),
            event_risks=(),
        )

    def from_items(
        self, instrument: UniversalInstrument, *, items: tuple[NewsItem, ...], as_of: datetime
    ) -> NewsSignal:
        relevant = tuple(
            item
            for item in items
            if instrument.symbol.casefold()
            in {symbol.casefold() for symbol in item.asset_relevance}
        )
        if not relevant:
            return NewsSignal(
                instrument=instrument,
                timestamp=as_of,
                status=NewsSignalStatus.DATA_INSUFFICIENT,
                sentiment=None,
                impact=None,
                confidence=Decimal("0.10"),
                source_quality=Decimal("0.10"),
                event_risks=(),
            )
        sentiment = sum((item.sentiment for item in relevant), Decimal("0")) / Decimal(
            len(relevant)
        )
        impact = max(item.importance for item in relevant)
        confidence = min(
            Decimal("1"),
            sum((item.confidence for item in relevant), Decimal("0")) / Decimal(len(relevant)),
        )
        source_quality = min(
            Decimal("1"), Decimal(len({item.source for item in relevant})) / Decimal("3")
        )
        return NewsSignal(
            instrument=instrument,
            timestamp=as_of,
            status=NewsSignalStatus.AVAILABLE,
            sentiment=sentiment,
            impact=impact,
            confidence=confidence,
            source_quality=source_quality,
            event_risks=(),
        )
