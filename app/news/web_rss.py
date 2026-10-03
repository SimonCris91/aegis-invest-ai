"""Bounded, read-only web news search through Google News RSS.

This adapter is deliberately separate from broker and execution code.  It is
used only to obtain candidate-specific evidence when the configured APIs are
unavailable or do not cover an international listing (for example a Tokyo
``.T`` instrument).  The existing news linker and RiskManager still decide
whether the evidence is usable.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

from app.data.historical.providers import HttpTextTransport, UrllibTextTransport, _sanitize_url
from app.data.models import DataProviderError, DataProviderStatus
from app.news.intelligence import (
    NEWS_FRESH_MAX_AGE,
    NewsProviderError,
    NewsProviderStatus,
    NewsSourceQuality,
    RawNewsItem,
)

WEB_NEWS_RSS_PROVIDER = "GOOGLE_NEWS_RSS"
BING_NEWS_RSS_PROVIDER = "BING_NEWS_RSS"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
BING_NEWS_RSS_URL = "https://www.bing.com/news/search"
WEB_NEWS_CACHE_TTL = timedelta(minutes=15)
WEB_NEWS_CACHE_RETENTION = NEWS_FRESH_MAX_AGE
WEB_NEWS_MAX_RECORDS = 20
_CACHE_LOCK = Lock()


class GoogleNewsRssProvider:
    """Fetch one bounded candidate-specific feed without credentials."""

    provider_name = WEB_NEWS_RSS_PROVIDER

    def __init__(
        self,
        *,
        query: str,
        candidate_symbol: str,
        transport: HttpTextTransport | None = None,
        base_url: str = GOOGLE_NEWS_RSS_URL,
        locale: str = "en-US",
        region: str = "US",
        limit: int = WEB_NEWS_MAX_RECORDS,
        cache_ttl: timedelta = WEB_NEWS_CACHE_TTL,
        cache_retention: timedelta = WEB_NEWS_CACHE_RETENTION,
        cache_path: Path | None = None,
    ) -> None:
        self._query = query.strip()
        self._candidate_symbol = candidate_symbol.strip()
        self._transport = transport or UrllibTextTransport()
        self._base_url = base_url.rstrip("?")
        self._locale = locale.strip() or "en-US"
        self._region = region.strip().upper() or "US"
        self._limit = min(max(1, limit), WEB_NEWS_MAX_RECORDS)
        self._cache_ttl = cache_ttl
        self._cache_retention = max(cache_ttl, cache_retention)
        self._cache_path = cache_path
        self._cached_items: tuple[RawNewsItem, ...] | None = None
        self._cached_at: datetime | None = None
        self.read_calls = 0
        self._last_status = NewsProviderStatus.PROVIDER_UNAVAILABLE
        self._last_diagnostics: dict[str, object] = {}
        self._load_cache()

    @property
    def last_status(self) -> NewsProviderStatus:
        return self._last_status

    @property
    def last_diagnostics(self) -> dict[str, object]:
        return dict(self._last_diagnostics)

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        if not self._query or not self._candidate_symbol:
            error = NewsProviderError(
                "web news query is incomplete",
                status=NewsProviderStatus.MALFORMED_RESPONSE,
                sanitized_endpoint=self._sanitized_url(),
                provider_error_message="missing query or candidate symbol",
            )
            self._record_error(error)
            raise error

        cached = self._fresh_cache(as_of=as_of)
        if cached is not None:
            self._last_status = (
                NewsProviderStatus.AVAILABLE if cached else NewsProviderStatus.DELAYED
            )
            self._last_diagnostics = {
                "status": self._last_status.value,
                "articles_returned": len(cached),
                "cache_hit": True,
                "locale": self._locale,
                "region": self._region,
                "candidate_symbol": self._candidate_symbol,
                "broker_write_calls": 0,
            }
            return cached

        self.read_calls += 1
        url = self._url()
        try:
            text = self._transport.get_text(
                url,
                {
                    "User-Agent": "AegisInvestAI/0.7 (read-only news)",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                },
            )
            items = self._parse_feed(text, as_of=as_of)
        except DataProviderError as exc:
            error = NewsProviderError(
                "web news provider read failed",
                status=_status_from_data_error(exc),
                http_status=exc.http_status,
                sanitized_endpoint=exc.sanitized_endpoint or self._sanitized_url(),
                provider_error_message=exc.provider_error_message or exc.provider_error_code,
                retry_after=exc.retry_after,
            )
            self._record_error(error)
            raise error from exc
        except (ElementTree.ParseError, ValueError, TypeError) as exc:
            error = NewsProviderError(
                "web news RSS response is malformed",
                status=NewsProviderStatus.MALFORMED_RESPONSE,
                sanitized_endpoint=self._sanitized_url(),
                provider_error_message=type(exc).__name__,
            )
            self._record_error(error)
            raise error from exc
        except (OSError, TimeoutError) as exc:
            error = NewsProviderError(
                "web news provider is unavailable",
                status=NewsProviderStatus.PROVIDER_UNAVAILABLE,
                sanitized_endpoint=self._sanitized_url(),
                provider_error_message=type(exc).__name__,
            )
            self._record_error(error)
            raise error from exc

        self._cached_items = items
        self._cached_at = datetime.now(UTC)
        self._persist_cache()
        self._last_status = NewsProviderStatus.AVAILABLE if items else NewsProviderStatus.DELAYED
        self._last_diagnostics = {
            "status": self._last_status.value,
            "articles_returned": len(items),
            "cache_hit": False,
            "locale": self._locale,
            "region": self._region,
            "candidate_symbol": self._candidate_symbol,
            "broker_write_calls": 0,
        }
        return items

    def _url(self) -> str:
        return f"{self._base_url}?{urlencode({
            'q': self._query,
            'hl': self._locale,
            'gl': self._region,
            'ceid': f'{self._region}:{self._locale.split("-", 1)[0]}',
        })}"

    def _sanitized_url(self) -> str:
        return _sanitize_url(self._url())

    def _parse_feed(self, text: str, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        root = ElementTree.fromstring(text)
        items: list[RawNewsItem] = []
        for item in root.iter():
            if _local_name(item.tag) != "item":
                continue
            values = {_local_name(child.tag): (child.text or "").strip() for child in item}
            headline = _clean_text(values.get("title"))
            published = _parse_timestamp(values.get("pubDate"))
            if not headline or published is None or published > as_of:
                continue
            if as_of - published > WEB_NEWS_CACHE_RETENTION:
                continue
            source = _clean_text(values.get("source")) or "Google News RSS"
            summary = _clean_text(values.get("description"))
            items.append(
                RawNewsItem(
                    headline=headline,
                    source=source,
                    published_at=published,
                    url_or_reference=_clean_text(values.get("link")),
                    language=self._locale.split("-", 1)[0],
                    geographic_scope="ASIA" if self._region in {"JP", "HK"} else "GLOBAL",
                    summary=summary,
                    provider_symbols=(self._candidate_symbol,),
                    source_quality=NewsSourceQuality.SECONDARY_MEDIA,
                    provider=self.provider_name,
                )
            )
            if len(items) >= self._limit:
                break
        return tuple(items)

    def _fresh_cache(self, *, as_of: datetime) -> tuple[RawNewsItem, ...] | None:
        if self._cached_items is None or self._cached_at is None:
            return None
        if datetime.now(UTC) - self._cached_at > self._cache_ttl:
            return None
        return tuple(item for item in self._cached_items if item.published_at <= as_of)

    def _load_cache(self) -> None:
        if self._cache_path is None or not self._cache_path.exists():
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        key = self._cache_key()
        entry = payload.get("entries", {}).get(key) if isinstance(payload, dict) else None
        if not isinstance(entry, dict):
            return
        cached_at = _parse_timestamp(entry.get("cached_at"))
        if cached_at is None or datetime.now(UTC) - cached_at > self._cache_retention:
            return
        raw_items = entry.get("items")
        if not isinstance(raw_items, list):
            return
        items: list[RawNewsItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                items.append(RawNewsItem.model_validate(raw))
            except (TypeError, ValueError):
                continue
        self._cached_items = tuple(items)
        self._cached_at = cached_at

    def _persist_cache(self) -> None:
        if self._cache_path is None or self._cached_items is None or self._cached_at is None:
            return
        try:
            with _CACHE_LOCK:
                payload: dict[str, Any] = {"entries": {}}
                if self._cache_path.exists():
                    loaded = json.loads(self._cache_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                        payload = loaded
                payload["entries"][self._cache_key()] = {
                    "cached_at": self._cached_at.isoformat(),
                    "items": [item.model_dump(mode="json") for item in self._cached_items],
                }
                # Keep the file bounded if many candidate queries were used.
                entries = payload["entries"]
                for key in tuple(entries)[:-16]:
                    entries.pop(key, None)
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                self._cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            # News cache persistence is an optimization; it must never change
            # the fail-closed behavior of the RiskManager.
            return

    def _cache_key(self) -> str:
        value = (
            f"{self.provider_name}|{self._base_url}|{self._query}|"
            f"{self._candidate_symbol}|{self._locale}|{self._region}"
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _record_error(self, error: NewsProviderError) -> None:
        self._last_status = error.status
        self._last_diagnostics = {
            "status": error.status.value,
            "http_status": error.http_status,
            "sanitized_endpoint": error.sanitized_endpoint,
            "provider_error_message": error.provider_error_message,
            "locale": self._locale,
            "region": self._region,
            "candidate_symbol": self._candidate_symbol,
            "broker_write_calls": 0,
        }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _status_from_data_error(error: DataProviderError) -> NewsProviderStatus:
    if error.status is DataProviderStatus.RATE_LIMITED or error.http_status == 429:
        return NewsProviderStatus.RATE_LIMITED
    if error.status is DataProviderStatus.DATA_CONFLICT:
        return NewsProviderStatus.MALFORMED_RESPONSE
    return NewsProviderStatus.PROVIDER_UNAVAILABLE


class BingNewsRssProvider(GoogleNewsRssProvider):
    """Bing News RSS adapter with the same bounded parser and cache rules."""

    provider_name = BING_NEWS_RSS_PROVIDER

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", BING_NEWS_RSS_URL)
        super().__init__(**kwargs)

    def _url(self) -> str:
        return f"{self._base_url}?{urlencode({
            'q': self._query,
            'format': 'rss',
            'setlang': self._locale,
            'cc': self._region,
        })}"
