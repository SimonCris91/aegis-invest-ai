"""Portfolio state models and deterministic aggregate properties."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import Currency, SettlementType


class Position(FrozenDomainModel):
    position_id: str = Field(min_length=1)
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    settlement_type: SettlementType
    units: Decimal = Field(gt=0)
    average_entry_price: Decimal = Field(gt=0)
    market_price: Decimal = Field(gt=0)
    realized_pnl: Decimal = Decimal("0")

    @property
    def market_value(self) -> Decimal:
        return self.units * self.market_price

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.market_price - self.average_entry_price) * self.units


class PortfolioSnapshot(FrozenDomainModel):
    as_of: datetime
    currency: Currency
    cash: Decimal = Field(ge=0)
    positions: tuple[Position, ...] = ()
    reported_total_value: Decimal | None = Field(default=None, ge=0)
    peak_value: Decimal | None = Field(default=None, gt=0)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @property
    def positions_value(self) -> Decimal:
        return sum((position.market_value for position in self.positions), Decimal("0"))

    @property
    def total_value(self) -> Decimal:
        return self.cash + self.positions_value

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((position.unrealized_pnl for position in self.positions), Decimal("0"))

    @property
    def realized_pnl(self) -> Decimal:
        return sum((position.realized_pnl for position in self.positions), Decimal("0"))

    @property
    def drawdown(self) -> Decimal:
        if self.peak_value is None or self.total_value >= self.peak_value:
            return Decimal("0")
        return (self.peak_value - self.total_value) / self.peak_value

    def market_value_for(self, instrument_id: int) -> Decimal:
        return sum(
            (
                position.market_value
                for position in self.positions
                if position.instrument_id == instrument_id
            ),
            Decimal("0"),
        )

    def positions_for(self, instrument_id: int) -> tuple[Position, ...]:
        return tuple(
            position for position in self.positions if position.instrument_id == instrument_id
        )

    def weight_for(self, instrument_id: int) -> Decimal:
        if self.total_value <= 0:
            return Decimal("0")
        return self.market_value_for(instrument_id) / self.total_value

    def is_consistent(self, tolerance: Decimal) -> bool:
        if self.reported_total_value is None:
            return True
        return abs(self.reported_total_value - self.total_value) <= tolerance
