"""Immutable models for a ledger that is explicitly separate from broker state."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import Currency, SettlementType, TradeIntent, TradeSide
from app.domain.portfolio import PortfolioSnapshot, Position


class PaperPosition(FrozenDomainModel):
    position_id: str = Field(min_length=1)
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    settlement_type: SettlementType = SettlementType.REAL
    units: Decimal = Field(gt=0)
    average_entry_price: Decimal = Field(gt=0)
    market_price: Decimal = Field(gt=0)

    @property
    def market_value(self) -> Decimal:
        return self.units * self.market_price

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.market_price - self.average_entry_price) * self.units


class PaperFill(FrozenDomainModel):
    fill_id: UUID
    symbol: str = Field(min_length=1)
    side: TradeSide
    action: TradeIntent
    quantity: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    simulated_fill_price: Decimal = Field(gt=0)
    timestamp: datetime
    fees: Decimal = Field(ge=0)
    slippage: Decimal = Field(ge=0)
    proposal_id: UUID
    idempotency_key: str = Field(min_length=8)
    realized_pnl: Decimal = Decimal("0")

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")


class PaperPortfolioState(FrozenDomainModel):
    as_of: datetime
    currency: Currency
    initial_cash: Decimal = Field(gt=0)
    cash: Decimal = Field(ge=0)
    positions: tuple[PaperPosition, ...] = ()
    trade_history: tuple[PaperFill, ...] = ()
    realized_pnl: Decimal = Decimal("0")
    peak_value: Decimal = Field(gt=0)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @property
    def positions_value(self) -> Decimal:
        return sum((position.market_value for position in self.positions), Decimal("0"))

    @property
    def portfolio_value(self) -> Decimal:
        return self.cash + self.positions_value

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((position.unrealized_pnl for position in self.positions), Decimal("0"))

    def to_portfolio_snapshot(self) -> PortfolioSnapshot:
        positions = tuple(
            Position(
                position_id=position.position_id,
                instrument_id=position.instrument_id,
                symbol=position.symbol,
                settlement_type=position.settlement_type,
                units=position.units,
                average_entry_price=position.average_entry_price,
                market_price=position.market_price,
            )
            for position in self.positions
        )
        return PortfolioSnapshot(
            as_of=self.as_of,
            currency=self.currency,
            cash=self.cash,
            positions=positions,
            reported_total_value=self.portfolio_value,
            peak_value=self.peak_value,
        )


class PaperExecutionResult(FrozenDomainModel):
    fill: PaperFill
    portfolio: PaperPortfolioState
