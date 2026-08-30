"""Research-only simulated execution and portfolio accounting."""

from decimal import Decimal

from app.domain.enums import RiskDecisionStatus, TradeIntent, TradeSide
from app.domain.market import MarketQuote
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskEvaluation
from app.validation.costs import TransactionCostModel
from app.validation.models import (
    ReplayDecisionStatus,
    SimulatedPortfolioState,
    SimulatedPosition,
    SimulatedTradeRecord,
    TransactionCostEstimate,
)


class SimulatedExecutionError(RuntimeError):
    """Raised when the research-only portfolio simulation cannot safely continue."""


class SimulatedExecutionEngine:
    """Local research engine; it has no broker transport or credential dependency."""

    def __init__(self, cost_model: TransactionCostModel | None = None) -> None:
        self._cost_model = cost_model or TransactionCostModel()

    def mark_to_market(
        self, portfolio: SimulatedPortfolioState, quotes: dict[int, MarketQuote]
    ) -> SimulatedPortfolioState:
        positions = tuple(
            position.model_copy(
                update={"market_price": _market_price(position=position, quotes=quotes)}
            )
            for position in portfolio.positions
        )
        updated = portfolio.model_copy(update={"positions": positions})
        return updated.model_copy(
            update={"peak_value": max(portfolio.peak_value, updated.total_value)}
        )

    def execute(
        self,
        *,
        portfolio: SimulatedPortfolioState,
        proposal: TradeProposal,
        quote: MarketQuote,
        risk: RiskEvaluation,
    ) -> tuple[SimulatedPortfolioState, SimulatedTradeRecord]:
        if risk.decision.status is not RiskDecisionStatus.APPROVED:
            record = _record(
                proposal=proposal,
                quote=quote,
                status=ReplayDecisionStatus.RISK_REJECTED,
                costs=self._cost_model.estimate(
                    quote=quote,
                    gross_value=Decimal("0"),
                    portfolio_currency=portfolio.currency,
                ),
                quantity=Decimal("0"),
                realized_pnl=Decimal("0"),
            )
            return portfolio, record
        if proposal.side is TradeSide.BUY:
            return self._buy(portfolio=portfolio, proposal=proposal, quote=quote)
        return self._sell(portfolio=portfolio, proposal=proposal, quote=quote)

    def _buy(
        self, *, portfolio: SimulatedPortfolioState, proposal: TradeProposal, quote: MarketQuote
    ) -> tuple[SimulatedPortfolioState, SimulatedTradeRecord]:
        if proposal.intent not in {TradeIntent.OPEN, TradeIntent.INCREASE}:
            raise SimulatedExecutionError("buy simulation requires OPEN or INCREASE")
        costs = self._cost_model.estimate(
            quote=quote,
            gross_value=proposal.amount,
            portfolio_currency=portfolio.currency,
        )
        if not costs.complete:
            raise SimulatedExecutionError("transaction costs are incomplete")
        if proposal.amount > portfolio.cash:
            raise SimulatedExecutionError("simulated portfolio has insufficient cash")
        investable = proposal.amount - costs.total_cost
        if investable <= 0:
            raise SimulatedExecutionError("transaction costs consume the simulated order")
        quantity = investable / quote.price
        positions = list(portfolio.positions)
        index = _position_index(positions, proposal.instrument_id)
        if index is None:
            positions.append(
                SimulatedPosition(
                    instrument_id=proposal.instrument_id,
                    symbol=proposal.symbol,
                    asset_class=proposal.asset_class,
                    units=quantity,
                    average_entry_price=quote.price,
                    market_price=quote.price,
                )
            )
        else:
            current = positions[index]
            total_cost_basis = current.units * current.average_entry_price + investable
            new_units = current.units + quantity
            positions[index] = current.model_copy(
                update={
                    "units": new_units,
                    "average_entry_price": total_cost_basis / new_units,
                    "market_price": quote.price,
                }
            )
        updated = SimulatedPortfolioState(
            as_of=quote.as_of,
            currency=portfolio.currency,
            cash=portfolio.cash - proposal.amount,
            positions=tuple(positions),
            realized_pnl=portfolio.realized_pnl,
            peak_value=portfolio.peak_value,
        )
        return (
            updated.model_copy(
                update={"peak_value": max(portfolio.peak_value, updated.total_value)}
            ),
            _record(
                proposal=proposal,
                quote=quote,
                status=ReplayDecisionStatus.SIMULATED_EXECUTED,
                costs=costs,
                quantity=quantity,
                realized_pnl=Decimal("0"),
            ),
        )

    def _sell(
        self, *, portfolio: SimulatedPortfolioState, proposal: TradeProposal, quote: MarketQuote
    ) -> tuple[SimulatedPortfolioState, SimulatedTradeRecord]:
        if proposal.intent not in {TradeIntent.REDUCE, TradeIntent.CLOSE}:
            raise SimulatedExecutionError("sell simulation requires REDUCE or CLOSE")
        positions = list(portfolio.positions)
        index = _position_index(positions, proposal.instrument_id)
        if index is None:
            raise SimulatedExecutionError("cannot sell a missing simulated position")
        current = positions[index]
        quantity = (
            current.units if proposal.intent is TradeIntent.CLOSE else proposal.amount / quote.price
        )
        if quantity > current.units:
            raise SimulatedExecutionError("sell simulation would create negative holdings")
        gross_value = quantity * quote.price
        costs = self._cost_model.estimate(
            quote=quote,
            gross_value=gross_value,
            portfolio_currency=portfolio.currency,
        )
        if not costs.complete:
            raise SimulatedExecutionError("transaction costs are incomplete")
        realized = (quote.price - current.average_entry_price) * quantity - costs.total_cost
        remaining = current.units - quantity
        if remaining == 0:
            positions.pop(index)
        else:
            positions[index] = current.model_copy(
                update={"units": remaining, "market_price": quote.price}
            )
        updated = SimulatedPortfolioState(
            as_of=quote.as_of,
            currency=portfolio.currency,
            cash=portfolio.cash + gross_value - costs.total_cost,
            positions=tuple(positions),
            realized_pnl=portfolio.realized_pnl + realized,
            peak_value=portfolio.peak_value,
        )
        return (
            updated.model_copy(
                update={"peak_value": max(portfolio.peak_value, updated.total_value)}
            ),
            _record(
                proposal=proposal,
                quote=quote,
                status=ReplayDecisionStatus.SIMULATED_EXECUTED,
                costs=costs,
                quantity=quantity,
                realized_pnl=realized,
            ),
        )


def _position_index(positions: list[SimulatedPosition], instrument_id: int) -> int | None:
    return next(
        (
            index
            for index, position in enumerate(positions)
            if position.instrument_id == instrument_id
        ),
        None,
    )


def _market_price(position: SimulatedPosition, quotes: dict[int, MarketQuote]) -> Decimal:
    quote = quotes.get(position.instrument_id)
    if quote is None:
        return position.market_price
    return quote.price


def _record(
    *,
    proposal: TradeProposal,
    quote: MarketQuote,
    status: ReplayDecisionStatus,
    costs: TransactionCostEstimate,
    quantity: Decimal,
    realized_pnl: Decimal,
) -> SimulatedTradeRecord:
    return SimulatedTradeRecord(
        timestamp=quote.as_of,
        instrument_id=proposal.instrument_id,
        symbol=proposal.symbol,
        asset_class=proposal.asset_class,
        side=proposal.side,
        action=proposal.intent,
        quantity=quantity,
        gross_value=proposal.amount,
        costs=costs,
        realized_pnl=realized_pnl,
        status=status,
        proposal_id=str(proposal.proposal_id),
        risk_status=RiskDecisionStatus.APPROVED
        if status is ReplayDecisionStatus.SIMULATED_EXECUTED
        else RiskDecisionStatus.REJECTED,
    )
