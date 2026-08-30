"""Risk-admitted local fills with no network or broker dependency."""

from datetime import datetime
from decimal import Decimal
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from app.domain.enums import SettlementType, TradeIntent, TradeSide
from app.domain.market import MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.execution.gate import AuthorizedTrade, RiskEnforcedExecutionGate
from app.paper_trading.models import (
    PaperExecutionResult,
    PaperFill,
    PaperPortfolioState,
    PaperPosition,
)


class PaperTradingError(RuntimeError):
    """Base error for rejected local simulation mutations."""


class DuplicatePaperExecutionError(PaperTradingError):
    pass


class InsufficientPaperCashError(PaperTradingError):
    pass


class InvalidPaperPositionError(PaperTradingError):
    pass


class PaperTradingEngine:
    """A local ledger that accepts only gate-minted AuthorizedTrade objects."""

    def __init__(
        self,
        *,
        initial_state: PaperPortfolioState,
        admission_gate: RiskEnforcedExecutionGate,
        fee_rate: Decimal = Decimal("0"),
        slippage_rate: Decimal = Decimal("0"),
        max_quote_age_seconds: int = 300,
    ) -> None:
        if fee_rate < 0 or fee_rate > Decimal("0.05"):
            raise ValueError("fee_rate must be between 0 and 0.05")
        if slippage_rate < 0 or slippage_rate > Decimal("0.05"):
            raise ValueError("slippage_rate must be between 0 and 0.05")
        if max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        self._state = initial_state
        self._gate = admission_gate
        self._fee_rate = fee_rate
        self._slippage_rate = slippage_rate
        self._max_quote_age_seconds = max_quote_age_seconds
        self._lock = RLock()
        self._proposal_ids = {fill.proposal_id for fill in initial_state.trade_history}
        self._idempotency_keys = {fill.idempotency_key for fill in initial_state.trade_history}

    @property
    def state(self) -> PaperPortfolioState:
        with self._lock:
            return self._state

    @property
    def recent_idempotency_keys(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._idempotency_keys)

    def daily_new_trade_count(self, at: datetime) -> int:
        with self._lock:
            return sum(
                1
                for fill in self._state.trade_history
                if fill.timestamp.date() == at.date()
                and fill.action in {TradeIntent.OPEN, TradeIntent.INCREASE}
            )

    def portfolio_snapshot(
        self, *, as_of: datetime, quotes: tuple[MarketQuote, ...] = ()
    ) -> PortfolioSnapshot:
        with self._lock:
            quote_prices = {quote.instrument_id: quote.price for quote in quotes}
            positions = tuple(
                position.model_copy(
                    update={
                        "market_price": quote_prices.get(
                            position.instrument_id, position.market_price
                        )
                    }
                )
                for position in self._state.positions
            )
            state = self._state.model_copy(update={"as_of": as_of, "positions": positions})
            return state.to_portfolio_snapshot()

    def execute(
        self,
        admitted: AuthorizedTrade,
        quote: MarketQuote,
        *,
        at: datetime,
    ) -> PaperExecutionResult:
        self._gate.assert_admitted(admitted, at=at)
        proposal = admitted.proposal
        if proposal.leverage != 1 or proposal.settlement_type is not SettlementType.REAL:
            raise PaperTradingError("paper engine permits only unleveraged real instruments")
        if (
            proposal.instrument_id != quote.instrument_id
            or proposal.symbol.casefold() != quote.symbol.casefold()
        ):
            raise PaperTradingError("paper quote does not match the admitted proposal")
        if (
            proposal.currency is not self._state.currency
            or quote.currency is not self._state.currency
        ):
            raise PaperTradingError("paper execution currency mismatch")
        if not quote.is_fresh(as_of=at, max_age_seconds=self._max_quote_age_seconds):
            raise PaperTradingError("paper execution quote is stale or future-dated")

        with self._lock:
            if proposal.proposal_id in self._proposal_ids:
                raise DuplicatePaperExecutionError("paper proposal has already executed")
            if proposal.idempotency_key in self._idempotency_keys:
                raise DuplicatePaperExecutionError("paper idempotency key has already executed")

            positions = list(self._state.positions)
            index = next(
                (
                    item_index
                    for item_index, position in enumerate(positions)
                    if position.instrument_id == proposal.instrument_id
                ),
                None,
            )
            if proposal.side is TradeSide.BUY:
                fill, cash, positions = self._buy(proposal, quote, at, positions, index)
            else:
                fill, cash, positions = self._sell(proposal, quote, at, positions, index)

            history = (*self._state.trade_history, fill)
            positions_value = sum((position.market_value for position in positions), Decimal("0"))
            portfolio_value = cash + positions_value
            self._state = PaperPortfolioState(
                as_of=at,
                currency=self._state.currency,
                initial_cash=self._state.initial_cash,
                cash=cash,
                positions=tuple(positions),
                trade_history=history,
                realized_pnl=self._state.realized_pnl + fill.realized_pnl,
                peak_value=max(self._state.peak_value, portfolio_value),
            )
            self._proposal_ids.add(proposal.proposal_id)
            self._idempotency_keys.add(proposal.idempotency_key)
            return PaperExecutionResult(fill=fill, portfolio=self._state)

    def _buy(
        self,
        proposal: TradeProposal,
        quote: MarketQuote,
        at: datetime,
        positions: list[PaperPosition],
        index: int | None,
    ) -> tuple[PaperFill, Decimal, list[PaperPosition]]:
        if proposal.intent not in {TradeIntent.OPEN, TradeIntent.INCREASE}:
            raise PaperTradingError("buy side requires OPEN or INCREASE")
        if proposal.amount > self._state.cash:
            raise InsufficientPaperCashError("paper account has insufficient cash")
        fill_price = quote.price * (Decimal("1") + self._slippage_rate)
        fees = proposal.amount * self._fee_rate
        invested = proposal.amount - fees
        if invested <= 0:
            raise InsufficientPaperCashError("paper fees consume the proposed amount")
        quantity = invested / fill_price
        if index is None:
            if proposal.intent is not TradeIntent.OPEN:
                raise InvalidPaperPositionError("cannot increase a missing paper position")
            positions.append(
                PaperPosition(
                    position_id=f"paper-{proposal.proposal_id.hex}",
                    instrument_id=proposal.instrument_id,
                    symbol=proposal.symbol,
                    units=quantity,
                    average_entry_price=fill_price,
                    market_price=quote.price,
                )
            )
        else:
            if proposal.intent is not TradeIntent.INCREASE:
                raise InvalidPaperPositionError("cannot open a duplicate paper position")
            current = positions[index]
            total_cost = current.units * current.average_entry_price + invested
            new_units = current.units + quantity
            positions[index] = current.model_copy(
                update={
                    "units": new_units,
                    "average_entry_price": total_cost / new_units,
                    "market_price": quote.price,
                }
            )
        return (
            self._fill(proposal, quote, at, quantity, fill_price, fees, Decimal("0")),
            self._state.cash - proposal.amount,
            positions,
        )

    def _sell(
        self,
        proposal: TradeProposal,
        quote: MarketQuote,
        at: datetime,
        positions: list[PaperPosition],
        index: int | None,
    ) -> tuple[PaperFill, Decimal, list[PaperPosition]]:
        if proposal.intent not in {TradeIntent.REDUCE, TradeIntent.CLOSE}:
            raise PaperTradingError("sell side requires REDUCE or CLOSE")
        if index is None:
            raise InvalidPaperPositionError("cannot sell a missing paper position")
        current = positions[index]
        fill_price = quote.price * (Decimal("1") - self._slippage_rate)
        if proposal.intent is TradeIntent.CLOSE:
            expected_value = current.units * quote.price
            if abs(proposal.amount - expected_value) > Decimal("0.01"):
                raise InvalidPaperPositionError("close amount must match the full paper position")
            quantity = current.units
        else:
            quantity = proposal.amount / quote.price
        if quantity > current.units:
            raise InvalidPaperPositionError("paper sale would create a short position")
        gross_proceeds = quantity * fill_price
        fees = gross_proceeds * self._fee_rate
        realized = (fill_price - current.average_entry_price) * quantity - fees
        remaining = current.units - quantity
        if remaining == 0:
            positions.pop(index)
        else:
            positions[index] = current.model_copy(
                update={"units": remaining, "market_price": quote.price}
            )
        return (
            self._fill(proposal, quote, at, quantity, fill_price, fees, realized),
            self._state.cash + gross_proceeds - fees,
            positions,
        )

    def _fill(
        self,
        proposal: TradeProposal,
        quote: MarketQuote,
        at: datetime,
        quantity: Decimal,
        fill_price: Decimal,
        fees: Decimal,
        realized_pnl: Decimal,
    ) -> PaperFill:
        fill_id = uuid5(NAMESPACE_URL, f"paper-fill:{proposal.proposal_id}")
        return PaperFill(
            fill_id=fill_id,
            symbol=proposal.symbol,
            side=proposal.side,
            action=proposal.intent,
            quantity=quantity,
            reference_price=quote.price,
            simulated_fill_price=fill_price,
            timestamp=at,
            fees=fees,
            slippage=abs(fill_price - quote.price),
            proposal_id=proposal.proposal_id,
            idempotency_key=proposal.idempotency_key,
            realized_pnl=realized_pnl,
        )
