from datetime import datetime
from decimal import Decimal

from app.domain.portfolio import PortfolioSnapshot
from app.portfolio.calculations import PortfolioCalculator


def test_portfolio_calculations_are_deterministic(
    portfolio: PortfolioSnapshot, now: datetime
) -> None:
    del now
    metrics = PortfolioCalculator.calculate(portfolio)

    assert metrics.cash == Decimal("160")
    assert metrics.positions_value == Decimal("40")
    assert metrics.total_value == Decimal("200")
    assert metrics.cash_weight == Decimal("0.8")
    assert metrics.gross_exposure == Decimal("0.2")
    assert metrics.drawdown == Decimal("10") / Decimal("210")


def test_portfolio_detects_reported_total_mismatch(portfolio: PortfolioSnapshot) -> None:
    inconsistent = portfolio.model_copy(update={"reported_total_value": Decimal("201")})

    assert inconsistent.is_consistent(Decimal("0.01")) is False
