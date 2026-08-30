"""Configurable news source boundary; no provider is selected in this phase."""

from datetime import datetime
from typing import Protocol

from app.domain.market import NewsItem


class NewsService(Protocol):
    def relevant_news(
        self, symbols: tuple[str, ...], *, as_of: datetime
    ) -> tuple[NewsItem, ...]: ...

    def data_is_available(self, *, as_of: datetime) -> bool: ...


class NewsProvider(Protocol):
    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]: ...
