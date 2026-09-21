"""Read-only Alpaca News provider for secondary market-news corroboration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from app.data.historical.providers import HttpTextTransport, UrllibTextTransport, _sanitize_url
from app.data.models import DataProviderError, DataProviderStatus
from app.news.intelligence import (
    NewsProviderError,
    NewsProviderStatus,
    NewsSourceQuality,
    RawNewsItem,
)

ALPACA_NEWS_PROVIDER = "ALPACA_NEWS"
ALPACA_NEWS_BASE_URL = "https://data.alpaca.markets/v1beta1/news"
ALPACA_API_KEY_ID_ENV = "ALPACA_API_KEY_ID"
ALPACA_API_SECRET_KEY_ENV = "ALPACA_API_SECRET_KEY"


class AlpacaNewsProvider:
    """Fetch Alpaca news without exposing credentials or trading capabilities."""

    provider_name = ALPACA_NEWS_PROVIDER

    def __init__(
        self,
        *,
        api_key_id: str | None,
        api_secret_key: str | None,
        transport: HttpTextTransport | None = None,
        base_url: str = ALPACA_NEWS_BASE_URL,
        symbols: tuple[str, ...] = (),
        limit: int = 50,
        lookback_hours: int = 48,
    ) -> None:
        self._api_key_id = api_key_id.strip() if api_key_id and api_key_id.strip() else None
        self._api_secret_key = (
            api_secret_key.strip() if api_secret_key and api_secret_key.strip() else None
        )
        self._transport = transport or UrllibTextTransport()
        self._base_url = base_url.rstrip("/")
        self._symbols = tuple(_alpaca_symbol(symbol) for symbol in symbols if symbol.strip())
        self._limit = min(max(1, limit), 50)
        self._lookback_hours = min(max(1, lookback_hours), 168)
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
        """Match Alpha's shared-run scope API for one-shot and continuous callers."""
        self._symbols = tuple(_alpaca_symbol(ticker) for ticker in tickers if ticker.strip())

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        self.read_calls += 1
        if self._api_key_id is None or self._api_secret_key is None:
            error = NewsProviderError(
                "Alpaca News credentials are not configured",
                status=NewsProviderStatus.AUTH_FAILED,
                sanitized_endpoint=self._sanitized_url(as_of=as_of),
            )
            self._record_error(error)
            raise error
        start = as_of.astimezone(UTC).replace(microsecond=0)
        start = start.replace(hour=start.hour, minute=start.minute, second=start.second)
        start -= timedelta(hours=self._lookback_hours)
        url = self._url(start=start, end=as_of.astimezone(UTC))
        try:
            text = self._transport.get_text(url, self._headers())
            payload = json.loads(text)
        except DataProviderError as exc:
            error = NewsProviderError(
                "Alpaca News provider read failed",
                status=_status_from_data_error(exc),
                http_status=exc.http_status,
                sanitized_endpoint=exc.sanitized_endpoint or self._sanitized_url(as_of=as_of),
                provider_error_message=exc.provider_error_message or exc.provider_error_code,
            )
            self._record_error(error)
            raise error from exc
        except (ValueError, OSError, TimeoutError) as exc:
            error = NewsProviderError(
                "Alpaca News provider is unavailable",
                status=NewsProviderStatus.PROVIDER_UNAVAILABLE,
                sanitized_endpoint=self._sanitized_url(as_of=as_of),
                provider_error_message=type(exc).__name__,
            )
            self._record_error(error)
            raise error from exc
        except Exception as exc:
            error = NewsProviderError(
                "Alpaca News provider is unavailable",
                status=NewsProviderStatus.PROVIDER_UNAVAILABLE,
                sanitized_endpoint=self._sanitized_url(as_of=as_of),
                provider_error_message=type(exc).__name__,
            )
            self._record_error(error)
            raise error from exc

        try:
            items = self._parse_payload(payload, as_of=as_of)
        except NewsProviderError as error:
            self._record_error(error)
            raise
        self._last_status = NewsProviderStatus.AVAILABLE if items else NewsProviderStatus.DELAYED
        self._last_diagnostics = {
            "status": self._last_status.value,
            "sanitized_endpoint": self._sanitized_url(as_of=as_of),
            "articles_returned": len(items),
            "symbols_requested": self._symbols,
            "causal_cutoff": as_of.astimezone(UTC).isoformat(),
        }
        return items

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": "AegisInvestAI/0.7",
            "Accept": "application/json",
            "APCA-API-KEY-ID": self._api_key_id or "",
            "APCA-API-SECRET-KEY": self._api_secret_key or "",
        }

    def _url(self, *, start: datetime, end: datetime) -> str:
        query: dict[str, str] = {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "sort": "desc",
            "limit": str(self._limit),
            "include_content": "false",
        }
        if self._symbols:
            query["symbols"] = ",".join(self._symbols)
        return f"{self._base_url}?{urlencode(query)}"

    def _sanitized_url(self, *, as_of: datetime) -> str:
        start = as_of.astimezone(UTC) - timedelta(hours=self._lookback_hours)
        return _sanitize_url(self._url(start=start, end=as_of.astimezone(UTC)))

    def _parse_payload(self, payload: object, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        if not isinstance(payload, dict):
            raise NewsProviderError(
                "Alpaca News response is malformed",
                status=NewsProviderStatus.MALFORMED_RESPONSE,
                sanitized_endpoint=self._sanitized_url(as_of=as_of),
                provider_error_message="response root is not an object",
            )
        raw_news = payload.get("news")
        if not isinstance(raw_news, list):
            raise NewsProviderError(
                "Alpaca News response is malformed",
                status=NewsProviderStatus.MALFORMED_RESPONSE,
                sanitized_endpoint=self._sanitized_url(as_of=as_of),
                provider_error_message="missing news array",
            )
        items: list[RawNewsItem] = []
        for raw in raw_news:
            if not isinstance(raw, dict):
                continue
            item = _parse_article(raw)
            if item is not None and item.published_at <= as_of:
                items.append(item)
        return tuple(items)

    def _record_error(self, error: NewsProviderError) -> None:
        self._last_status = error.status
        self._last_diagnostics = {
            "status": error.status.value,
            "http_status": error.http_status,
            "sanitized_endpoint": error.sanitized_endpoint,
            "provider_error_message": error.provider_error_message,
        }


def _parse_article(raw: dict[str, Any]) -> RawNewsItem | None:
    headline = _text(raw.get("headline"))
    source = _text(raw.get("source")) or "ALPACA"
    published = raw.get("created_at") or raw.get("updated_at")
    if not headline or not published:
        return None
    try:
        published_at = datetime.fromisoformat(str(published).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    symbols = tuple(
        str(symbol).strip().upper()
        for symbol in raw.get("symbols", ())
        if str(symbol).strip()
    )
    summary = _text(raw.get("summary")) or _text(raw.get("content"))
    symbol_text = " ".join(symbols)
    return RawNewsItem(
        headline=headline,
        source=source,
        published_at=published_at,
        url_or_reference=_text(raw.get("url")) or _text(raw.get("id")),
        language="en",
        geographic_scope="GLOBAL",
        summary=f"{summary or ''} {symbol_text}".strip() or None,
        source_quality=NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
        provider=ALPACA_NEWS_PROVIDER,
    )


def _alpaca_symbol(symbol: str) -> str:
    raw = symbol.strip().upper()
    if raw.startswith("CRYPTO:"):
        return f"{raw.split(':', 1)[1]}USD"
    return raw


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _status_from_data_error(error: DataProviderError) -> NewsProviderStatus:
    if error.status is DataProviderStatus.RATE_LIMITED:
        return NewsProviderStatus.RATE_LIMITED
    if error.http_status in {401, 403}:
        return NewsProviderStatus.AUTH_FAILED
    if error.status is DataProviderStatus.DATA_CONFLICT:
        return NewsProviderStatus.MALFORMED_RESPONSE
    return NewsProviderStatus.PROVIDER_UNAVAILABLE
