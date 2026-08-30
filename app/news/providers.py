"""Normalized offline news providers with duplicate and freshness controls."""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError

from app.domain.market import NewsItem


class NewsDataError(RuntimeError):
    """Base error for normalized news failures."""


class NewsUnavailableError(NewsDataError):
    pass


class UnsupportedNewsSymbolError(NewsDataError):
    pass


class StaleNewsError(NewsDataError):
    pass


class MalformedNewsError(NewsDataError):
    pass


class FakeNewsProvider:
    def __init__(
        self,
        *,
        items: tuple[NewsItem, ...],
        max_age_seconds: int = 86_400,
        unavailable: bool = False,
    ) -> None:
        self._items = items
        self._max_age = timedelta(seconds=max_age_seconds)
        self._unavailable = unavailable

    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]:
        if self._unavailable:
            raise NewsUnavailableError("news provider is unavailable")
        requested = {symbol.casefold() for symbol in symbols}
        if not requested:
            raise UnsupportedNewsSymbolError("at least one news symbol is required")

        matching = tuple(
            item
            for item in self._items
            if requested.intersection(symbol.casefold() for symbol in item.asset_relevance)
        )
        if not matching:
            raise NewsUnavailableError("no relevant normalized news is available")
        if any(
            as_of - item.timestamp < timedelta(0) or as_of - item.timestamp > self._max_age
            for item in matching
        ):
            raise StaleNewsError("stale news data was returned")

        unique: list[NewsItem] = []
        seen: set[str] = set()
        for item in matching:
            if item.deduplication_key not in seen:
                seen.add(item.deduplication_key)
                unique.append(item)
        return tuple(unique)


class FixtureNewsProvider(FakeNewsProvider):
    """Normalizes caller-supplied records without network access."""

    def __init__(
        self,
        *,
        records: Iterable[Mapping[str, object]],
        max_age_seconds: int = 86_400,
    ) -> None:
        try:
            items = tuple(NewsItem.model_validate(record) for record in records)
        except (ValidationError, TypeError, ValueError) as exc:
            raise MalformedNewsError("malformed news fixture") from exc
        super().__init__(items=items, max_age_seconds=max_age_seconds)
