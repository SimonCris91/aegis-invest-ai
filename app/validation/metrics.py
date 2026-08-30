"""Deterministic performance metrics for strategy validation."""

from datetime import datetime
from decimal import Decimal
from math import sqrt

from app.validation.models import PerformanceSummary, SimulatedTradeRecord

NOT_ENOUGH_DATA = "NOT_ENOUGH_DATA"


class PerformanceMetricCalculator:
    def calculate(
        self,
        *,
        equity_curve: tuple[tuple[datetime, Decimal], ...],
        trades: tuple[SimulatedTradeRecord, ...],
        periods_per_year: int = 252,
    ) -> PerformanceSummary:
        returns = _periodic_returns(equity_curve)
        realized_trades = tuple(trade for trade in trades if trade.realized_pnl != 0)
        wins = tuple(trade.realized_pnl for trade in realized_trades if trade.realized_pnl > 0)
        losses = tuple(
            abs(trade.realized_pnl) for trade in realized_trades if trade.realized_pnl < 0
        )
        total_gross = sum((trade.gross_value for trade in trades), Decimal("0"))
        total = _total_return(equity_curve)
        max_dd = _maximum_drawdown(equity_curve)
        annualized = _annualized_return(equity_curve, total)
        volatility = _annualized_volatility(returns, periods_per_year=periods_per_year)
        sharpe = _sharpe(returns, volatility, periods_per_year=periods_per_year)
        sortino = _sortino(returns, periods_per_year=periods_per_year)
        profit_factor = (
            sum(wins, Decimal("0")) / sum(losses, Decimal("0")) if losses else NOT_ENOUGH_DATA
        )
        average_win = sum(wins, Decimal("0")) / Decimal(len(wins)) if wins else NOT_ENOUGH_DATA
        average_loss = (
            sum(losses, Decimal("0")) / Decimal(len(losses)) if losses else NOT_ENOUGH_DATA
        )
        payoff = (
            average_win / average_loss
            if isinstance(average_win, Decimal)
            and isinstance(average_loss, Decimal)
            and average_loss > 0
            else NOT_ENOUGH_DATA
        )
        expectancy = (
            sum((trade.realized_pnl for trade in realized_trades), Decimal("0"))
            / Decimal(len(realized_trades))
            if realized_trades
            else NOT_ENOUGH_DATA
        )
        exposure = _average_exposure(equity_curve)
        return PerformanceSummary(
            total_return=total,
            annualized_return=annualized,
            win_rate=Decimal(len(wins)) / Decimal(len(realized_trades))
            if realized_trades
            else NOT_ENOUGH_DATA,
            loss_rate=Decimal(len(losses)) / Decimal(len(realized_trades))
            if realized_trades
            else NOT_ENOUGH_DATA,
            profit_factor=profit_factor,
            expectancy=expectancy,
            average_win=average_win,
            average_loss=average_loss,
            payoff_ratio=payoff,
            volatility=volatility,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            maximum_drawdown=max_dd,
            calmar_ratio=_ratio(annualized, max_dd),
            recovery_factor=_ratio(total, max_dd),
            mfe=max((trade.mfe for trade in realized_trades), default=NOT_ENOUGH_DATA),
            mae=min((trade.mae for trade in realized_trades), default=NOT_ENOUGH_DATA),
            turnover=total_gross / equity_curve[0][1] if equity_curve else NOT_ENOUGH_DATA,
            exposure=exposure,
            trade_count=len(realized_trades),
            average_holding_period=NOT_ENOUGH_DATA,
        )


def _total_return(equity_curve: tuple[tuple[datetime, Decimal], ...]) -> Decimal | str:
    if len(equity_curve) < 2:
        return NOT_ENOUGH_DATA
    start = equity_curve[0][1]
    if start <= 0:
        return NOT_ENOUGH_DATA
    return equity_curve[-1][1] / start - Decimal("1")


def _periodic_returns(equity_curve: tuple[tuple[datetime, Decimal], ...]) -> tuple[Decimal, ...]:
    return tuple(
        current[1] / previous[1] - Decimal("1")
        for previous, current in zip(equity_curve, equity_curve[1:], strict=False)
        if previous[1] > 0
    )


def _annualized_return(
    equity_curve: tuple[tuple[datetime, Decimal], ...], total_return: Decimal | str
) -> Decimal | str:
    if len(equity_curve) < 2 or isinstance(total_return, str):
        return NOT_ENOUGH_DATA
    days = max(1, (equity_curve[-1][0] - equity_curve[0][0]).days)
    annualized = float(Decimal("1") + total_return) ** (365.0 / float(days)) - 1.0
    return Decimal(str(annualized))


def _annualized_volatility(returns: tuple[Decimal, ...], *, periods_per_year: int) -> Decimal | str:
    if len(returns) < 3:
        return NOT_ENOUGH_DATA
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum((value - mean) ** 2 for value in returns) / Decimal(len(returns) - 1)
    return variance.sqrt() * Decimal(str(sqrt(periods_per_year)))


def _sharpe(
    returns: tuple[Decimal, ...], volatility: Decimal | str, *, periods_per_year: int
) -> Decimal | str:
    if isinstance(volatility, str) or volatility == 0 or not returns:
        return NOT_ENOUGH_DATA
    annualized = sum(returns, Decimal("0")) / Decimal(len(returns)) * Decimal(periods_per_year)
    return annualized / volatility


def _sortino(returns: tuple[Decimal, ...], *, periods_per_year: int) -> Decimal | str:
    downside = tuple(value for value in returns if value < 0)
    if len(downside) < 2:
        return NOT_ENOUGH_DATA
    downside_deviation = (
        sum(value**2 for value in downside) / Decimal(len(downside) - 1)
    ).sqrt() * Decimal(str(sqrt(periods_per_year)))
    if downside_deviation == 0:
        return NOT_ENOUGH_DATA
    annualized = sum(returns, Decimal("0")) / Decimal(len(returns)) * Decimal(periods_per_year)
    return annualized / downside_deviation


def _maximum_drawdown(equity_curve: tuple[tuple[datetime, Decimal], ...]) -> Decimal | str:
    if len(equity_curve) < 2:
        return NOT_ENOUGH_DATA
    peak = equity_curve[0][1]
    worst = Decimal("0")
    for _, value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _ratio(numerator: Decimal | str, denominator: Decimal | str) -> Decimal | str:
    if isinstance(numerator, str) or isinstance(denominator, str) or denominator <= 0:
        return NOT_ENOUGH_DATA
    return numerator / denominator


def _average_exposure(equity_curve: tuple[tuple[datetime, Decimal], ...]) -> Decimal | str:
    if not equity_curve:
        return NOT_ENOUGH_DATA
    invested = tuple(value for _, value in equity_curve if value > 0)
    if not invested:
        return NOT_ENOUGH_DATA
    return Decimal("1")
