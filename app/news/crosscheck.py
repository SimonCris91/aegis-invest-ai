"""Read-only multi-source news corroboration."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from app.news.intelligence import (
    GlobalNewsProvider,
    NewsProviderError,
    NewsProviderStatus,
    RawNewsItem,
)


class CrossCheckedNewsProvider:
    """Combine two independent feeds while preserving each source's status."""

    provider_name = "ALPHA_VANTAGE_PLUS_ALPACA"

    def __init__(self, primary: GlobalNewsProvider, secondary: GlobalNewsProvider) -> None:
        self._primary = primary
        self._secondary = secondary
        self.read_calls = 0
        self._last_status = NewsProviderStatus.PROVIDER_UNAVAILABLE
        self._last_diagnostics: dict[str, object] = {}

    @property
    def last_status(self) -> NewsProviderStatus:
        return self._last_status

    @property
    def last_diagnostics(self) -> dict[str, object]:
        return dict(self._last_diagnostics)

    def set_tickers(self, tickers: tuple[str, ...]) -> None:
        for provider in (self._primary, self._secondary):
            setter = getattr(provider, "set_tickers", None)
            if callable(setter):
                setter(tickers)

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        self.read_calls += 1
        results: dict[str, tuple[RawNewsItem, ...]] = {}
        errors: dict[str, NewsProviderError] = {}
        for provider in (self._primary, self._secondary):
            name = str(getattr(provider, "provider_name", type(provider).__name__))
            try:
                results[name] = tuple(provider.fetch_global_news(as_of=as_of))
            except NewsProviderError as exc:
                errors[name] = exc

        if not results:
            error = next(iter(errors.values()), NewsProviderError(
                "all news providers are unavailable",
                status=NewsProviderStatus.PROVIDER_UNAVAILABLE,
            ))
            self._last_status = error.status
            self._last_diagnostics = self._diagnostics(results, errors)
            raise error

        merged = tuple(item for items in results.values() for item in items)
        corroborated, conflicts = _cross_source_comparison(results.values())
        self._last_status = (
            NewsProviderStatus.AVAILABLE
            if not errors
            else NewsProviderStatus.PARTIAL
        )
        self._last_diagnostics = self._diagnostics(
            results,
            errors,
            corroborated=corroborated,
            conflicts=conflicts,
        )
        return merged

    def _diagnostics(
        self,
        results: dict[str, tuple[RawNewsItem, ...]],
        errors: dict[str, NewsProviderError],
        *,
        corroborated: int = 0,
        conflicts: int = 0,
    ) -> dict[str, object]:
        statuses = {
            name: (
                "ERROR:" + error.status.value
                if (error := errors.get(name)) is not None
                else "AVAILABLE"
            )
            for name in {
                str(getattr(self._primary, "provider_name", "primary")),
                str(getattr(self._secondary, "provider_name", "secondary")),
            }
        }
        return {
            "status": self._last_status.value,
            "provider_statuses": statuses,
            "articles_by_provider": {name: len(items) for name, items in results.items()},
            "corroborated_event_groups": corroborated,
            "conflicting_event_groups": conflicts,
            "cross_source_status": (
                "CORROBORATED" if corroborated else "NO_CROSS_SOURCE_MATCH"
            ),
            "broker_write_calls": 0,
        }


def _cross_source_comparison(
    result_sets: Iterable[tuple[RawNewsItem, ...]],
) -> tuple[int, int]:
    sets = [tuple(items) for items in result_sets]
    if len(sets) < 2:
        return 0, 0
    left, right = sets[0], sets[1]
    corroborated = 0
    conflicts = 0
    for first in left:
        matches = [second for second in right if _same_event(first, second)]
        if not matches:
            continue
        corroborated += 1
        if any(_sentiment_polarity(first) * _sentiment_polarity(second) < 0 for second in matches):
            conflicts += 1
    return corroborated, conflicts


def _same_event(first: RawNewsItem, second: RawNewsItem) -> bool:
    if first.published_at.date() != second.published_at.date():
        return False
    left = _tokens(f"{first.headline} {first.summary or ''}")
    right = _tokens(f"{second.headline} {second.summary or ''}")
    if not left or not right:
        return False
    overlap = len(left & right) / max(1, len(left | right))
    return overlap >= 0.45


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{4,}", text.casefold())
        if token not in _STOPWORDS
    }


def _sentiment_polarity(item: RawNewsItem) -> int:
    text = f"{item.headline} {item.summary or ''}".casefold()
    positive = any(
        term in text for term in ("beats", "growth", "surge", "approval", "bullish", "easing")
    )
    negative = any(
        term in text
        for term in ("war", "sanction", "ban", "misses", "lawsuit", "bearish", "uncertainty")
    )
    return -1 if negative and not positive else 1 if positive and not negative else 0


_STOPWORDS = {"about", "after", "among", "from", "into", "that", "the", "this", "with"}
