"""Authenticated eToro reads using only documented official routes."""

from datetime import UTC, datetime
from typing import NoReturn
from urllib.parse import urlencode

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.http import (
    DisciplinedHttpClient,
    EtoroHttpFailureKind,
    HttpResponse,
    TransportError,
    classify_http_failure,
    diagnostic_headers,
)
from app.brokers.etoro.mapping import (
    SAFE_CLASSIFICATION_FIELDS,
    EtoroMappingError,
    map_demo_eligibility,
    map_demo_order_state,
    map_demo_portfolio,
    map_identity,
    map_instrument_resolution,
    map_portfolio,
    map_quote,
    map_universal_instrument_search,
)
from app.brokers.models import (
    BrokerIdentity,
    DemoEligibility,
    DemoPortfolioSnapshot,
    ExecutionState,
    InstrumentResolution,
)
from app.domain.enums import Currency
from app.domain.market import MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import UniversalInstrument

BASE = "https://public-api.etoro.com"
IDENTITY_PATH = "/api/v1/me"
DEMO_PORTFOLIO_PATH = "/api/v1/trading/info/demo/portfolio"
DEMO_AGGREGATE_PATH = "/api/v1/trading/info/demo/aggregate-portfolio"
DEMO_ELIGIBILITY_PATH = "/api/v2/trading/info/demo/eligibility"
DEMO_INSTRUMENT_BREAKDOWN_PATH = "/api/v2/trading/info/demo/instrument-breakdown"
REAL_PORTFOLIO_PATH = "/api/v1/trading/info/portfolio"
RATES_PATH = "/api/v1/market-data/instruments/rates"
SEARCH_PATH = "/api/v1/market-data/search"
INSTRUMENTS_PATH = "/api/v1/market-data/instruments"
INSTRUMENT_TYPES_PATH = "/api/v1/market-data/instrument-types"
CANDLE_HISTORY_PATH = (
    "/api/v1/market-data/instruments/"
    "{instrument_id}/history/candles/{direction}/{interval}/{candles_count}"
)
UNIVERSAL_SEARCH_FIELDS = ",".join(
    (
        "instrumentId",
        "displayname",
        "internalSymbolFull",
        *SAFE_CLASSIFICATION_FIELDS,
        "isOpen",
        "isExchangeOpen",
        "isCurrentlyTradable",
        "isBuyEnabled",
        "isHiddenFromClient",
        "isDelisted",
        "isActiveInPlatform",
        "currentRate",
    )
)


class EtoroApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        status: int | None = None,
        category: EtoroHttpFailureKind = EtoroHttpFailureKind.HTTP_ERROR,
        response_body: str | None = None,
        response_headers: dict[str, str] | None = None,
        transport_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.category = category
        self.response_body = response_body
        self.response_headers = response_headers or {}
        self.transport_detail = transport_detail

    def safe_metadata(self) -> dict[str, str]:
        metadata = {
            "category": self.category.value,
            "endpoint": self.endpoint,
        }
        if self.status is not None:
            metadata["http_status"] = str(self.status)
        if self.transport_detail is not None:
            metadata["transport_detail"] = self.transport_detail
        cf_ray = next(
            (value for key, value in self.response_headers.items() if key.casefold() == "cf-ray"),
            None,
        )
        if cf_ray is not None:
            metadata["cf_ray"] = cf_ray
        content_type = next(
            (
                value
                for key, value in self.response_headers.items()
                if key.casefold() == "content-type"
            ),
            None,
        )
        if content_type is not None:
            metadata["content_type"] = content_type
        return metadata


class EtoroReadClient:
    def __init__(self, credentials: EtoroCredentials, http: DisciplinedHttpClient) -> None:
        self._credentials = credentials
        self._http = http

    def _get_response(self, path: str) -> HttpResponse:
        try:
            response = self._http.get(BASE + path, self._credentials.headers())
        except TransportError as exc:
            self._raise_transport_error(path, "eToro read", exc)
        if response.status != 200:
            self._raise_response_error(response, path, "eToro read")
        return response

    def _get(self, path: str) -> object:
        response = self._get_response(path)
        return self._response_json(response, path)

    def _response_json(self, response: HttpResponse, path: str) -> object:
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise EtoroApiError("eToro returned invalid JSON", endpoint=path) from exc

    def _post_read(self, path: str, payload: dict[str, object]) -> object:
        try:
            response = self._http.post_read(BASE + path, self._credentials.headers(), payload)
        except TransportError as exc:
            self._raise_transport_error(path, "eToro read lookup", exc)
        if response.status != 200:
            self._raise_response_error(response, path, "eToro read lookup")
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise EtoroApiError("eToro returned invalid JSON", endpoint=path) from exc

    def identity(self) -> BrokerIdentity:
        return map_identity(self._get(IDENTITY_PATH))

    def identity_diagnostic_headers(self) -> dict[str, str]:
        response = self._get_response(IDENTITY_PATH)
        map_identity(self._response_json(response, IDENTITY_PATH))
        return diagnostic_headers(response)

    def demo_portfolio(
        self, symbols: dict[int, str], *, currency: Currency = Currency.USD
    ) -> PortfolioSnapshot:
        return map_portfolio(
            self._get(DEMO_PORTFOLIO_PATH),
            as_of=datetime.now(UTC),
            currency=currency,
            symbols=symbols,
        )

    def real_portfolio_read_only(
        self, symbols: dict[int, str], *, currency: Currency = Currency.USD
    ) -> PortfolioSnapshot:
        return map_portfolio(
            self._get(REAL_PORTFOLIO_PATH),
            as_of=datetime.now(UTC),
            currency=currency,
            symbols=symbols,
        )

    def quote(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> MarketQuote:
        query = urlencode({"instrumentIds": str(instrument_id)})
        return map_quote(
            self._get(f"{RATES_PATH}?{query}"),
            instrument_id=instrument_id,
            symbol=symbol,
            currency=currency,
        )

    def resolve_instrument(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> InstrumentResolution:
        query = urlencode(
            {
                "fields": UNIVERSAL_SEARCH_FIELDS,
                "internalSymbolFull": symbol,
                "pageSize": "10",
                "pageNumber": "1",
            }
        )
        return map_instrument_resolution(
            self._get(f"{SEARCH_PATH}?{query}"),
            symbol=symbol,
            as_of=as_of or datetime.now(UTC),
        )

    def raw_instrument_search(self, symbol: str) -> object:
        query = urlencode(
            {
                "fields": UNIVERSAL_SEARCH_FIELDS,
                "internalSymbolFull": symbol,
                "pageSize": "10",
                "pageNumber": "1",
            }
        )
        return self._get(f"{SEARCH_PATH}?{query}")

    def instrument_metadata(self, instrument_ids: tuple[int, ...]) -> dict[int, dict[str, object]]:
        if not instrument_ids:
            return {}
        query = urlencode({"instrumentIds": ",".join(str(item) for item in instrument_ids)})
        raw = self._get(f"{INSTRUMENTS_PATH}?{query}")
        return _extract_instrument_metadata(raw)

    def instrument_type_names(self) -> dict[int, str]:
        raw = self._get(INSTRUMENT_TYPES_PATH)
        return _extract_instrument_type_names(raw)

    def discover_instruments(
        self,
        *,
        as_of: datetime | None = None,
        page_size: int = 50,
        page_number: int = 1,
        search_text: str | None = None,
    ) -> tuple[UniversalInstrument, ...]:
        if page_size <= 0 or page_number <= 0:
            raise ValueError("page_size and page_number must be positive")
        query_parts: dict[str, str] = {
            "fields": UNIVERSAL_SEARCH_FIELDS,
            "pageSize": str(page_size),
            "pageNumber": str(page_number),
        }
        if search_text is not None:
            query_parts["searchText"] = search_text
        return map_universal_instrument_search(
            self._get(f"{SEARCH_PATH}?{urlencode(query_parts)}"),
            as_of=as_of or datetime.now(UTC),
            broker="etoro",
        )

    def demo_account(self, identity: BrokerIdentity) -> DemoPortfolioSnapshot:
        return map_demo_portfolio(self._get(DEMO_AGGREGATE_PATH), identity)

    def demo_eligibility(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> DemoEligibility:
        raw = self._post_read(
            DEMO_ELIGIBILITY_PATH,
            {
                "instrumentIds": [instrument_id],
                "symbols": [symbol],
                "currency": currency.value,
            },
        )
        return map_demo_eligibility(raw, instrument_id, symbol)

    def candle_history(
        self,
        *,
        instrument_id: int,
        direction: str,
        interval: str,
        candles_count: int,
    ) -> object:
        if direction not in {"asc", "desc"}:
            raise ValueError("candle history direction must be asc or desc")
        if not 1 <= candles_count <= 1000:
            raise ValueError("candle history count must be between 1 and 1000")
        path = CANDLE_HISTORY_PATH.format(
            instrument_id=instrument_id,
            direction=direction,
            interval=interval,
            candles_count=candles_count,
        )
        return self._get(path)

    def demo_order_state(
        self, identity: BrokerIdentity, instrument_id: int, order_id: str
    ) -> ExecutionState:
        headers = {**self._credentials.headers(), "CID": str(identity.demo_account_id)}
        query = urlencode({"instrumentIds": str(instrument_id)})
        try:
            response = self._http.get(BASE + f"{DEMO_INSTRUMENT_BREAKDOWN_PATH}?{query}", headers)
        except TransportError as exc:
            self._raise_transport_error(
                DEMO_INSTRUMENT_BREAKDOWN_PATH, "eToro Demo status read", exc
            )
        if response.status != 200:
            self._raise_response_error(
                response, DEMO_INSTRUMENT_BREAKDOWN_PATH, "eToro Demo status read"
            )
        try:
            return map_demo_order_state(response.json(), instrument_id, order_id)
        except (ValueError, UnicodeDecodeError, EtoroMappingError) as exc:
            raise EtoroApiError(
                "eToro Demo status could not be normalized",
                endpoint=DEMO_INSTRUMENT_BREAKDOWN_PATH,
            ) from exc

    @staticmethod
    def _raise_response_error(response: HttpResponse, endpoint: str, operation: str) -> None:
        category = classify_http_failure(response)
        body = response.body.decode("utf-8", errors="replace")
        raise EtoroApiError(
            f"{operation} failed with HTTP {response.status}: {category.value}",
            endpoint=endpoint,
            status=response.status,
            category=category,
            response_body=body,
            response_headers=diagnostic_headers(response),
        )

    @staticmethod
    def _raise_transport_error(endpoint: str, operation: str, exc: TransportError) -> NoReturn:
        raise EtoroApiError(
            f"{operation} failed before HTTP response: {exc.kind.value}",
            endpoint=endpoint,
            category=exc.kind,
            transport_detail=exc.detail.value,
        ) from exc


def _extract_instrument_metadata(raw: object) -> dict[int, dict[str, object]]:
    instruments: dict[int, dict[str, object]] = {}
    for item in _iter_payload_objects(raw):
        instrument_id = _int_from_any(item.get("instrumentId") or item.get("instrumentID"))
        if instrument_id is not None:
            instruments[instrument_id] = item
    return instruments


def _extract_instrument_type_names(raw: object) -> dict[int, str]:
    names: dict[int, str] = {}
    for item in _iter_payload_objects(raw):
        type_id = _int_from_any(
            item.get("instrumentTypeID")
            or item.get("instrumentTypeId")
            or item.get("id")
            or item.get("ID")
        )
        name = next(
            (
                value.strip()
                for key in (
                    "instrumentType",
                    "name",
                    "displayName",
                    "instrumentTypeName",
                    "internalAssetClassName",
                )
                if isinstance((value := item.get(key)), str) and value.strip()
            ),
            None,
        )
        if type_id is not None and name is not None:
            names[type_id] = name
    return names


def _iter_payload_objects(raw: object) -> tuple[dict[str, object], ...]:
    if isinstance(raw, list):
        return tuple(item for item in raw if isinstance(item, dict))
    if not isinstance(raw, dict):
        return ()
    direct = tuple(item for item in raw.values() if isinstance(item, list))
    for value in direct:
        objects = tuple(item for item in value if isinstance(item, dict))
        if objects:
            return objects
    return ()


def _int_from_any(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
