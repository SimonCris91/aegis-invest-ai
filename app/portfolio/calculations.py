"""Deterministic portfolio metrics independent of eToro transport models."""

from dataclasses import dataclass
from decimal import Decimal

from app.domain.portfolio import PortfolioSnapshot


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    cash: Decimal
    positions_value: Decimal
    total_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    drawdown: Decimal
    cash_weight: Decimal
    gross_exposure: Decimal


class PortfolioCalculator:
    @staticmethod
    def calculate(snapshot: PortfolioSnapshot) -> PortfolioMetrics:
        total = snapshot.total_value
        cash_weight = snapshot.cash / total if total > 0 else Decimal("0")
        gross_exposure = snapshot.positions_value / total if total > 0 else Decimal("0")
        return PortfolioMetrics(
            cash=snapshot.cash,
            positions_value=snapshot.positions_value,
            total_value=total,
            unrealized_pnl=snapshot.unrealized_pnl,
            realized_pnl=snapshot.realized_pnl,
            drawdown=snapshot.drawdown,
            cash_weight=cash_weight,
            gross_exposure=gross_exposure,
        )
