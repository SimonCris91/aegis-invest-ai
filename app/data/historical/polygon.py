"""Read-only Polygon/Massive historical aggregate adapter."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.data.historical.providers import HttpTextTransport, UrllibTextTransport
from app.data.models import DataProviderError, DataProviderStatus
from app.domain.enums import AssetClass, Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame

_INTERVALS = {
    TimeFrame.ONE_DAY: (1, "day"),
    TimeFrame.FOUR_HOUR: (4, "hour"),
}


class PolygonHistoricalMarketDataProvider:
    """Provider-neutral Polygon/Massive aggregates adapter.

    The API key is appended only inside the transport URL. The adapter never
    returns URLs, headers, or credential-bearing values to callers.
    """

    provider_name = "polygon"
    supported_timeframes = tuple(_INTERVALS)

    def __init__(
        self,
        *,
        api_key: str,
        transport: HttpTextTransport | None = None,
        base_url: str = "https://api.massive.com",
        max_pages: int = 5,
    ) -> None:
        cleaned = api_key.strip()
        if not cleaned:
            raise DataProviderError(
                "Polygon/Massive API key is not configured",
                status=DataProviderStatus.MAPPING_AMBIGUOUS,
            )
        self._api_key = cleaned
        self._transport = transport or UrllibTextTransport()
        self._base_url = base_url.rstrip("/")
        self._max_pages = max(1, max_pages)

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]:
        end = as_of
        start = as_of - _default_lookback(timeframe=timeframe, limit=limit)
        return self.get_bars_range(
            instrument,
            timeframe,
            start=start,
            end=end,
            limit=limit,
            max_pages=self._max_pages,
        )

    def get_bars_range(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        start: datetime,
        end: datetime,
        limit: int = 50_000,
        max_pages: int | None = None,
    ) -> tuple[MarketBar, ...]:
        if timeframe not in _INTERVALS:
            return ()
        if start >= end:
            raise DataProviderError(
                "historical range start must be before end",
                status=DataProviderStatus.DATA_INSUFFICIENT,
            )
        pages = max(1, max_pages or self._max_pages)
        url = self._aggregate_url(
            instrument=instrument,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
        )
        all_bars: list[MarketBar] = []
        for _ in range(pages):
            payload = self._get_json(url)
            all_bars.extend(_normalize_polygon_aggregates(payload, instrument, timeframe))
            next_url = payload.get("next_url")
            if not isinstance(next_url, str) or not next_url:
                break
            url = self._with_api_key(next_url)
        deduped = _dedupe_bars(tuple(all_bars))
        return tuple(bar for bar in deduped if start <= bar.timestamp <= end)

    def _aggregate_url(
        self,
        *,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> str:
        multiplier, timespan = _INTERVALS[timeframe]
        ticker = _provider_symbol(instrument)
        path_ticker = ticker.replace("/", "")
        path = f"/v2/aggs/ticker/{path_ticker}/range/{multiplier}/{timespan}"
        params = urlencode(
            {
                "adjusted": "true",
                "sort": "asc",
                "limit": str(min(max(1, limit), 50_000)),
                "apiKey": self._api_key,
            }
        )
        return f"{self._base_url}{path}/{_date_or_ms(start)}/{_date_or_ms(end)}?{params}"

    def _with_api_key(self, next_url: str) -> str:
        parsed = urlparse(next_url)
        safe_pairs = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"apikey", "api_key", "token", "authorization", "auth"}
        ]
        safe_pairs.append(("apiKey", self._api_key))
        query = urlencode(safe_pairs)
        return urlunparse(parsed._replace(query=query))

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            text = self._transport.get_text(url, {"User-Agent": "AegisInvestAI/0.7"})
            payload = json.loads(text)
        except DataProviderError:
            raise
        except TimeoutError as exc:
            raise DataProviderError(
                "Polygon/Massive historical read timed out",
                status=DataProviderStatus.TIMEOUT,
            ) from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DataProviderError(
                "Polygon/Massive historical read failed",
                status=DataProviderStatus.PROVIDER_UNAVAILABLE,
            ) from exc
        if not isinstance(payload, dict):
            raise DataProviderError(
                "Polygon/Massive historical response was not an object",
                status=DataProviderStatus.DATA_CONFLICT,
            )
        status = str(payload.get("status", "")).upper()
        if status in {"ERROR", "NOT_AUTHORIZED"}:
            raise DataProviderError(
                "Polygon/Massive historical response was not authorized",
                status=DataProviderStatus.PROVIDER_UNAVAILABLE,
                sanitized_endpoint=_sanitize_provider_payload_endpoint(payload),
                transport_category=status,
                provider_error_code=str(payload.get("status")),
                provider_error_message=_safe_payload_message(payload),
            )
        return payload


def _provider_symbol(instrument: UniversalInstrument) -> str:
    if instrument.asset_class is AssetClass.CRYPTO:
        return f"X:{instrument.symbol.upper()}USD"
    return instrument.symbol.upper()


def _date_or_ms(value: datetime) -> str:
    if value.hour == value.minute == value.second == value.microsecond == 0:
        return value.date().isoformat()
    return str(int(value.timestamp() * 1000))


def _default_lookback(*, timeframe: TimeFrame, limit: int) -> timedelta:
    if timeframe is TimeFrame.ONE_DAY:
        return timedelta(days=max(limit * 2, 30))
    if timeframe is TimeFrame.FOUR_HOUR:
        return timedelta(hours=max(limit * 4, 24))
    return timedelta(days=30)


def _normalize_polygon_aggregates(
    payload: dict[str, Any],
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
) -> tuple[MarketBar, ...]:
    raw_results = payload.get("results", [])
    if raw_results is None:
        return ()
    if not isinstance(raw_results, list):
        raise DataProviderError(
            "Polygon/Massive historical results were malformed",
            status=DataProviderStatus.DATA_CONFLICT,
        )
    bars: list[MarketBar] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        try:
            timestamp = datetime.fromtimestamp(
                float(Decimal(str(item["t"])) / Decimal("1000")), tz=UTC
            )
            volume = item.get("v")
            bars.append(
                MarketBar(
                    instrument=instrument,
                    timestamp=timestamp,
                    timeframe=timeframe,
                    open=Decimal(str(item["o"])),
                    high=Decimal(str(item["h"])),
                    low=Decimal(str(item["l"])),
                    close=Decimal(str(item["c"])),
                    volume=Decimal(str(volume)) if volume is not None else None,
                    currency=instrument.currency or Currency.USD,
                    source="polygon",
                )
            )
        except (KeyError, ValueError):
            continue
    return tuple(sorted(bars, key=lambda bar: bar.timestamp))


def _dedupe_bars(bars: tuple[MarketBar, ...]) -> tuple[MarketBar, ...]:
    by_timestamp: dict[datetime, MarketBar] = {}
    for bar in bars:
        existing = by_timestamp.get(bar.timestamp)
        if existing is None:
            by_timestamp[bar.timestamp] = bar
            continue
        if _bar_signature(existing) != _bar_signature(bar):
            raise DataProviderError(
                "Polygon/Massive duplicate timestamp contains conflicting OHLCV values",
                status=DataProviderStatus.DATA_CONFLICT,
            )
    return tuple(by_timestamp[timestamp] for timestamp in sorted(by_timestamp))


def _bar_signature(bar: MarketBar) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal | None]:
    return (bar.open, bar.high, bar.low, bar.close, bar.volume)


def _sanitize_provider_payload_endpoint(payload: dict[str, Any]) -> str | None:
    request_id = payload.get("request_id")
    if request_id is None:
        return None
    return f"polygon-request-id:{str(request_id)[:80]}"


def _safe_payload_message(payload: dict[str, Any]) -> str | None:
    for key in ("message", "error", "error_message"):
        value = payload.get(key)
        if value is not None:
            return str(value)[:240]
    return None
