"""Read-only GDELT DOC 2.0 global news discovery provider."""

from __future__ import annotations

import json
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlencode

from app.data.historical.providers import HttpTextTransport, UrllibTextTransport, _sanitize_url
from app.data.models import DataProviderError, DataProviderStatus
from app.news.intelligence import (
    NewsProviderError,
    NewsProviderStatus,
    NEWS_FRESH_MAX_AGE,
    NewsSourceQuality,
    RawNewsItem,
)

GDELT_NEWS_PROVIDER = "GDELT_DOC"
GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MAX_RECORDS = 75
GDELT_CACHE_TTL = timedelta(minutes=10)
GDELT_CACHE_RETENTION_TTL = NEWS_FRESH_MAX_AGE
GDELT_MIN_REQUEST_INTERVAL_SECONDS = 5.0
GDELT_RATE_LIMIT_BACKOFF_BASE_SECONDS = 30.0
GDELT_RATE_LIMIT_BACKOFF_MAX_SECONDS = 900.0
GDELT_QUERY = (
    '("armed conflict" OR ceasefire OR sanctions OR tariff OR election OR '
    '"central bank" OR inflation OR "interest rates" OR "oil supply" OR '
    '"trade restrictions" OR cyberattack OR "supply chain")'
)

_REQUEST_LOCK = Lock()
_LAST_REQUEST_STARTED_AT = 0.0
_SHARED_RATE_LIMIT_UNTIL = 0.0
_SHARED_RATE_LIMIT_STREAK = 0


class GdeltNewsProvider:
    """Fetch one bounded global article list; never accesses broker capabilities."""

    provider_name = GDELT_NEWS_PROVIDER

    def __init__(
        self,
        *,
        transport: HttpTextTransport | None = None,
        base_url: str = GDELT_DOC_API_URL,
        query: str = GDELT_QUERY,
        max_records: int = GDELT_MAX_RECORDS,
        cache_ttl: timedelta = GDELT_CACHE_TTL,
        cache_retention_ttl: timedelta = GDELT_CACHE_RETENTION_TTL,
        cache_path: Path | None = None,
    ) -> None:
        self._transport = transport or UrllibTextTransport()
        self._base_url = base_url.rstrip("?")
        self._query = query.strip() or GDELT_QUERY
        self._max_records = min(max(1, max_records), 250)
        self._cache_ttl = cache_ttl
        self._cache_retention_ttl = max(cache_ttl, cache_retention_ttl)
        self._cache_path = cache_path
        self._cached_items: tuple[RawNewsItem, ...] | None = None
        self._cached_at: float | None = None
        self._cached_as_of: datetime | None = None
        self._cache_status = NewsProviderStatus.PROVIDER_UNAVAILABLE
        self._rate_limit_until = 0.0
        self._rate_limit_streak = 0
        self.read_calls = 0
        self._last_status = NewsProviderStatus.PROVIDER_UNAVAILABLE
        self._last_diagnostics: dict[str, object] = {}
        self._load_persisted_cache()

    @property
    def last_status(self) -> NewsProviderStatus:
        return self._last_status

    @property
    def last_diagnostics(self) -> dict[str, object]:
        return dict(self._last_diagnostics)

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        global _SHARED_RATE_LIMIT_UNTIL, _SHARED_RATE_LIMIT_STREAK
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise NewsProviderError(
                "GDELT news cutoff must include a timezone",
                status=NewsProviderStatus.MALFORMED_RESPONSE,
                provider_error_message="naive as_of timestamp",
            )

        with _REQUEST_LOCK:
            cached = self._cached_result(as_of=as_of)
            if cached is not None:
                self._last_status = self._cache_status
                self._last_diagnostics = {
                    "status": self._last_status.value,
                    "articles_returned": len(cached),
                    "cache_hit": True,
                    "max_records": self._max_records,
                    "minimum_request_interval_seconds": GDELT_MIN_REQUEST_INTERVAL_SECONDS,
                    "broker_write_calls": 0,
                }
                return cached

            now_monotonic = monotonic()
            cooldown_until = max(self._rate_limit_until, _SHARED_RATE_LIMIT_UNTIL)
            if now_monotonic < cooldown_until:
                retry_after_seconds = max(1, int(cooldown_until - now_monotonic))
                error = NewsProviderError(
                    "GDELT rate-limit cooldown is active",
                    status=NewsProviderStatus.RATE_LIMITED,
                    http_status=429,
                    provider_error_message="provider cooldown",
                    retry_after=str(retry_after_seconds),
                )
                fallback = self._fresh_cache_fallback(as_of=as_of)
                if fallback:
                    return self._return_cache_fallback(
                        fallback,
                        as_of=as_of,
                        cause=error,
                        retry_after_seconds=retry_after_seconds,
                    )
                self._record_error(error)
                raise error

            global _LAST_REQUEST_STARTED_AT
            wait_seconds = GDELT_MIN_REQUEST_INTERVAL_SECONDS - (
                monotonic() - _LAST_REQUEST_STARTED_AT
            )
            if _LAST_REQUEST_STARTED_AT and wait_seconds > 0:
                sleep(wait_seconds)
            _LAST_REQUEST_STARTED_AT = monotonic()
            self.read_calls += 1

            url = self._url(as_of=as_of)
            try:
                text = self._transport.get_text(
                    url,
                    {
                        "User-Agent": "AegisInvestAI/0.7",
                        "Accept": "application/json",
                    },
                )
                payload = json.loads(text)
                items = self._parse_payload(payload, as_of=as_of)
            except DataProviderError as exc:
                error = NewsProviderError(
                    "GDELT news provider read failed",
                    status=_status_from_data_error(exc),
                    http_status=exc.http_status,
                    sanitized_endpoint=exc.sanitized_endpoint or _sanitize_url(url),
                    provider_error_message=(
                        exc.provider_error_code or exc.transport_category or "provider error"
                    ),
                    retry_after=exc.retry_after,
                )
                self._record_error(error)
                fallback = self._fallback_after_error(error, as_of=as_of)
                if fallback is not None:
                    return fallback
                raise error from exc
            except NewsProviderError as exc:
                self._record_error(exc)
                fallback = self._fallback_after_error(exc, as_of=as_of)
                if fallback is not None:
                    return fallback
                raise
            except Exception as exc:
                error = NewsProviderError(
                    "GDELT news provider response is unavailable",
                    status=(
                        NewsProviderStatus.MALFORMED_RESPONSE
                        if isinstance(exc, (json.JSONDecodeError, TypeError, ValueError))
                        else NewsProviderStatus.PROVIDER_UNAVAILABLE
                    ),
                    sanitized_endpoint=_sanitize_url(url),
                    provider_error_message=type(exc).__name__,
                )
                self._record_error(error)
                fallback = self._fallback_after_error(error, as_of=as_of)
                if fallback is not None:
                    return fallback
                raise error from exc

            self._cached_items = items
            self._cached_at = monotonic()
            self._cached_as_of = as_of
            self._rate_limit_until = 0.0
            self._rate_limit_streak = 0
            _SHARED_RATE_LIMIT_UNTIL = 0.0
            _SHARED_RATE_LIMIT_STREAK = 0
            self._cache_status = (
                NewsProviderStatus.AVAILABLE if items else NewsProviderStatus.DELAYED
            )
            self._persist_cache(as_of=as_of, items=items)
            self._last_status = self._cache_status
            self._last_diagnostics = {
                "status": self._last_status.value,
                "articles_returned": len(items),
                "cache_hit": False,
                "max_records": self._max_records,
                "minimum_request_interval_seconds": GDELT_MIN_REQUEST_INTERVAL_SECONDS,
                "causal_cutoff": as_of.astimezone(UTC).isoformat(),
                "broker_write_calls": 0,
            }
            return items

    def _load_persisted_cache(self) -> None:
        """Warm the bounded cache after a runner restart without another API call."""
        if self._cache_path is None:
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            saved_at = datetime.fromisoformat(str(payload.get("saved_at", "")))
            cached_as_of = datetime.fromisoformat(str(payload.get("as_of", "")))
            raw_items = payload.get("items")
            if (
                saved_at.tzinfo is None
                or saved_at.utcoffset() is None
                or cached_as_of.tzinfo is None
                or cached_as_of.utcoffset() is None
                or not isinstance(raw_items, list)
            ):
                return
            age = datetime.now(UTC) - saved_at.astimezone(UTC)
            if age < timedelta(0) or age > self._cache_retention_ttl:
                return
            items = tuple(RawNewsItem.model_validate(row) for row in raw_items)
            self._cached_items = items
            self._cached_at = monotonic() - age.total_seconds()
            self._cached_as_of = cached_as_of
            self._cache_status = (
                NewsProviderStatus.AVAILABLE if items else NewsProviderStatus.DELAYED
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # A damaged/expired cache is never treated as evidence; the next
            # bounded provider request will rebuild it.
            return

    def _persist_cache(
        self, *, as_of: datetime, items: tuple[RawNewsItem, ...]
    ) -> None:
        if self._cache_path is None or not items:
            return
        temporary_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        payload = {
            "saved_at": datetime.now(UTC).isoformat(),
            "as_of": as_of.isoformat(),
            "items": [item.model_dump(mode="json") for item in items],
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary_path.replace(self._cache_path)
        except OSError:
            # Persistence is an optimization for restart/rate-limit resilience;
            # it must not discard a successful live response.
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _cached_result(self, *, as_of: datetime) -> tuple[RawNewsItem, ...] | None:
        if self._cached_items is None or self._cached_at is None or self._cached_as_of is None:
            return None
        age_seconds = monotonic() - self._cached_at
        cutoff_delta = as_of.astimezone(UTC) - self._cached_as_of.astimezone(UTC)
        if age_seconds < 0 or age_seconds > self._cache_ttl.total_seconds():
            return None
        if cutoff_delta < timedelta(0) or cutoff_delta > self._cache_ttl:
            return None
        return tuple(item for item in self._cached_items if item.published_at <= as_of)

    def _fresh_cache_fallback(
        self, *, as_of: datetime
    ) -> tuple[RawNewsItem, ...] | None:
        if self._cached_items is None or self._cached_at is None or self._cached_as_of is None:
            return None
        age_seconds = monotonic() - self._cached_at
        cutoff_delta = as_of.astimezone(UTC) - self._cached_as_of.astimezone(UTC)
        if age_seconds < 0 or age_seconds > self._cache_retention_ttl.total_seconds():
            return None
        if cutoff_delta < timedelta(0) or cutoff_delta > self._cache_retention_ttl:
            return None
        fresh_after = as_of.astimezone(UTC) - NEWS_FRESH_MAX_AGE
        fresh_items = tuple(
            item
            for item in self._cached_items
            if fresh_after <= item.published_at.astimezone(UTC) <= as_of.astimezone(UTC)
        )
        return fresh_items or None

    def _fallback_after_error(
        self, error: NewsProviderError, *, as_of: datetime
    ) -> tuple[RawNewsItem, ...] | None:
        retry_after_seconds: int | None = None
        if error.status is NewsProviderStatus.RATE_LIMITED:
            retry_after_seconds = self._start_rate_limit_backoff(error.retry_after)
        fallback = self._fresh_cache_fallback(as_of=as_of)
        if fallback is None:
            return None
        return self._return_cache_fallback(
            fallback,
            as_of=as_of,
            cause=error,
            retry_after_seconds=retry_after_seconds,
        )

    def _return_cache_fallback(
        self,
        items: tuple[RawNewsItem, ...],
        *,
        as_of: datetime,
        cause: NewsProviderError,
        retry_after_seconds: int | None,
    ) -> tuple[RawNewsItem, ...]:
        self._last_status = NewsProviderStatus.DELAYED
        self._last_diagnostics = {
            "status": self._last_status.value,
            "articles_returned": len(items),
            "fresh_articles_returned": len(items),
            "cache_hit": True,
            "stale_cache_fallback": True,
            "source_error_status": cause.status.value,
            "source_http_status": cause.http_status,
            "retry_after_seconds": retry_after_seconds,
            "cache_retention_seconds": int(self._cache_retention_ttl.total_seconds()),
            "freshness_max_age_seconds": int(NEWS_FRESH_MAX_AGE.total_seconds()),
            "causal_cutoff": as_of.astimezone(UTC).isoformat(),
            "minimum_request_interval_seconds": GDELT_MIN_REQUEST_INTERVAL_SECONDS,
            "broker_write_calls": 0,
        }
        return items

    def _start_rate_limit_backoff(self, retry_after: str | None) -> int:
        global _SHARED_RATE_LIMIT_UNTIL, _SHARED_RATE_LIMIT_STREAK
        _SHARED_RATE_LIMIT_STREAK += 1
        self._rate_limit_streak = _SHARED_RATE_LIMIT_STREAK
        delay = _retry_after_seconds(retry_after)
        if delay is None:
            delay = min(
                GDELT_RATE_LIMIT_BACKOFF_MAX_SECONDS,
                GDELT_RATE_LIMIT_BACKOFF_BASE_SECONDS
                * 2 ** (_SHARED_RATE_LIMIT_STREAK - 1),
            )
        delay = max(GDELT_MIN_REQUEST_INTERVAL_SECONDS, delay)
        _SHARED_RATE_LIMIT_UNTIL = max(
            _SHARED_RATE_LIMIT_UNTIL,
            monotonic() + delay,
        )
        self._rate_limit_until = _SHARED_RATE_LIMIT_UNTIL
        if self._last_diagnostics:
            self._last_diagnostics["retry_after_seconds"] = int(delay)
        return int(delay)

    def _url(self, *, as_of: datetime) -> str:
        end = as_of.astimezone(UTC)
        start = end - timedelta(hours=24)
        query = urlencode(
            {
                "query": self._query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": str(self._max_records),
                "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                "enddatetime": end.strftime("%Y%m%d%H%M%S"),
                "sort": "DateDesc",
            }
        )
        return f"{self._base_url}?{query}"

    def _parse_payload(self, payload: object, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
            raise NewsProviderError(
                "GDELT news response is malformed",
                status=NewsProviderStatus.MALFORMED_RESPONSE,
                sanitized_endpoint=_sanitize_url(self._url(as_of=as_of)),
                provider_error_message="missing articles array",
            )

        articles = payload["articles"]
        items = tuple(
            item
            for raw in articles
            if isinstance(raw, dict)
            if (item := _parse_article(raw)) is not None and item.published_at <= as_of
        )
        if articles and not items:
            raise NewsProviderError(
                "GDELT news response contains no usable article records",
                status=NewsProviderStatus.MALFORMED_RESPONSE,
                sanitized_endpoint=_sanitize_url(self._url(as_of=as_of)),
                provider_error_message="article fields could not be parsed",
            )
        return items

    def _record_error(self, error: NewsProviderError) -> None:
        self._last_status = error.status
        self._last_diagnostics = {
            "status": error.status.value,
            "http_status": error.http_status,
            "sanitized_endpoint": error.sanitized_endpoint,
            "provider_error_message": error.provider_error_message,
            "broker_write_calls": 0,
        }
        retry_after_seconds = _retry_after_seconds(error.retry_after)
        if retry_after_seconds is not None:
            self._last_diagnostics["retry_after_seconds"] = int(retry_after_seconds)


def _parse_article(raw: dict[str, Any]) -> RawNewsItem | None:
    headline = _text(raw.get("title"))
    published_at = _parse_timestamp(raw.get("seendate"))
    if not headline or published_at is None:
        return None

    source = (
        _text(raw.get("sourceCommonName"))
        or _text(raw.get("domain"))
        or _text(raw.get("sourcecountry"))
        or "GDELT indexed outlet"
    )
    country = _text(raw.get("sourcecountry"))
    language = _text(raw.get("language")) or "und"
    return RawNewsItem(
        headline=headline,
        source=source,
        published_at=published_at,
        url_or_reference=_text(raw.get("url")),
        language=language,
        geographic_scope=country.upper() if country else "GLOBAL",
        source_quality=NewsSourceQuality.SECONDARY_MEDIA,
        provider=GDELT_NEWS_PROVIDER,
    )


def _parse_timestamp(value: object) -> datetime | None:
    raw = _text(value)
    if raw is None:
        return None
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _status_from_data_error(error: DataProviderError) -> NewsProviderStatus:
    if error.status is DataProviderStatus.RATE_LIMITED or error.http_status == 429:
        return NewsProviderStatus.RATE_LIMITED
    if error.http_status in {401, 403}:
        return NewsProviderStatus.AUTH_FAILED
    if error.status is DataProviderStatus.DATA_CONFLICT:
        return NewsProviderStatus.MALFORMED_RESPONSE
    return NewsProviderStatus.PROVIDER_UNAVAILABLE


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (AttributeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
