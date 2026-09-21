"""Normalized broker models; provider payloads never cross this boundary."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import Currency, MarketStatus, OperatingMode, SettlementType, TradeSide


class ExecutionState(StrEnum):
    SUBMITTED = "SUBMITTED"
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class BrokerCapabilities(FrozenDomainModel):
    provider: str = Field(min_length=1)
    mode: OperatingMode
    authenticated_reads: bool
    demo_execution: bool
    real_execution: bool = False


class BrokerIdentity(FrozenDomainModel):
    stable_user_id: str = Field(min_length=1)
    demo_account_id: int = Field(gt=0)
    real_account_id: int = Field(gt=0)
    username: str | None = Field(default=None, min_length=1, repr=False, exclude=True)
    scopes: tuple[str, ...] = ()

    @property
    def redacted_reference(self) -> str:
        return f"user:{self.stable_user_id[-4:]}"


class AccountKind(StrEnum):
    REAL = "REAL"
    DEMO = "DEMO"


class TrackRecordKind(StrEnum):
    SHADOW = "SHADOW"
    OFFLINE_PAPER = "OFFLINE_PAPER"
    ETORO_DEMO = "ETORO_DEMO"
    REAL_ACCOUNT_OBSERVATION = "REAL_ACCOUNT_OBSERVATION"


class BrokerAccountContext(FrozenDomainModel):
    stable_user_id: str = Field(min_length=1)
    account_id: int = Field(gt=0)
    kind: AccountKind


class InstrumentResolution(FrozenDomainModel):
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    internal_symbol_full: str = Field(min_length=1)
    display_name: str | None = Field(default=None, min_length=1)
    instrument_type: str | None = Field(default=None, min_length=1)
    classification_metadata: dict[str, str | int | bool] = Field(default_factory=dict)
    classification_evidence_source: str = "search"
    classification_status: str = "UNCLASSIFIED"
    market_status: MarketStatus
    is_exchange_open: bool | None = None
    is_open: bool | None = None
    is_currently_tradable: bool | None
    is_buy_enabled: bool | None
    is_internal_instrument: bool | None = None
    is_hidden_from_client: bool | None
    is_delisted: bool | None
    is_active_in_platform: bool | None
    current_rate: Decimal | None = Field(default=None, gt=0)
    resolved: bool
    structurally_supported: bool
    structural_status: str = Field(min_length=1)
    verified: bool
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def resolution_as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class DemoPortfolioPosition(FrozenDomainModel):
    instrument_id: int = Field(gt=0)
    asset_currency: Currency
    side: TradeSide
    units: Decimal = Field(ge=0)
    current_exposure: Decimal = Field(ge=0)
    initial_exposure: Decimal = Field(ge=0)
    unrealized_pnl_account_currency: Decimal
    unrealized_pnl_asset_currency: Decimal
    leverage: Decimal = Field(ge=0)
    average_open_rate: Decimal = Field(gt=0)


class DemoPortfolioSnapshot(FrozenDomainModel):
    context: BrokerAccountContext
    as_of: datetime
    currency: Currency
    cash: Decimal = Field(ge=0)
    total_value: Decimal = Field(ge=0)
    current_pnl: Decimal = Decimal("0")
    account_balance: Decimal = Field(default=Decimal("0"), ge=0)
    positions: tuple[DemoPortfolioPosition, ...] = ()
    position_ids: tuple[str, ...] = ()

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class DemoEligibility(FrozenDomainModel):
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    currency: Currency
    minimum_position: Decimal = Field(gt=0)
    allow_open: bool
    allow_close: bool | None = None
    max_units_per_order: Decimal | None = Field(default=None, gt=0)
    allowed_order_quantity_types: tuple[str, ...] = ()
    settlement_type: SettlementType
    leverage: int = Field(ge=1)
    verified: bool


class FxRate(FrozenDomainModel):
    base_currency: Currency
    quote_currency: Currency
    rate: Decimal = Field(gt=0)
    as_of: datetime
    source: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def fx_as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class PreflightDecision(FrozenDomainModel):
    allowed: bool
    reasons: tuple[str, ...] = ()
    minimum_trade_amount: Decimal | None = Field(default=None, gt=0)


class BrokerSubmission(FrozenDomainModel):
    idempotency_key: str = Field(min_length=8)
    request_id: str = Field(min_length=1)
    state: ExecutionState
    broker_order_id: str | None = None
    broker_reference_id: str | None = None
    proposal_id: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    submitted_at: datetime

    @field_validator("submitted_at")
    @classmethod
    def submitted_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "submitted_at")


class PerformanceSnapshot(FrozenDomainModel):
    timestamp: datetime
    strategy_version: str = Field(min_length=1)
    equity: Decimal = Field(gt=0)
    benchmark_value: Decimal | None = Field(default=None, gt=0)
    currency: Currency

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")


class BrokerStatus(FrozenDomainModel):
    credentials_configured: bool
    authentication_attempted: bool
    authentication_successful: bool
    account_identity_verified: bool
    real_portfolio_read_available: bool
    demo_portfolio_read_available: bool
    market_data_available: bool
    demo_execution_available: bool
    real_execution_available: bool = False
    status: str = Field(min_length=1)
