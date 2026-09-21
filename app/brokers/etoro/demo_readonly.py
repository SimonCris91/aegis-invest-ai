"""Read-only eToro Demo portfolio adapter for external Demo state ingestion."""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator

from app.brokers.etoro.client import DEMO_PORTFOLIO_PATH, EtoroApiError, EtoroReadClient
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import Currency, OperatingMode, SettlementType, TradeSide


class EtoroDemoReadOnlyError(RuntimeError):
    pass


class EtoroDemoReadStatus(StrEnum):
    OK = "OK"
    AUTH_FAILED = "AUTH_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    REAL_ENVIRONMENT_REJECTED = "REAL_ENVIRONMENT_REJECTED"


class EtoroDemoReadOnlyPosition(FrozenDomainModel):
    instrument_id: int | None = Field(default=None, gt=0)
    symbol: str | None = Field(default=None, min_length=1)
    position_id: str | None = Field(default=None, min_length=1)
    side: TradeSide | None = None
    settlement_type: SettlementType | None = None
    units: Decimal | None = Field(default=None, ge=0)
    average_open_price: Decimal | None = Field(default=None, gt=0)
    current_value: Decimal | None = Field(default=None, ge=0)
    current_price: Decimal | None = Field(default=None, gt=0)
    unrealized_pnl: Decimal | None = None
    leverage: Decimal | None = Field(default=None, ge=0)


class EtoroDemoReadOnlyPortfolio(FrozenDomainModel):
    environment: OperatingMode
    endpoint: str = DEMO_PORTFOLIO_PATH
    as_of: datetime
    currency: Currency | None = None
    available_cash: Decimal | None = Field(default=None, ge=0)
    buying_power: Decimal | None = Field(default=None, ge=0)
    equity: Decimal | None = Field(default=None, ge=0)
    account_value: Decimal | None = Field(default=None, ge=0)
    open_positions: tuple[EtoroDemoReadOnlyPosition, ...] = ()
    provider: str = "etoro"
    provenance: str = "etoro-demo-portfolio-read-only-v1"
    broker_write_calls: int = 0

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class EtoroDemoReadOnlyResult(FrozenDomainModel):
    status: EtoroDemoReadStatus
    endpoint: str = DEMO_PORTFOLIO_PATH
    http_status: int | None = None
    portfolio: EtoroDemoReadOnlyPortfolio | None = None
    sanitized_error: dict[str, str] | None = None
    write_request_sent: bool = False
    broker_write_calls: int = 0


class EtoroDemoReadOnlyAdapter:
    """Consumes only the explicit eToro Demo portfolio read endpoint."""

    def __init__(self, client: EtoroReadClient, *, environment: OperatingMode) -> None:
        if environment is not OperatingMode.ETORO_DEMO:
            raise EtoroDemoReadOnlyError("eToro Demo read adapter requires ETORO_DEMO environment")
        self._client = client
        self.environment = environment
        self.endpoint = DEMO_PORTFOLIO_PATH
        self.broker_write_calls = 0

    def read_portfolio(self) -> EtoroDemoReadOnlyResult:
        try:
            raw = self._client.demo_portfolio_payload()
        except EtoroApiError as exc:
            return EtoroDemoReadOnlyResult(
                status=_status_from_api_error(exc),
                http_status=exc.status,
                sanitized_error=exc.safe_metadata(),
                broker_write_calls=self.broker_write_calls,
            )
        except (RuntimeError, ValueError) as exc:
            return EtoroDemoReadOnlyResult(
                status=EtoroDemoReadStatus.PROVIDER_UNAVAILABLE,
                sanitized_error={"category": type(exc).__name__},
                broker_write_calls=self.broker_write_calls,
            )

        try:
            portfolio = normalize_demo_portfolio_payload(raw, environment=self.environment)
        except (TypeError, ValueError) as exc:
            return EtoroDemoReadOnlyResult(
                status=EtoroDemoReadStatus.MALFORMED_RESPONSE,
                sanitized_error={"category": type(exc).__name__},
                broker_write_calls=self.broker_write_calls,
            )
        return EtoroDemoReadOnlyResult(
            status=EtoroDemoReadStatus.OK,
            http_status=200,
            portfolio=portfolio,
            broker_write_calls=self.broker_write_calls,
        )


def normalize_demo_portfolio_payload(
    raw: object, *, environment: OperatingMode
) -> EtoroDemoReadOnlyPortfolio:
    if environment is not OperatingMode.ETORO_DEMO:
        raise EtoroDemoReadOnlyError("Demo portfolio normalization requires ETORO_DEMO")
    if not isinstance(raw, Mapping):
        raise TypeError("Demo portfolio payload must be an object")

    client_portfolio = raw.get("clientPortfolio")
    if client_portfolio is not None and not isinstance(client_portfolio, Mapping):
        raise TypeError("clientPortfolio must be an object when present")
    source = client_portfolio if isinstance(client_portfolio, Mapping) else raw

    return EtoroDemoReadOnlyPortfolio(
        environment=environment,
        as_of=_payload_timestamp(raw),
        currency=_optional_currency(
            _first_present(source, raw, keys=("currency", "accountCurrency"))
        ),
        available_cash=_optional_decimal(
            _first_present(
                source, raw, keys=("availableCash", "accountAvailableCash", "credit", "cash")
            )
        ),
        buying_power=_optional_decimal(
            _first_present(source, raw, keys=("buyingPower", "availableToWithdraw", "credit"))
        ),
        equity=_optional_decimal(
            _first_present(source, raw, keys=("equity", "totalEquity", "accountTotalValue"))
        ),
        account_value=_optional_decimal(
            _first_present(source, raw, keys=("accountValue", "totalValue", "accountTotalValue"))
        ),
        open_positions=tuple(_normalize_position(item) for item in _positions(source)),
    )


def _status_from_api_error(exc: EtoroApiError) -> EtoroDemoReadStatus:
    if exc.status in {401, 403}:
        return EtoroDemoReadStatus.AUTH_FAILED
    return EtoroDemoReadStatus.PROVIDER_UNAVAILABLE


def _payload_timestamp(raw: Mapping[object, object]) -> datetime:
    raw_timestamp = raw.get("timestamp") or raw.get("asOf") or raw.get("as_of")
    if raw_timestamp is None:
        return datetime.now(UTC)
    text = str(raw_timestamp).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positions(source: Mapping[object, object]) -> tuple[object, ...]:
    raw_positions = _first_present(
        source, keys=("positions", "openPositions", "instrumentAggregates")
    )
    if raw_positions is None:
        raw_positions = []
    if not isinstance(raw_positions, list):
        raise TypeError("Demo portfolio positions must be a list when present")
    return tuple(raw_positions)


def _normalize_position(raw: object) -> EtoroDemoReadOnlyPosition:
    if not isinstance(raw, Mapping):
        raise TypeError("Demo portfolio position must be an object")
    units = _optional_decimal(_first_present(raw, keys=("units", "netUnits", "quantity")))
    return EtoroDemoReadOnlyPosition(
        instrument_id=_optional_int(_first_present(raw, keys=("instrumentID", "instrumentId"))),
        symbol=_optional_str(_first_present(raw, keys=("symbol", "internalSymbolFull"))),
        position_id=_optional_str(_first_present(raw, keys=("positionID", "positionId", "id"))),
        side=_side(raw, units),
        settlement_type=_settlement_type(raw),
        units=abs(units) if units is not None else None,
        average_open_price=_optional_decimal(
            _first_present(raw, keys=("openRate", "avgOpenRate", "averageOpenPrice"))
        ),
        current_value=_optional_decimal(
            _first_present(
                raw,
                keys=(
                    "currentValue",
                    "currentExposure",
                    "netCurrentExposureAccountCurrency",
                ),
            )
        ),
        current_price=_optional_decimal(_first_present(raw, keys=("currentRate", "marketPrice"))),
        unrealized_pnl=_optional_decimal(
            _first_present(
                raw,
                keys=("unrealizedPnl", "accountCurrencyReturn", "pnlAccountCurrency"),
            )
        ),
        leverage=_optional_decimal(_first_present(raw, keys=("leverage", "avgLeverage"))),
    )


def _first_present(*sources: Mapping[object, object], keys: tuple[str, ...]) -> object | None:
    for source in sources:
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
    return None


def _optional_decimal(value: object | None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _optional_int(value: object | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(str(value))


def _optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_currency(value: object | None) -> Currency | None:
    text = _optional_str(value)
    if text is None:
        return None
    return Currency(text.upper())


def _side(raw: Mapping[object, object], units: Decimal | None) -> TradeSide | None:
    is_buy = raw.get("isBuy")
    if isinstance(is_buy, bool):
        return TradeSide.BUY if is_buy else TradeSide.SELL
    if units is None:
        return None
    return TradeSide.BUY if units >= 0 else TradeSide.SELL


def _settlement_type(raw: Mapping[object, object]) -> SettlementType | None:
    raw_value = _optional_str(raw.get("settlementType"))
    if raw_value is None:
        return None
    return SettlementType(raw_value)
