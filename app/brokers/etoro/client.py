"""Authenticated eToro reads using only documented official routes."""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from time import time_ns
from typing import NoReturn
from urllib.parse import quote, urlencode

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
    map_demo_order_lookup_state,
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
DEMO_PNL_PATH = "/api/v1/trading/info/demo/pnl"
DEMO_ELIGIBILITY_PATH = "/api/v2/trading/info/demo/eligibility"
DEMO_INSTRUMENT_BREAKDOWN_PATH = "/api/v2/trading/info/demo/instrument-breakdown"
DEMO_ORDER_LOOKUP_PATH = "/api/v2/trading/info/demo/orders:lookup"
DEMO_CLOSE_ORDER_PATH = "/api/v1/trading/info/demo/close-orders"
DEMO_TRADE_HISTORY_PATH = "/api/v1/trading/info/trade/demo/history"
REAL_PORTFOLIO_PATH = "/api/v1/trading/info/portfolio"
RATES_PATH = "/api/v1/market-data/instruments/rates"
SEARCH_PATH = "/api/v1/market-data/search"
INSTRUMENTS_PATH = "/api/v1/market-data/instruments"
INSTRUMENT_TYPES_PATH = "/api/v1/market-data/instrument-types"
CANDLE_HISTORY_PATH = (
    "/api/v1/market-data/instruments/"
    "{instrument_id}/history/candles/{direction}/{interval}/{candles_count}"
)
PUBLIC_PORTFOLIO_GAIN_PATH = "/api/v2/portfolios/{username}/gain/{granularity}"
PUBLIC_PORTFOLIO_COPIERS_PATH = "/api/v2/portfolios/{username}/copiers"
PUBLIC_PORTFOLIO_ASSETS_PATH = "/api/v2/portfolios/{username}/assets/history"
PUBLIC_PORTFOLIO_EXPOSURE_PATH = "/api/v2/portfolios/{username}/exposure/history"
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
        "isInternalInstrument",
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
        self._last_server_now: datetime | None = None

    @property
    def last_server_now(self) -> datetime | None:
        return self._last_server_now

    def _get_response(
        self, path: str, *, extra_headers: dict[str, str] | None = None
    ) -> HttpResponse:
        try:
            headers = {**self._credentials.headers(), **(extra_headers or {})}
            response = self._http.get(BASE + path, headers)
        except TransportError as exc:
            self._raise_transport_error(path, "eToro read", exc)
        if response.status != 200:
            self._raise_response_error(response, path, "eToro read")
        return response

    def _get(self, path: str, *, extra_headers: dict[str, str] | None = None) -> object:
        response = self._get_response(path, extra_headers=extra_headers)
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

    def demo_portfolio_payload(self) -> object:
        return self._get(DEMO_PORTFOLIO_PATH)

    def demo_pnl_payload(self) -> object:
        """Return documented per-position Demo P/L and broker position IDs."""
        return self._get(DEMO_PNL_PATH)

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
        quote, _, _ = self.quote_with_diagnostics(instrument_id, symbol, currency=currency)
        return quote

    def quote_with_diagnostics(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> tuple[MarketQuote, tuple[object, ...], datetime | None]:
        # Prevent an intermediary from replaying a stale cached rates response.
        query = urlencode(
            {"instrumentIds": str(instrument_id), "_aegis_quote_refresh": str(time_ns())}
        )
        response = self._get_response(
            f"{RATES_PATH}?{query}",
            extra_headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        raw = self._response_json(response, RATES_PATH)
        self._last_server_now = _http_server_datetime(response.headers)
        if not isinstance(raw, dict) or not isinstance(raw.get("rates"), list):
            raise EtoroApiError("invalid rate payload", endpoint=RATES_PATH)
        dates = tuple(
            row.get("date")
            for row in raw["rates"]
            if isinstance(row, dict) and str(row.get("instrumentID")) == str(instrument_id)
        )
        return (
            map_quote(
                raw,
                instrument_id=instrument_id,
                symbol=symbol,
                currency=currency,
            ),
            dates,
            self._last_server_now,
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

    def resolve_instrument_id(
        self, instrument_id: int, *, symbol: str, as_of: datetime | None = None
    ) -> InstrumentResolution:
        """Resolve session metadata by the authoritative broker instrument ID."""
        if instrument_id <= 0:
            raise ValueError("instrument_id must be positive")
        query = urlencode(
            {
                "fields": UNIVERSAL_SEARCH_FIELDS,
                "instrumentId": str(instrument_id),
                "pageSize": "10",
                "pageNumber": "1",
            }
        )
        return map_instrument_resolution(
            self._get(f"{SEARCH_PATH}?{query}"),
            symbol=symbol,
            as_of=as_of or datetime.now(UTC),
            expected_instrument_id=instrument_id,
        )

    def resolve_session_instrument_id(
        self, instrument_id: int, *, symbol: str, as_of: datetime | None = None
    ) -> InstrumentResolution:
        """Recover delisted session evidence without weakening execution lookup."""
        try:
            return self.resolve_instrument_id(instrument_id, symbol=symbol, as_of=as_of)
        except EtoroMappingError as exc:
            if str(exc) != "instrument symbol was not resolved exactly":
                raise
        query = urlencode({
            "fields": UNIVERSAL_SEARCH_FIELDS,
            "instrumentId": str(instrument_id),
            "isDelisted": "true",
            "pageSize": "10",
            "pageNumber": "1",
        })
        # One physical request. Never retry a diagnostic fallback on throttling.
        path = f"{SEARCH_PATH}?{query}"
        try:
            response = self._http.get_once(BASE + path, self._credentials.headers())
        except TransportError as exc:
            self._raise_transport_error(path, "eToro delisted session lookup", exc)
        if response.status != 200:
            self._raise_response_error(response, path, "eToro delisted session lookup")
        resolution = map_instrument_resolution(
            self._response_json(response, path), symbol=symbol,
            as_of=as_of or datetime.now(UTC), expected_instrument_id=instrument_id,
        )
        if resolution.is_delisted is not True:
            raise EtoroMappingError("delisted session lookup did not confirm delisting")
        return resolution

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

    def instrument_display_data(self) -> object:
        """Read the unfiltered official instrument display catalog."""
        return self._get(INSTRUMENTS_PATH)

    def public_portfolio_gain(
        self, username: str, *, granularity: str = "monthly", count: int = 60
    ) -> object:
        """Read a public profile's return history; this endpoint never writes."""
        path = _public_profile_path(PUBLIC_PORTFOLIO_GAIN_PATH, username, granularity=granularity)
        query = urlencode({"count": str(count)})
        return self._get(f"{path}?{query}")

    def public_portfolio_copiers(self, username: str) -> object:
        """Read public copier statistics for one profile."""
        return self._get(_public_profile_path(PUBLIC_PORTFOLIO_COPIERS_PATH, username))

    def public_portfolio_assets(self, username: str, *, period: str = "LastTwoYears") -> object:
        """Read public asset allocation history for one profile."""
        path = _public_profile_path(PUBLIC_PORTFOLIO_ASSETS_PATH, username)
        return self._get(f"{path}?{urlencode({'period': period})}")

    def public_portfolio_exposure(self, username: str, *, period: str = "LastTwoYears") -> object:
        """Read public exposure history for one profile."""
        path = _public_profile_path(PUBLIC_PORTFOLIO_EXPOSURE_PATH, username)
        return self._get(f"{path}?{urlencode({'period': period})}")

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

    def session_catalog_page(self, *, page_number: int, page_size: int = 100) -> object:
        if page_number <= 0 or page_size <= 0:
            raise ValueError("pagination must be positive")
        query = urlencode(
            {
                "fields": UNIVERSAL_SEARCH_FIELDS,
                "pageSize": page_size,
                "pageNumber": page_number,
            }
        )
        return self._get(f"{SEARCH_PATH}?{query}")

    def session_catalog_ids(self, instrument_ids: tuple[int, ...]) -> object:
        """Exact single-ID search; this endpoint has no instrumentIds bulk filter."""
        if len(instrument_ids) != 1 or instrument_ids[0] <= 0:
            raise ValueError("session lookup requires exactly one positive instrument ID")
        query = urlencode(
            {
                "fields": UNIVERSAL_SEARCH_FIELDS,
                "instrumentId": str(instrument_ids[0]),
                "pageNumber": 1,
                "pageSize": 100,
            }
        )
        path = f"{SEARCH_PATH}?{query}"
        try:
            response = self._http.get_once(BASE + path, self._credentials.headers())
        except TransportError as exc:
            self._raise_transport_error(path, "eToro session lookup", exc)
        if response.status != 200:
            self._raise_response_error(response, path, "eToro session lookup")
        return self._response_json(response, path)

    def demo_account(self, identity: BrokerIdentity) -> DemoPortfolioSnapshot:
        return map_demo_portfolio(self._get(DEMO_AGGREGATE_PATH), identity)

    def demo_eligibility(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> DemoEligibility:
        response = self.demo_eligibility_response(instrument_id, symbol, currency=currency)
        if response.status != 200:
            self._raise_response_error(response, DEMO_ELIGIBILITY_PATH, "eToro eligibility lookup")
        raw = self._response_json(response, DEMO_ELIGIBILITY_PATH)
        return map_demo_eligibility(raw, instrument_id, symbol)

    def demo_eligibility_response(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> HttpResponse:
        """Informational POST shared with diagnostics; return HTTP errors unmapped."""
        try:
            return self._http.post_read(
                BASE + DEMO_ELIGIBILITY_PATH,
                self._credentials.headers(),
                {
                    "instrumentIds": [instrument_id],
                    "symbols": [symbol],
                    "currency": currency.value,
                },
            )
        except TransportError as exc:
            self._raise_transport_error(DEMO_ELIGIBILITY_PATH, "eToro eligibility lookup", exc)

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
        raw: object | None = None
        try:
            raw = response.json()
            return map_demo_order_state(raw, instrument_id, order_id)
        except (ValueError, UnicodeDecodeError, EtoroMappingError) as exc:
            # eToro's instrument-breakdown endpoint exposes active orders, but
            # can omit an order immediately after it has been filled.  In that
            # case the aggregate Demo portfolio is the authoritative read-only
            # evidence: a new position for the requested instrument means the
            # market order reached the account even though its order row is no
            # longer present.  Only use this fallback for a structurally valid
            # breakdown containing the requested instrument; malformed payloads
            # must remain fail-closed.
            if isinstance(exc, EtoroMappingError) and _has_instrument_breakdown(
                raw, instrument_id
            ):
                try:
                    portfolio = self.demo_account(identity)
                except (EtoroApiError, RuntimeError, ValueError):
                    raise EtoroApiError(
                        "eToro Demo status could not be normalized",
                        endpoint=DEMO_INSTRUMENT_BREAKDOWN_PATH,
                    ) from exc
                if any(
                    position.instrument_id == instrument_id
                    and position.current_exposure > 0
                    for position in portfolio.positions
                ):
                    return ExecutionState.FILLED
            raise EtoroApiError(
                "eToro Demo status could not be normalized",
                endpoint=DEMO_INSTRUMENT_BREAKDOWN_PATH,
            ) from exc

    def demo_order_lookup(
        self, order_id: str, *, reference_id: str | None = None
    ) -> ExecutionState:
        """Read one Demo order through eToro's order-level lookup endpoint.

        This is read-only and intentionally separate from instrument-breakdown:
        a filled order can disappear from the active instrument view while its
        authoritative order record remains queryable here.
        """
        details = self.demo_order_lookup_details(order_id, reference_id=reference_id)
        state = details["state"]
        if not isinstance(state, ExecutionState):
            raise EtoroMappingError("Demo order lookup state is invalid")
        return state

    def demo_close_order_position_affected(
        self, order_id: str, position_id: str
    ) -> bool:
        """Verify a Demo close order against its exact position, read-only.

        Close-order IDs are not served by the open-order ``orders:lookup``
        endpoint.  A response without the expected position is not evidence
        of execution; callers must not use it to replay a close.
        """
        if not order_id.isdigit() or not position_id.isdigit():
            raise ValueError("numeric close order and position IDs are required")
        raw = self._get(f"{DEMO_CLOSE_ORDER_PATH}/{order_id}")
        if not isinstance(raw, dict) or str(raw.get("orderID")) != order_id:
            raise EtoroMappingError("Demo close-order identity mismatch")
        positions = raw.get("positions")
        if not isinstance(positions, list):
            raise EtoroMappingError("Demo close-order positions unavailable")
        observed_ids = {
            str(row.get("positionID")) for row in positions if isinstance(row, dict)
        }
        if observed_ids and position_id not in observed_ids:
            raise EtoroMappingError("Demo close-order position mismatch")
        if raw.get("errorCode") not in (None, 0, "0"):
            return False
        return position_id in observed_ids

    def demo_closed_trade_by_position(
        self, position_id: str, *, min_date: date, instrument_id: int
    ) -> dict[str, str] | None:
        """Find an exact closed Demo position in bounded broker history."""
        if not position_id.isdigit() or instrument_id <= 0:
            raise ValueError("valid position and instrument IDs are required")
        matches: list[dict[str, object]] = []
        for page in range(1, 6):
            query = urlencode({"minDate": min_date.isoformat(), "page": page, "pageSize": 100})
            raw = self._get(f"{DEMO_TRADE_HISTORY_PATH}?{query}")
            if not isinstance(raw, list):
                raise EtoroMappingError("Demo trading history is not a list")
            matches.extend(
                row for row in raw
                if isinstance(row, dict)
                and str(row.get("positionId")) == position_id
                and str(row.get("instrumentId")) == str(instrument_id)
            )
            if len(matches) > 1:
                raise EtoroMappingError("Demo position has ambiguous history rows")
            if len(raw) < 100:
                break
        if not matches:
            return None
        row = matches[0]
        result = {"position_id": position_id}
        try:
            profit = Decimal(str(row.get("netProfit")))
        except (InvalidOperation, TypeError, ValueError):
            profit = None
        if profit is not None and profit.is_finite():
            result["realized_pnl_account_currency"] = str(profit)
        timestamp = row.get("closeTimestamp")
        if isinstance(timestamp, str):
            try:
                closed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                closed_at = None
            if closed_at is not None and closed_at.tzinfo is not None:
                result["closed_at"] = closed_at.isoformat()
        return result

    def demo_order_lookup_details(
        self, order_id: str, *, reference_id: str | None = None
    ) -> dict[str, object]:
        """Return only safe state and uniquely attributable open-position data.

        eToro's official order lookup includes per-order position executions.
        Keep account/customer identifiers out of the returned projection and
        expose a position ID only when exactly one open execution is present.
        """
        if (not order_id and not reference_id) or (order_id and reference_id):
            raise ValueError("provide exactly one of order_id or reference_id")
        identifier = {"referenceId": reference_id} if reference_id else {"orderId": order_id}
        path = f"{DEMO_ORDER_LOOKUP_PATH}?{urlencode(identifier)}"
        raw = self._get(path)
        if not isinstance(raw, dict):
            raise EtoroMappingError("Demo order lookup payload must be an object")
        state = map_demo_order_lookup_state(raw, order_id)
        result: dict[str, object] = {
            "state": state,
            "position_executions_count": 0,
        }
        raw_executions = raw.get("positionExecutions")
        if not isinstance(raw_executions, list):
            return result
        open_executions = [
            row
            for row in raw_executions
            if isinstance(row, dict)
            and str(row.get("state", "")).casefold() in {"open", "opened"}
            and str(row.get("positionId", "")).isdigit()
        ]
        result["position_executions_count"] = len(raw_executions)
        if state is not ExecutionState.FILLED or len(open_executions) != 1:
            return result
        execution = open_executions[0]
        result["position_id"] = str(execution["positionId"])
        exposure = execution.get("initialExposureAccountCurrency")
        if exposure is None:
            exposure = execution.get("investedAmountCurrency")
        try:
            parsed_exposure = Decimal(str(exposure))
        except (InvalidOperation, TypeError, ValueError):
            return result
        if parsed_exposure.is_finite() and parsed_exposure > 0:
            result["executed_exposure_account_currency"] = str(parsed_exposure)
        return result

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


def _has_instrument_breakdown(raw: object, instrument_id: int) -> bool:
    if not isinstance(raw, dict):
        return False
    instruments = raw.get("instruments")
    if not isinstance(instruments, list):
        return False
    return any(
        isinstance(item, dict) and item.get("instrumentId") == instrument_id
        for item in instruments
    )


def _public_profile_path(template: str, username: str, **values: str) -> str:
    if not isinstance(username, str) or not username or len(username) > 50:
        raise ValueError("public eToro username is invalid")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in username):
        raise ValueError("public eToro username is invalid")
    if "granularity" in values and values["granularity"] not in {"daily", "monthly", "yearly"}:
        raise ValueError("public gain granularity is invalid")
    return template.format(username=quote(username, safe=""), **values)


def _extract_instrument_metadata(raw: object) -> dict[int, dict[str, object]]:
    instruments: dict[int, dict[str, object]] = {}
    for item in _iter_payload_objects(raw):
        instrument_id = _int_from_any(item.get("instrumentId") or item.get("instrumentID"))
        if instrument_id is not None:
            instruments[instrument_id] = item
    return instruments


def _http_server_datetime(headers: dict[str, str]) -> datetime | None:
    raw_date = next((value for key, value in headers.items() if key.casefold() == "date"), None)
    if raw_date is None:
        return None
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


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
