"""Deterministic performance metrics that refuse weak samples."""

from decimal import Decimal
from math import sqrt

from app.brokers.models import PerformanceSnapshot

NOT_ENOUGH_DATA = "NOT_ENOUGH_DATA"


def total_return(snapshots: tuple[PerformanceSnapshot, ...]) -> Decimal | str:
    if len(snapshots) < 2:
        return NOT_ENOUGH_DATA
    return snapshots[-1].equity / snapshots[0].equity - Decimal("1")


def maximum_drawdown(snapshots: tuple[PerformanceSnapshot, ...]) -> Decimal | str:
    if len(snapshots) < 2:
        return NOT_ENOUGH_DATA
    peak = snapshots[0].equity
    worst = Decimal("0")
    for snapshot in snapshots:
        peak = max(peak, snapshot.equity)
        worst = max(worst, (peak - snapshot.equity) / peak)
    return worst


def benchmark_relative_return(snapshots: tuple[PerformanceSnapshot, ...]) -> Decimal | str:
    if len(snapshots) < 2 or any(x.benchmark_value is None for x in snapshots):
        return NOT_ENOUGH_DATA
    first, last = snapshots[0], snapshots[-1]
    assert first.benchmark_value is not None and last.benchmark_value is not None
    return total_return(snapshots) - (last.benchmark_value / first.benchmark_value - Decimal("1"))  # type: ignore[operator]


def periodic_returns(snapshots: tuple[PerformanceSnapshot, ...]) -> tuple[Decimal, ...]:
    return tuple(
        current.equity / previous.equity - Decimal("1")
        for previous, current in zip(snapshots, snapshots[1:], strict=False)
    )


def annualized_volatility(
    snapshots: tuple[PerformanceSnapshot, ...], *, periods_per_year: int = 252
) -> Decimal | str:
    returns = periodic_returns(snapshots)
    if len(returns) < 30:
        return NOT_ENOUGH_DATA
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum((value - mean) ** 2 for value in returns) / Decimal(len(returns) - 1)
    return variance.sqrt() * Decimal(str(sqrt(periods_per_year)))


def sharpe_ratio(
    snapshots: tuple[PerformanceSnapshot, ...], *, periods_per_year: int = 252
) -> Decimal | str:
    volatility = annualized_volatility(snapshots, periods_per_year=periods_per_year)
    if isinstance(volatility, str) or volatility == 0:
        return NOT_ENOUGH_DATA
    returns = periodic_returns(snapshots)
    annualized_return = (
        sum(returns, Decimal("0")) / Decimal(len(returns)) * Decimal(periods_per_year)
    )
    return annualized_return / volatility


def sortino_ratio(
    snapshots: tuple[PerformanceSnapshot, ...], *, periods_per_year: int = 252
) -> Decimal | str:
    returns = periodic_returns(snapshots)
    if len(returns) < 30:
        return NOT_ENOUGH_DATA
    downside = tuple(value for value in returns if value < 0)
    if len(downside) < 2:
        return NOT_ENOUGH_DATA
    downside_deviation = (
        sum(value**2 for value in downside) / Decimal(len(downside) - 1)
    ).sqrt() * Decimal(str(sqrt(periods_per_year)))
    if downside_deviation == 0:
        return NOT_ENOUGH_DATA
    annualized_return = (
        sum(returns, Decimal("0")) / Decimal(len(returns)) * Decimal(periods_per_year)
    )
    return annualized_return / downside_deviation


def operational_counts(records: tuple[dict[str, object], ...]) -> dict[str, int]:
    actions = [str(record.get("would_do", "")) for record in records]
    decisions = [str(record.get("risk_status", "")) for record in records]
    return {
        "number_of_runs": len(records),
        "hold_count": actions.count("HOLD"),
        "proposal_count": sum(action not in {"", "HOLD"} for action in actions),
        "approved_proposals": decisions.count("APPROVED"),
        "risk_rejections": decisions.count("REJECTED"),
        "demo_executions": sum(bool(record.get("demo_execution", False)) for record in records),
    }
