"""Read-only Alpha Vantage NEWS_SENTIMENT provider adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlencode

from app.data.historical.providers import HttpTextTransport, UrllibTextTransport, _sanitize_url
from app.data.models import DataProviderError, DataProviderStatus
from app.domain.enums import AssetClass
from app.news.intelligence import (
    NewsProviderError,
    NewsProviderStatus,
    NewsSourceQuality,
    RawNewsItem,
)

ALPHA_VANTAGE_PROVIDER = "ALPHA_VANTAGE"
ALPHA_VANTAGE_NEWS_FUNCTION = "NEWS_SENTIMENT"
ALPHA_VANTAGE_API_KEY_ENV = "ALPHA_VANTAGE_API_KEY"
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
MAX_NEWS_TICKER_SCOPE = 50


class AlphaVantageNewsProvider:
    """Provider-neutral adapter for Alpha Vantage NEWS_SENTIMENT.

    It only reads public/news data through an injectable HTTP transport. The
    API key is never logged, persisted, or exposed in provider diagnostics.
    """

    provider_name = ALPHA_VANTAGE_PROVIDER

    def __init__(
        self,
        *,
        api_key: str | None,
        transport: HttpTextTransport | None = None,
        base_url: str = ALPHA_VANTAGE_BASE_URL,
        tickers: tuple[str, ...] = (),
        topics: tuple[str, ...] = (),
        limit: int = 50,
    ) -> None:
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._transport = transport or UrllibTextTransport()
        self._base_url = base_url
        self._tickers = tickers
        self._topics = topics
        self._limit = limit
        self._last_status = NewsProviderStatus.PROVIDER_UNAVAILABLE
        self._last_diagnostics: dict[str, object] = {}

    @property
    def last_status(self) -> NewsProviderStatus:
        return self._last_status

    @property
    def last_diagnostics(self) -> dict[str, object]:
        return dict(self._last_diagnostics)

    def set_tickers(self, tickers: tuple[str, ...]) -> None:
        """Select the ticker scope for the next read on a shared run provider."""
        # The runtime scanner can contain the whole eToro catalog.  Keep the
        # provider request bounded and retain crypto symbols first so the
        # Crypto lane is not crowded out by the equity catalog.
        normalized = tuple(dict.fromkeys(ticker.strip() for ticker in tickers if ticker.strip()))
        crypto = tuple(
            ticker
            for ticker in normalized
            if ticker.upper().startswith("CRYPTO:")
            or ticker.upper()
            in {"BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "LINK", "LTC", "BCH", "DOT"}
        )
        remainder = tuple(ticker for ticker in normalized if ticker not in crypto)
        self._tickers = (crypto + remainder)[:MAX_NEWS_TICKER_SCOPE]

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        if self._api_key is None:
            raise NewsProviderError(
                "Alpha Vantage API key is not configured",
                status=NewsProviderStatus.AUTH_FAILED,
                sanitized_endpoint=self._sanitized_url(),
            )
        url = self._url(redacted=False)
        try:
            payload = json.loads(
                self._transport.get_text(
                    url,
                    {"User-Agent": "AegisInvestAI/0.7", "Accept": "application/json"},
                )
            )
        except NewsProviderError:
            raise
        except DataProviderError as exc:
            status = _status_from_data_provider_error(exc)
            raise NewsProviderError(
                "Alpha Vantage news provider read failed",
                status=status,
                http_status=exc.http_status,
                sanitized_endpoint=exc.sanitized_endpoint or self._sanitized_url(),
                provider_error_message=self._sanitize_provider_message(
                    exc.provider_error_message
                    or exc.provider_error_code
                    or exc.transport_category
                    or "provider error"
                ),
            ) from exc
        except Exception as exc:
            raise NewsProviderError(
                "Alpha Vantage news provider is unavailable",
                status=NewsProviderStatus.PROVIDER_UNAVAILABLE,
                sanitized_endpoint=self._sanitized_url(),
                provider_error_message=type(exc).__name__,
            ) from exc
        items = self._parse_payload(payload, as_of=as_of)
        self._last_status = NewsProviderStatus.AVAILABLE
        self._last_diagnostics = {
            "status": self._last_status.value,
            "sanitized_endpoint": self._sanitized_url(),
            "articles_returned": len(items),
        }
        return items

    def _url(self, *, redacted: bool) -> str:
        query: dict[str, str] = {
            "function": ALPHA_VANTAGE_NEWS_FUNCTION,
            "apikey": "REDACTED" if redacted else (self._api_key or ""),
            "limit": str(self._limit),
        }
        if self._tickers:
            query["tickers"] = ",".join(self._tickers)
        if self._topics:
            query["topics"] = ",".join(self._topics)
        return f"{self._base_url}?{urlencode(query)}"

    def _sanitized_url(self) -> str:
        return _sanitize_url(self._url(redacted=False))

    def _parse_payload(self, payload: object, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        if not isinstance(payload, dict):
            raise self._malformed("response root is not an object")
        message = _provider_message(payload)
        if message is not None:
            safe_message = self._sanitize_provider_message(message)
            status = _status_from_provider_message(safe_message)
            raise NewsProviderError(
                "Alpha Vantage news provider returned a failure status",
                status=status,
                sanitized_endpoint=self._sanitized_url(),
                provider_error_message=safe_message,
            )
        feed = payload.get("feed")
        if not isinstance(feed, list):
            raise self._malformed("missing feed array")
        items: list[RawNewsItem] = []
        for raw in feed:
            if not isinstance(raw, dict):
                raise self._malformed("feed item is not an object")
            item = _parse_article(raw)
            if item.published_at <= as_of:
                items.append(item)
        return tuple(items)

    def _malformed(self, message: str) -> NewsProviderError:
        return NewsProviderError(
            "Alpha Vantage news response is malformed",
            status=NewsProviderStatus.MALFORMED_RESPONSE,
            sanitized_endpoint=self._sanitized_url(),
            provider_error_message=message,
        )

    def _sanitize_provider_message(self, message: str) -> str:
        """Prevent provider-echoed credentials from reaching diagnostics."""
        if self._api_key is None:
            return message
        return message.replace(self._api_key, "<redacted>")


def alpha_vantage_request_plan(
    *, universe_symbols: tuple[str, ...], include_macro_topics: bool = True
) -> dict[str, object]:
    equity_tickers = tuple(symbol for symbol in universe_symbols if symbol not in _CRYPTO_SYMBOLS)
    crypto_tickers = tuple(
        f"CRYPTO:{symbol}" for symbol in universe_symbols if symbol in _CRYPTO_SYMBOLS
    )
    topics = (
        "economy_monetary",
        "economy_fiscal",
        "mergers_and_acquisitions",
        "earnings",
        "blockchain",
        "financial_markets",
    )
    return {
        "provider": ALPHA_VANTAGE_PROVIDER,
        "strategy": "batch broad requests; do not fetch once per symbol",
        "ticker_batches": (
            equity_tickers[:50],
            crypto_tickers[:50],
        ),
        "topics": topics if include_macro_topics else (),
        "polling": (
            "macro/topics request for high-impact global context",
            "batched universe ticker request for 34 monitored symbols",
            "dedupe through GlobalNewsIntelligenceEngine before scanner use",
        ),
        "broker_write_calls": 0,
    }


def news_provider_readiness_matrix() -> tuple[dict[str, object], ...]:
    return (
        {
            "provider": "ALPHA_VANTAGE_NEWS_SENTIMENT",
            "status": "IMPLEMENTED_OFFLINE_READY",
            "role": "primary broad financial/news context",
            "credentials": (ALPHA_VANTAGE_API_KEY_ENV,),
            "network_calls_in_tests": 0,
        },
        {
            "provider": "MASSIVE_NEWS",
            "status": "READINESS_ENTRY_ONLY",
            "role": (
                "future ticker-specific corroboration, publisher diversity, "
                "second-source verification"
            ),
            "implemented": False,
        },
    )


def alpha_vantage_symbol_to_aegis(symbol: str) -> dict[str, object]:
    raw = symbol.strip().upper()
    if raw.startswith("CRYPTO:"):
        return {
            "provider_symbol": raw,
            "canonical_symbol": raw.split(":", maxsplit=1)[1],
            "asset_class": AssetClass.CRYPTO.value,
            "known": True,
        }
    if raw.startswith("FOREX:"):
        return {
            "provider_symbol": raw,
            "canonical_symbol": raw.split(":", maxsplit=1)[1],
            "asset_class": AssetClass.FOREX.value,
            "known": False,
        }
    if raw:
        return {
            "provider_symbol": raw,
            "canonical_symbol": raw,
            "asset_class": AssetClass.UNKNOWN.value,
            "known": False,
        }
    return {
        "provider_symbol": symbol,
        "canonical_symbol": "",
        "asset_class": AssetClass.UNKNOWN.value,
        "known": False,
    }


def _parse_article(raw: dict[str, object]) -> RawNewsItem:
    title = _required_string(raw, "title")
    published_at = _parse_alpha_vantage_timestamp(_required_string(raw, "time_published"))
    source = _required_string(raw, "source")
    topics = _topics(raw)
    ticker_sentiment = _ticker_sentiment(raw)
    return RawNewsItem(
        headline=title,
        source=source,
        published_at=published_at,
        url_or_reference=_optional_string(raw, "url") or _article_id(raw),
        language="en",
        geographic_scope=_geographic_scope(topics=ticker_sentiment + topics),
        summary=_summary_with_provider_fields(
            raw, topics=topics, ticker_sentiment=ticker_sentiment
        ),
        source_quality=_source_quality(source),
        provider=ALPHA_VANTAGE_PROVIDER,
    )


def _parse_alpha_vantage_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise NewsProviderError(
            "Alpha Vantage timestamp is malformed",
            status=NewsProviderStatus.MALFORMED_RESPONSE,
            provider_error_message="invalid time_published",
        ) from exc


def _summary_with_provider_fields(
    raw: dict[str, object],
    *,
    topics: tuple[str, ...],
    ticker_sentiment: tuple[str, ...],
) -> str:
    summary = _optional_string(raw, "summary") or ""
    overall = _optional_string(raw, "overall_sentiment_label")
    pieces = [summary]
    if overall:
        pieces.append(f"overall_sentiment={overall}")
    if topics:
        pieces.append("topics=" + ",".join(topics))
    if ticker_sentiment:
        pieces.append("tickers=" + ",".join(ticker_sentiment))
    return " ".join(piece for piece in pieces if piece).strip() or "Alpha Vantage news item"


def _topics(raw: dict[str, object]) -> tuple[str, ...]:
    topics = raw.get("topics", ())
    if not isinstance(topics, list):
        return ()
    result: list[str] = []
    for item in topics:
        if isinstance(item, dict) and isinstance(item.get("topic"), str):
            result.append(cast(str, item["topic"]).strip().lower())
    return tuple(item for item in result if item)


def _ticker_sentiment(raw: dict[str, object]) -> tuple[str, ...]:
    rows = raw.get("ticker_sentiment", ())
    if not isinstance(rows, list):
        return ()
    symbols: list[str] = []
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("ticker"), str):
            continue
        ticker = cast(str, item["ticker"]).strip().upper()
        mapped = alpha_vantage_symbol_to_aegis(ticker)
        canonical = str(mapped["canonical_symbol"])
        if canonical:
            symbols.append(canonical)
    return tuple(symbols)


def _article_id(raw: dict[str, object]) -> str:
    seed = "|".join(
        (
            _optional_string(raw, "title") or "",
            _optional_string(raw, "source") or "",
            _optional_string(raw, "time_published") or "",
        )
    )
    return "alpha-vantage:" + seed


def _provider_message(payload: dict[str, object]) -> str | None:
    for key in ("Information", "Note", "Error Message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _status_from_provider_message(message: str) -> NewsProviderStatus:
    lowered = message.casefold()
    if "rate" in lowered or "frequency" in lowered or "standard api call frequency" in lowered:
        return NewsProviderStatus.RATE_LIMITED
    if "api key" in lowered or "invalid" in lowered:
        return NewsProviderStatus.AUTH_FAILED
    if "delayed" in lowered:
        return NewsProviderStatus.DELAYED
    return NewsProviderStatus.PROVIDER_UNAVAILABLE


def _status_from_data_provider_error(exc: DataProviderError) -> NewsProviderStatus:
    if exc.status is DataProviderStatus.RATE_LIMITED or exc.http_status == 429:
        return NewsProviderStatus.RATE_LIMITED
    if exc.http_status in {401, 403}:
        return NewsProviderStatus.AUTH_FAILED
    if exc.status is DataProviderStatus.TIMEOUT:
        return NewsProviderStatus.PROVIDER_UNAVAILABLE
    return NewsProviderStatus.PROVIDER_UNAVAILABLE


def _source_quality(source: str) -> NewsSourceQuality:
    lowered = source.casefold()
    if any(term in lowered for term in ("sec", "federal reserve", "treasury", "white house")):
        return NewsSourceQuality.REGULATORY_GOVERNMENT
    if any(term in lowered for term in ("reuters", "bloomberg", "cnbc", "wsj", "financial times")):
        return NewsSourceQuality.MAJOR_FINANCIAL_NEWS
    if "press release" in lowered or "investor relations" in lowered:
        return NewsSourceQuality.COMPANY_RELEASE
    return NewsSourceQuality.SECONDARY_MEDIA


def _geographic_scope(*, topics: tuple[str, ...]) -> str:
    if any(symbol in topics for symbol in ("USD", "AAPL", "SPY")):
        return "US"
    if topics:
        return "GLOBAL"
    return "UNKNOWN"


def _required_string(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NewsProviderError(
            "Alpha Vantage news response is malformed",
            status=NewsProviderStatus.MALFORMED_RESPONSE,
            sanitized_endpoint=_sanitize_url(ALPHA_VANTAGE_BASE_URL),
            provider_error_message=f"missing {key}",
        )
    return value.strip()


def _optional_string(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


_CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "LINK", "LTC", "BCH", "DOT"}
