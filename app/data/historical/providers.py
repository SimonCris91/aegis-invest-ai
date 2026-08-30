"""External read-only historical data providers."""

import csv
import io
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from app.data.mapping import InstrumentMappingService
from app.data.models import DataProviderError, DataProviderStatus
from app.domain.enums import Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame


class HttpTextTransport(Protocol):
    def get_text(self, url: str, headers: dict[str, str]) -> str: ...


class UrllibTextTransport:
    def get_text(self, url: str, headers: dict[str, str]) -> str:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=20) as response:
                content = cast(bytes, response.read())
                return content.decode("utf-8")
        except HTTPError as exc:
            body = _read_http_error_body(exc)
            details = _safe_provider_error_details(body)
            status = _provider_status_from_http(exc.code)
            message = (
                "historical data provider rate limited"
                if status is DataProviderStatus.RATE_LIMITED
                else "historical data provider HTTP error"
            )
            raise DataProviderError(
                message,
                status=status,
                http_status=exc.code,
                sanitized_endpoint=_sanitize_url(url),
                transport_category=_http_transport_category(exc.code),
                provider_error_code=details.get("code"),
                provider_error_message=details.get("message"),
                retry_after=exc.headers.get("Retry-After"),
            ) from exc
        except TimeoutError as exc:
            raise DataProviderError(
                "historical data provider timed out",
                status=DataProviderStatus.TIMEOUT,
                sanitized_endpoint=_sanitize_url(url),
                transport_category="timeout",
            ) from exc
        except URLError as exc:
            category = _url_error_category(exc)
            raise DataProviderError(
                "historical data provider unavailable",
                status=DataProviderStatus.PROVIDER_UNAVAILABLE,
                sanitized_endpoint=_sanitize_url(url),
                transport_category=category,
            ) from exc


class StooqHistoricalDataProvider:
    """No-key public EOD adapter. Intraday requests return DATA_INSUFFICIENT."""

    provider_name = "stooq"
    supported_timeframes = (TimeFrame.ONE_DAY, TimeFrame.ONE_WEEK)

    def __init__(
        self,
        *,
        mapping_service: InstrumentMappingService | None = None,
        transport: HttpTextTransport | None = None,
    ) -> None:
        self._mapping_service = mapping_service or InstrumentMappingService()
        self._transport = transport or UrllibTextTransport()

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]:
        if timeframe not in self.supported_timeframes:
            return ()
        mapping = self._mapping_service.resolve(instrument, provider=self.provider_name)
        if not mapping.usable or mapping.selected is None:
            raise DataProviderError(
                "instrument mapping is unavailable",
                status=mapping.status,
            )
        query = urlencode({"s": mapping.selected.provider_symbol.lower(), "i": "d"})
        text = self._transport.get_text(
            f"https://stooq.com/q/d/l/?{query}",
            {"User-Agent": "AegisInvestAI/0.7"},
        )
        daily = _parse_stooq_csv(instrument, text, as_of=as_of)
        if timeframe is TimeFrame.ONE_WEEK:
            daily = _resample_weekly(daily)
        return daily[-limit:]


class JsonFixtureHistoricalDataProvider:
    provider_name = "fixture-json"
    supported_timeframes = (
        TimeFrame.INTRADAY,
        TimeFrame.ONE_HOUR,
        TimeFrame.FOUR_HOUR,
        TimeFrame.ONE_DAY,
        TimeFrame.ONE_WEEK,
    )

    def __init__(self, raw_payloads: dict[tuple[str, TimeFrame], str]) -> None:
        self._raw_payloads = raw_payloads

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]:
        raw = self._raw_payloads.get((instrument.key, timeframe))
        if raw is None:
            return ()
        data = cast(list[dict[str, Any]], json.loads(raw))
        bars = tuple(
            MarketBar(
                instrument=instrument,
                timestamp=datetime.fromisoformat(str(item["timestamp"])),
                timeframe=timeframe,
                open=Decimal(str(item["open"])),
                high=Decimal(str(item["high"])),
                low=Decimal(str(item["low"])),
                close=Decimal(str(item["close"])),
                volume=Decimal(str(item["volume"])) if item.get("volume") is not None else None,
                currency=instrument.currency or Currency.USD,
                source=self.provider_name,
            )
            for item in data
            if datetime.fromisoformat(str(item["timestamp"])) <= as_of
        )
        return bars[-limit:]


def _parse_stooq_csv(
    instrument: UniversalInstrument, text: str, *, as_of: datetime
) -> tuple[MarketBar, ...]:
    reader = csv.DictReader(io.StringIO(text))
    bars: list[MarketBar] = []
    for row in reader:
        if not row or row.get("Close") in {None, "No data"}:
            continue
        date_value = row.get("Date")
        open_value = row.get("Open")
        high_value = row.get("High")
        low_value = row.get("Low")
        close_value = row.get("Close")
        if (
            date_value is None
            or open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
        ):
            continue
        timestamp = datetime.fromisoformat(f"{date_value}T00:00:00+00:00")
        if timestamp > as_of:
            continue
        volume_value = row.get("Volume")
        volume = None if volume_value in {None, ""} else Decimal(str(volume_value))
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=timestamp,
                timeframe=TimeFrame.ONE_DAY,
                open=Decimal(open_value),
                high=Decimal(high_value),
                low=Decimal(low_value),
                close=Decimal(close_value),
                volume=volume,
                currency=instrument.currency or Currency.USD,
                source="stooq",
            )
        )
    return tuple(bars)


def _resample_weekly(daily: tuple[MarketBar, ...]) -> tuple[MarketBar, ...]:
    grouped: dict[tuple[int, int], list[MarketBar]] = {}
    for bar in daily:
        year, week, _ = bar.timestamp.isocalendar()
        grouped.setdefault((year, week), []).append(bar)
    weekly: list[MarketBar] = []
    for bars in grouped.values():
        ordered = sorted(bars, key=lambda bar: bar.timestamp)
        weekly.append(
            MarketBar(
                instrument=ordered[-1].instrument,
                timestamp=ordered[-1].timestamp,
                timeframe=TimeFrame.ONE_WEEK,
                open=ordered[0].open,
                high=max(bar.high for bar in ordered),
                low=min(bar.low for bar in ordered),
                close=ordered[-1].close,
                volume=_weekly_volume(ordered),
                currency=ordered[-1].currency,
                source="stooq",
            )
        )
    return tuple(sorted(weekly, key=lambda bar: bar.timestamp))


def _weekly_volume(ordered: list[MarketBar]) -> Decimal | None:
    if not all(bar.volume is not None for bar in ordered):
        return None
    return sum((bar.volume for bar in ordered if bar.volume is not None), Decimal("0"))


def _provider_status_from_http(status: int) -> DataProviderStatus:
    if status == 429:
        return DataProviderStatus.RATE_LIMITED
    if status in {401, 403, 404} or status >= 500:
        return DataProviderStatus.PROVIDER_UNAVAILABLE
    return DataProviderStatus.PROVIDER_UNAVAILABLE


def _http_transport_category(status: int) -> str:
    if status == 401:
        return "HTTP_401_UNAUTHORIZED"
    if status == 403:
        return "HTTP_403_FORBIDDEN"
    if status == 404:
        return "HTTP_404_NOT_FOUND"
    if status == 429:
        return "HTTP_429_RATE_LIMITED"
    if status >= 500:
        return "HTTP_5XX_PROVIDER_ERROR"
    return f"HTTP_{status}"


def _read_http_error_body(exc: HTTPError) -> str:
    try:
        raw = exc.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")[:1000]


def _safe_provider_error_details(body: str) -> dict[str, str | None]:
    if not body:
        return {"code": None, "message": None}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {"code": None, "message": body[:240]}
    if not isinstance(parsed, dict):
        return {"code": None, "message": None}
    code = parsed.get("code") or parsed.get("status") or parsed.get("error")
    message = parsed.get("message") or parsed.get("error_message") or parsed.get("detail")
    return {
        "code": str(code)[:120] if code is not None else None,
        "message": str(message)[:240] if message is not None else None,
    }


def _sanitize_url(url: str) -> str:
    parsed = urlparse(url)
    safe_query_parts: list[str] = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        key = part.split("=", maxsplit=1)[0]
        if key.lower() in {"apikey", "api_key", "token", "authorization", "auth"}:
            safe_query_parts.append(f"{key}=<redacted>")
        else:
            safe_query_parts.append(part)
    return urlunparse(parsed._replace(query="&".join(safe_query_parts)))


def _url_error_category(exc: URLError) -> str:
    reason = getattr(exc, "reason", None)
    reason_text = str(reason).lower()
    if "name or service not known" in reason_text or "getaddrinfo" in reason_text:
        return "DNS"
    if "ssl" in reason_text or "certificate" in reason_text or "tls" in reason_text:
        return "TLS"
    if "timed out" in reason_text or "timeout" in reason_text:
        return "timeout"
    if "connection reset" in reason_text:
        return "connection_reset"
    if "proxy" in reason_text:
        return "proxy_network_policy"
    return type(reason).__name__ if reason is not None else "transport_unavailable"
