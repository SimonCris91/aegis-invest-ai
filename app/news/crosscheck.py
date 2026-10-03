"""Read-only multi-source news corroboration."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from time import monotonic

from app.news.intelligence import (
    GlobalNewsProvider,
    NewsProviderError,
    NewsProviderStatus,
    RawNewsItem,
)


class CrossCheckedNewsProvider:
    """Combine independent feeds while preserving each source's status."""

    def __init__(
        self,
        primary: GlobalNewsProvider,
        secondary: GlobalNewsProvider,
        *additional: GlobalNewsProvider,
    ) -> None:
        self._providers = (primary, secondary, *additional)
        self._provider_name = "_PLUS_".join(
            _provider_label(provider) for provider in self._providers
        )
        self.read_calls = 0
        self._last_status = NewsProviderStatus.PROVIDER_UNAVAILABLE
        self._last_diagnostics: dict[str, object] = {}
        self._rate_limit_until: dict[str, float] = {}

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def last_status(self) -> NewsProviderStatus:
        return self._last_status

    @property
    def last_diagnostics(self) -> dict[str, object]:
        return dict(self._last_diagnostics)

    def set_tickers(self, tickers: tuple[str, ...]) -> None:
        for provider in self._providers:
            setter = getattr(provider, "set_tickers", None)
            if callable(setter):
                setter(tickers)

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        self.read_calls += 1
        results: dict[str, tuple[RawNewsItem, ...]] = {}
        errors: dict[str, NewsProviderError] = {}
        suppressed: set[str] = set()
        for provider in self._providers:
            name = str(getattr(provider, "provider_name", type(provider).__name__))
            now = monotonic()
            retry_at = self._rate_limit_until.get(name, 0.0)
            if now < retry_at:
                suppressed.add(name)
                errors[name] = NewsProviderError(
                    "news provider is in rate-limit cooldown",
                    status=NewsProviderStatus.RATE_LIMITED,
                    retry_after=str(max(1, int(retry_at - now))),
                )
                continue
            self._rate_limit_until.pop(name, None)
            try:
                results[name] = tuple(provider.fetch_global_news(as_of=as_of))
            except NewsProviderError as exc:
                errors[name] = exc
                if exc.status is NewsProviderStatus.RATE_LIMITED:
                    try:
                        retry_after = int(exc.retry_after or "")
                    except ValueError:
                        retry_after = 300
                    # Bound provider-supplied cooldowns; a malformed header
                    # cannot disable a source indefinitely.
                    self._rate_limit_until[name] = monotonic() + min(
                        900, max(30, retry_after)
                    )

        source_statuses = {
            name: (
                errors[name].status.value
                if name in errors
                else getattr(getattr(provider, "last_status", None), "value", "AVAILABLE")
            )
            for provider in self._providers
            if (name := str(getattr(provider, "provider_name", type(provider).__name__)))
        }

        if not results:
            error = next(iter(errors.values()), NewsProviderError(
                "all news providers are unavailable",
                status=NewsProviderStatus.PROVIDER_UNAVAILABLE,
            ))
            self._last_status = error.status
            self._last_diagnostics = self._diagnostics(
                results, errors, source_statuses=source_statuses, suppressed=suppressed
            )
            raise error

        merged = tuple(item for items in results.values() for item in items)
        corroborated, conflicts = _cross_source_comparison(results.values())
        has_degraded_source = bool(errors) or any(
            status not in {NewsProviderStatus.AVAILABLE.value}
            for status in source_statuses.values()
        )
        self._last_status = (
            NewsProviderStatus.PARTIAL if has_degraded_source else NewsProviderStatus.AVAILABLE
        )
        self._last_diagnostics = self._diagnostics(
            results,
            errors,
            source_statuses=source_statuses,
            corroborated=corroborated,
            conflicts=conflicts,
            suppressed=suppressed,
        )
        return merged

    def _diagnostics(
        self,
        results: dict[str, tuple[RawNewsItem, ...]],
        errors: dict[str, NewsProviderError],
        *,
        source_statuses: dict[str, str],
        corroborated: int = 0,
        conflicts: int = 0,
        suppressed: set[str] | None = None,
    ) -> dict[str, object]:
        statuses = {
            name: "ERROR:" + errors[name].status.value if name in errors else status
            for name, status in source_statuses.items()
        }
        return {
            "status": self._last_status.value,
            "provider_statuses": statuses,
            "articles_by_provider": {name: len(items) for name, items in results.items()},
            "corroborated_event_groups": corroborated,
            "conflicting_event_groups": conflicts,
            "suppressed_provider_count": len(suppressed or ()),
            "suppressed_provider_names": tuple(sorted(suppressed or ())),
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
    corroborated = 0
    conflicts = 0
    for left_index, left in enumerate(sets[:-1]):
        for right in sets[left_index + 1 :]:
            for first in left:
                matches = [second for second in right if _same_event(first, second)]
                if not matches:
                    continue
                corroborated += 1
                if any(
                    _sentiment_polarity(first) * _sentiment_polarity(second) < 0
                    for second in matches
                ):
                    conflicts += 1
    return corroborated, conflicts


def _provider_label(provider: GlobalNewsProvider) -> str:
    name = str(getattr(provider, "provider_name", type(provider).__name__))
    return {"ALPACA_NEWS": "ALPACA", "GDELT_DOC": "GDELT"}.get(name, name)


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
