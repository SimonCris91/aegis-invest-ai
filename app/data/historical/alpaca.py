"""Read-only Alpaca historical market-data adapter."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.data.historical.providers import HttpTextTransport, UrllibTextTransport, _sanitize_url
from app.data.models import DataProviderError, DataProviderStatus
from app.domain.enums import AssetClass, Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame

_TIMEFRAMES = {
    TimeFrame.ONE_HOUR: "1Hour",
    TimeFrame.ONE_DAY: "1Day",
    TimeFrame.FOUR_HOUR: "4Hour",
}


class AlpacaHistoricalMarketDataProvider:
    """Provider-neutral Alpaca bars adapter.

    Stock/ETF reads use header authentication. Crypto historical bars can be
    queried without keys according to Alpaca's documented historical crypto
    exception, but credentials are still sent when configured.
    """

    provider_name = "alpaca"
    supported_timeframes = tuple(_TIMEFRAMES)

    def __init__(
        self,
        *,
        api_key_id: str | None = None,
        api_secret_key: str | None = None,
        transport: HttpTextTransport | None = None,
        base_url: str = "https://data.alpaca.markets",
        stock_feed: str = "sip",
        crypto_location: str = "us",
        max_pages: int = 3,
    ) -> None:
        self._api_key_id = api_key_id.strip() if api_key_id and api_key_id.strip() else None
        self._api_secret_key = (
            api_secret_key.strip() if api_secret_key and api_secret_key.strip() else None
        )
        self._transport = transport or UrllibTextTransport()
        self._base_url = base_url.rstrip("/")
        self._stock_feed = stock_feed.strip().lower()
        self._crypto_location = crypto_location.strip().lower()
        self._max_pages = max(1, max_pages)
        self._last_pagination_state: dict[str, object] = {
            "pagination_requested": False,
            "pagination_token_observed": False,
            "second_page_fetched": False,
            "pagination_verified": False,
            "pages_fetched": 0,
        }

    @property
    def last_pagination_state(self) -> dict[str, object]:
        return dict(self._last_pagination_state)

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
        limit: int = 10_000,
        max_pages: int | None = None,
    ) -> tuple[MarketBar, ...]:
        if timeframe not in _TIMEFRAMES:
            return ()
        if start >= end:
            raise DataProviderError(
                "historical range start must be before end",
                status=DataProviderStatus.DATA_INSUFFICIENT,
            )
        if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            self._require_stock_credentials()
        pages = max(1, max_pages or self._max_pages)
        url = self._bars_url(
            instrument=instrument,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
            page_token=None,
        )
        pagination_state: dict[str, object] = {
            "pagination_requested": pages > 1,
            "pagination_token_observed": False,
            "second_page_fetched": False,
            "pagination_verified": False,
            "pagination_truncated": False,
            "pages_fetched": 0,
        }
        self._last_pagination_state = dict(pagination_state)
        all_bars: list[MarketBar] = []
        for page_index in range(pages):
            payload = self._get_json(url, instrument=instrument)
            pagination_state["pages_fetched"] = page_index + 1
            if page_index > 0:
                pagination_state["second_page_fetched"] = True
            all_bars.extend(_normalize_alpaca_bars(payload, instrument, timeframe))
            token = payload.get("next_page_token")
            if not isinstance(token, str) or not token:
                break
            pagination_state["pagination_token_observed"] = True
            if page_index == pages - 1:
                pagination_state["pagination_truncated"] = True
                break
            url = self._bars_url(
                instrument=instrument,
                timeframe=timeframe,
                start=start,
                end=end,
                limit=limit,
                page_token=token,
            )
            self._last_pagination_state = dict(pagination_state)
        pagination_state["pagination_verified"] = (
            bool(pagination_state["pagination_requested"])
            and bool(pagination_state["pagination_token_observed"])
            and bool(pagination_state["second_page_fetched"])
        )
        self._last_pagination_state = dict(pagination_state)
        deduped = _dedupe_bars(tuple(all_bars))
        return tuple(bar for bar in deduped if start <= bar.timestamp <= end)

    def _bars_url(
        self,
        *,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime,
        limit: int,
        page_token: str | None,
    ) -> str:
        params = {
            "symbols": _provider_symbol(instrument),
            "timeframe": _TIMEFRAMES[timeframe],
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": str(min(max(1, limit), 10_000)),
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            params["feed"] = self._stock_feed
            path = "/v2/stocks/bars"
        elif instrument.asset_class is AssetClass.CRYPTO:
            path = f"/v1beta3/crypto/{self._crypto_location}/bars"
        else:
            raise DataProviderError(
                "Alpaca pilot supports only equity, ETF, and crypto historical bars",
                status=DataProviderStatus.MAPPING_AMBIGUOUS,
            )
        return f"{self._base_url}{path}?{urlencode(params)}"

    def _get_json(self, url: str, *, instrument: UniversalInstrument) -> dict[str, Any]:
        try:
            text = self._transport.get_text(url, self._headers(instrument))
            payload = json.loads(text)
        except DataProviderError:
            raise
        except TimeoutError as exc:
            raise DataProviderError(
                "Alpaca historical read timed out",
                status=DataProviderStatus.TIMEOUT,
            ) from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DataProviderError(
                "Alpaca historical read failed",
                status=DataProviderStatus.PROVIDER_UNAVAILABLE,
                sanitized_endpoint=_sanitize_url(url),
                transport_category=type(exc).__name__,
                exception_type=type(exc).__name__,
                exception_message=_safe_exception_message(exc),
                errno=getattr(exc, "errno", None),
                winerror=getattr(exc, "winerror", None),
            ) from exc
        if not isinstance(payload, dict):
            raise DataProviderError(
                "Alpaca historical response was not an object",
                status=DataProviderStatus.DATA_CONFLICT,
            )
        return payload

    def _headers(self, instrument: UniversalInstrument) -> dict[str, str]:
        headers = {"User-Agent": "AegisInvestAI/0.7"}
        if self._api_key_id and self._api_secret_key:
            headers["APCA-API-KEY-ID"] = self._api_key_id
            headers["APCA-API-SECRET-KEY"] = self._api_secret_key
        elif instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            self._require_stock_credentials()
        return headers

    def _require_stock_credentials(self) -> None:
        if self._api_key_id and self._api_secret_key:
            return
        raise DataProviderError(
            "Alpaca stock/ETF historical reads require API key ID and secret",
            status=DataProviderStatus.MAPPING_AMBIGUOUS,
        )


def _provider_symbol(instrument: UniversalInstrument) -> str:
    if instrument.asset_class is AssetClass.CRYPTO:
        return f"{instrument.symbol.upper()}/USD"
    return instrument.symbol.upper()


def _safe_exception_message(exc: BaseException) -> str:
    message = str(exc)
    return message.replace("\r", " ").replace("\n", " ")[:240]


def _default_lookback(*, timeframe: TimeFrame, limit: int) -> timedelta:
    if timeframe is TimeFrame.ONE_HOUR:
        return timedelta(hours=max(limit, 48))
    if timeframe is TimeFrame.ONE_DAY:
        return timedelta(days=max(limit * 2, 30))
    if timeframe is TimeFrame.FOUR_HOUR:
        return timedelta(hours=max(limit * 4, 24))
    return timedelta(days=30)


def _normalize_alpaca_bars(
    payload: dict[str, Any],
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
) -> tuple[MarketBar, ...]:
    container = payload.get("bars", {})
    if isinstance(container, dict):
        raw_results = container.get(_provider_symbol(instrument), [])
    elif isinstance(container, list):
        raw_results = container
    else:
        raw_results = []
    if raw_results is None:
        return ()
    if not isinstance(raw_results, list):
        raise DataProviderError(
            "Alpaca historical bars were malformed",
            status=DataProviderStatus.DATA_CONFLICT,
        )
    bars: list[MarketBar] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        try:
            timestamp = datetime.fromisoformat(str(item["t"]).replace("Z", "+00:00"))
            volume = item.get("v")
            bars.append(
                MarketBar(
                    instrument=instrument,
                    timestamp=timestamp.astimezone(UTC),
                    timeframe=timeframe,
                    open=Decimal(str(item["o"])),
                    high=Decimal(str(item["h"])),
                    low=Decimal(str(item["l"])),
                    close=Decimal(str(item["c"])),
                    volume=Decimal(str(volume)) if volume is not None else None,
                    currency=instrument.currency or Currency.USD,
                    source="alpaca",
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
                "Alpaca duplicate timestamp contains conflicting OHLCV values",
                status=DataProviderStatus.DATA_CONFLICT,
            )
    return tuple(by_timestamp[timestamp] for timestamp in sorted(by_timestamp))


def _bar_signature(bar: MarketBar) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal | None]:
    return (bar.open, bar.high, bar.low, bar.close, bar.volume)
