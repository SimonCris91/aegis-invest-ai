"""Broker-neutral market universe, portfolio, and opportunity models."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from app.brokers.models import AccountKind
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.market import MarketQuote


class DataQualityStatus(StrEnum):
    GOOD = "GOOD"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"


class CandidateState(StrEnum):
    OPEN_AND_ALLOWED = "OPEN_AND_ALLOWED"
    OPEN_BUT_POLICY_BLOCKED = "OPEN_BUT_POLICY_BLOCKED"
    MARKET_CLOSED = "MARKET_CLOSED"
    BROKER_INELIGIBLE = "BROKER_INELIGIBLE"
    INSUFFICIENT_METADATA = "INSUFFICIENT_METADATA"
    FX_UNAVAILABLE = "FX_UNAVAILABLE"
    STALE_DATA = "STALE_DATA"
    RISK_BUDGET_BLOCKED = "RISK_BUDGET_BLOCKED"
    UNKNOWN = "UNKNOWN"


class BrokerEligibilitySnapshot(FrozenDomainModel):
    broker: str = Field(min_length=1)
    broker_instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    checked_at: datetime
    currency: Currency | None = None
    verified: bool
    allow_open: bool | None = None
    allow_close: bool | None = None
    minimum_order_value: Decimal | None = Field(default=None, gt=0)
    max_units_per_order: Decimal | None = Field(default=None, gt=0)
    allowed_order_quantity_types: tuple[str, ...] = ()
    settlement_type: SettlementType | None = None
    leverage_configs: tuple[int, ...] = ()
    reason: str | None = Field(default=None, min_length=1)

    @field_validator("checked_at")
    @classmethod
    def checked_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "checked_at")


class UniversalInstrument(FrozenDomainModel):
    """A provider-neutral instrument view; raw broker payloads do not cross this boundary."""

    broker: str = Field(min_length=1)
    broker_instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    display_name: str | None = Field(default=None, min_length=1)
    asset_class: AssetClass = AssetClass.UNKNOWN
    currency: Currency | None = None
    exchange: str | None = Field(default=None, min_length=1)
    market: str | None = Field(default=None, min_length=1)
    market_status: MarketStatus = MarketStatus.UNKNOWN
    tradeable: bool | None = None
    buy_allowed: bool | None = None
    sell_allowed: bool | None = None
    short_allowed: bool | None = None
    leverage_available: bool | None = None
    max_leverage: Decimal = Field(default=Decimal("1"), ge=Decimal("1"))
    settlement_type: SettlementType | None = None
    minimum_order_value: Decimal | None = Field(default=None, gt=0)
    minimum_quantity: Decimal | None = Field(default=None, gt=0)
    quantity_type: str | None = Field(default=None, min_length=1)
    fractional_supported: bool | None = None
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    last_price: Decimal | None = Field(default=None, gt=0)
    price_timestamp: datetime | None = None
    metadata_timestamp: datetime
    broker_eligibility: BrokerEligibilitySnapshot | None = None
    tags: tuple[str, ...] = ()

    @field_validator("metadata_timestamp", "price_timestamp")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "instrument timestamp")

    @model_validator(mode="after")
    def bid_does_not_exceed_ask(self) -> "UniversalInstrument":
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        return self

    @property
    def key(self) -> str:
        return f"{self.broker}:{self.broker_instrument_id}"

    @property
    def numeric_instrument_id(self) -> int | None:
        try:
            value = int(self.broker_instrument_id)
        except ValueError:
            return None
        return value if value > 0 else None


class OpportunityFeatures(FrozenDomainModel):
    mid_price: Decimal | None = Field(default=None, gt=0)
    spread: Decimal | None = Field(default=None, ge=0)
    spread_percentage: Decimal | None = Field(default=None, ge=0)
    price_change: Decimal | None = None
    short_term_momentum: Decimal | None = None
    medium_term_momentum: Decimal | None = None
    volatility: Decimal | None = Field(default=None, ge=0)
    distance_from_recent_range: Decimal | None = None
    volume: Decimal | None = Field(default=None, ge=0)
    current_portfolio_weight: Decimal = Field(default=Decimal("0"), ge=0)
    asset_class_exposure: Decimal = Field(default=Decimal("0"), ge=0)
    currency_exposure: Decimal = Field(default=Decimal("0"), ge=0)
    news_signal: str = "NEWS_NOT_CONFIGURED"
    market_regime: str = "UNKNOWN"
    drawdown: Decimal = Field(default=Decimal("0"), ge=0)
    risk_adjusted_potential: Decimal | None = None


class OpportunityCandidate(FrozenDomainModel):
    candidate_id: str = Field(min_length=1)
    broker: str = Field(min_length=1)
    instrument: UniversalInstrument
    asset_class: AssetClass
    market_status: MarketStatus
    quote: MarketQuote | None = None
    broker_eligibility: BrokerEligibilitySnapshot | None = None
    policy_allowed: bool
    policy_version: str = Field(min_length=1)
    candidate_state: CandidateState
    data_quality: DataQualityStatus
    candidate_score: Decimal = Field(ge=0, le=100)
    opportunity_factors: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    features: OpportunityFeatures
    confidence: Decimal = Field(ge=0, le=1)
    rank: int | None = Field(default=None, ge=1)
    scanner_version: str = Field(min_length=1)
    ranking_version: str = Field(min_length=1)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")


class MarketScanResult(FrozenDomainModel):
    broker: str = Field(min_length=1)
    as_of: datetime
    scanner_version: str = Field(min_length=1)
    ranking_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    total_discovered: int = Field(ge=0)
    candidates: tuple[OpportunityCandidate, ...]
    ranked_candidates: tuple[OpportunityCandidate, ...]
    persisted_record_id: int | None = Field(default=None, ge=1)
    broker_write_calls: int = Field(default=0, ge=0, le=0)
    real_execution_available: bool = False
    demo_execution_enabled: bool = False

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @property
    def asset_classes_found(self) -> tuple[AssetClass, ...]:
        return tuple(sorted({item.asset_class for item in self.candidates}, key=lambda x: x.value))

    @property
    def open_markets(self) -> int:
        return sum(
            1
            for item in self.candidates
            if item.market_status in {MarketStatus.OPEN, MarketStatus.CONTINUOUS_24_7}
        )

    @property
    def closed_markets(self) -> int:
        return sum(1 for item in self.candidates if item.market_status is MarketStatus.CLOSED)

    @property
    def policy_blocked(self) -> int:
        return sum(
            1
            for item in self.candidates
            if item.candidate_state is CandidateState.OPEN_BUT_POLICY_BLOCKED
        )

    @property
    def broker_eligible(self) -> int:
        return sum(
            1
            for item in self.candidates
            if item.broker_eligibility is not None and item.broker_eligibility.verified
        )


class InstrumentExposure(FrozenDomainModel):
    broker: str = Field(min_length=1)
    broker_instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    currency: Currency
    exposure: Decimal = Field(ge=0)


class BrokerAccountSnapshot(FrozenDomainModel):
    broker: str = Field(min_length=1)
    account_kind: AccountKind
    currency: Currency
    cash: Decimal = Field(ge=0)
    total_value: Decimal = Field(ge=0)
    positions_count: int = Field(ge=0)


class ExposureBucket(FrozenDomainModel):
    key: str = Field(min_length=1)
    exposure: Decimal = Field(ge=0)


class UnifiedPortfolio(FrozenDomainModel):
    as_of: datetime
    accounts: tuple[BrokerAccountSnapshot, ...]
    positions: tuple[InstrumentExposure, ...] = ()
    exposure_by_asset_class: tuple[ExposureBucket, ...] = ()
    exposure_by_currency: tuple[ExposureBucket, ...] = ()
    exposure_by_broker: tuple[ExposureBucket, ...] = ()

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class BrokerRoutingDecision(FrozenDomainModel):
    broker: str = Field(min_length=1)
    broker_instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    eligible: bool
    reasons: tuple[str, ...] = ()
    minimum_trade_size: Decimal | None = Field(default=None, gt=0)
    spread: Decimal | None = Field(default=None, ge=0)
    currency: Currency | None = None
    execution_capability: str = "ANALYSIS_ONLY"
