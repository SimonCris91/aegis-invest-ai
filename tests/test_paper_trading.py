from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.config.models import RiskPolicyConfig
from app.domain.enums import (
    Currency,
    HoldingPeriod,
    RiskViolationCode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem, InstrumentMetadata, MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.execution.gate import AuthorizedTrade, RiskEnforcedExecutionGate
from app.paper_trading.engine import (
    DuplicatePaperExecutionError,
    InsufficientPaperCashError,
    InvalidPaperPositionError,
    PaperTradingEngine,
)
from app.paper_trading.models import PaperPortfolioState, PaperPosition
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager


def paper_state(
    now: datetime,
    *,
    cash: Decimal = Decimal("200"),
    position: PaperPosition | None = None,
) -> PaperPortfolioState:
    positions = (position,) if position is not None else ()
    value = cash + sum((item.market_value for item in positions), Decimal("0"))
    return PaperPortfolioState(
        as_of=now,
        currency=Currency.USD,
        initial_cash=Decimal("200"),
        cash=cash,
        positions=positions,
        peak_value=max(value, Decimal("200")),
    )


def proposal_for(
    now: datetime,
    instrument: InstrumentMetadata,
    *,
    number: int,
    intent: TradeIntent,
    side: TradeSide,
    amount: Decimal,
    idempotency_key: str | None = None,
) -> TradeProposal:
    return TradeProposal(
        proposal_id=UUID(int=number),
        idempotency_key=idempotency_key or f"paper-proposal-{number:04d}",
        created_at=now,
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        side=side,
        intent=intent,
        amount=amount,
        currency=Currency.USD,
        target_weight=Decimal("0.15"),
        current_weight=Decimal("0"),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="deterministic paper test",
        evidence=(
            EvidenceItem(
                source="fixture",
                timestamp=now,
                summary="synthetic evidence",
                confidence=Decimal("0.90"),
            ),
        ),
        confidence=Decimal("0.80"),
        risk_factors=("market risk",),
        invalidation_conditions=("fixture invalidation",),
        expected_holding_period=HoldingPeriod.MONTHS,
    )


def engine_for(
    now: datetime,
    risk_manager: RiskManager,
    *,
    state: PaperPortfolioState | None = None,
) -> tuple[PaperTradingEngine, RiskEnforcedExecutionGate]:
    gate = RiskEnforcedExecutionGate(risk_manager)
    return (
        PaperTradingEngine(
            initial_state=state or paper_state(now),
            admission_gate=gate,
        ),
        gate,
    )


def approve(
    manager: RiskManager,
    gate: RiskEnforcedExecutionGate,
    engine: PaperTradingEngine,
    proposal: TradeProposal,
    quote: MarketQuote,
    instrument: InstrumentMetadata,
    now: datetime,
) -> AuthorizedTrade:
    portfolio = engine.portfolio_snapshot(as_of=now, quotes=(quote,))
    evaluation = manager.evaluate(
        proposal,
        RiskContext(
            evaluated_at=now,
            portfolio=portfolio,
            price=quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            news_data_available=True,
            daily_new_trade_count=engine.daily_new_trade_count(now),
            recent_idempotency_keys=frozenset(),
            api_state_consistent=True,
        ),
    )
    assert evaluation.authorization is not None
    return gate.admit(proposal, evaluation.authorization, at=now)


def test_open_increase_reduce_and_close_update_local_ledger(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    engine, gate = engine_for(now, risk_manager)
    open_proposal = proposal_for(
        now,
        instrument,
        number=1001,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    opened = engine.execute(
        approve(risk_manager, gate, engine, open_proposal, market_quote, instrument, now),
        market_quote,
        at=now,
    )
    assert opened.portfolio.cash == Decimal("190")
    assert opened.portfolio.positions[0].units == Decimal("1")

    increase = proposal_for(
        now,
        instrument,
        number=1002,
        intent=TradeIntent.INCREASE,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    increased = engine.execute(
        approve(risk_manager, gate, engine, increase, market_quote, instrument, now),
        market_quote,
        at=now,
    )
    assert increased.portfolio.positions[0].units == Decimal("2")
    assert increased.portfolio.positions[0].average_entry_price == Decimal("10")

    reduce = proposal_for(
        now,
        instrument,
        number=1003,
        intent=TradeIntent.REDUCE,
        side=TradeSide.SELL,
        amount=Decimal("10"),
    )
    reduced = engine.execute(
        approve(risk_manager, gate, engine, reduce, market_quote, instrument, now),
        market_quote,
        at=now,
    )
    assert reduced.portfolio.positions[0].units == Decimal("1")
    assert reduced.portfolio.cash == Decimal("190")

    close = proposal_for(
        now,
        instrument,
        number=1004,
        intent=TradeIntent.CLOSE,
        side=TradeSide.SELL,
        amount=Decimal("10"),
    )
    closed = engine.execute(
        approve(risk_manager, gate, engine, close, market_quote, instrument, now),
        market_quote,
        at=now,
    )
    assert closed.portfolio.positions == ()
    assert closed.portfolio.cash == Decimal("200")
    assert len(closed.portfolio.trade_history) == 4


def test_unauthorized_raw_proposal_cannot_execute(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    engine, _ = engine_for(now, risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1101,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )

    with pytest.raises(PermissionError, match="AuthorizedTrade"):
        engine.execute(cast(AuthorizedTrade, proposal), market_quote, at=now)


def test_duplicate_execution_is_rejected(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    engine, gate = engine_for(now, risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1201,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    admitted = approve(risk_manager, gate, engine, proposal, market_quote, instrument, now)
    engine.execute(admitted, market_quote, at=now)

    with pytest.raises(DuplicatePaperExecutionError):
        engine.execute(admitted, market_quote, at=now)


def test_forged_admission_is_rejected(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    engine, gate = engine_for(now, risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1251,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    real = approve(risk_manager, gate, engine, proposal, market_quote, instrument, now)
    forged = AuthorizedTrade(
        proposal=real.proposal,
        authorization=real.authorization,
        admission_id=uuid4(),
        admitted_at=now,
        _origin_seal=object(),
    )

    with pytest.raises(PermissionError, match="not admitted"):
        engine.execute(forged, market_quote, at=now)


def test_duplicate_idempotency_key_is_rejected_by_ledger(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    engine, gate = engine_for(now, risk_manager)
    first = proposal_for(
        now,
        instrument,
        number=1301,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
        idempotency_key="shared-paper-key",
    )
    engine.execute(
        approve(risk_manager, gate, engine, first, market_quote, instrument, now),
        market_quote,
        at=now,
    )
    second = proposal_for(
        now,
        instrument,
        number=1302,
        intent=TradeIntent.INCREASE,
        side=TradeSide.BUY,
        amount=Decimal("10"),
        idempotency_key="shared-paper-key",
    )
    admitted = approve(risk_manager, gate, engine, second, market_quote, instrument, now)

    with pytest.raises(DuplicatePaperExecutionError, match="idempotency"):
        engine.execute(admitted, market_quote, at=now)


def test_insufficient_cash_is_defensively_rejected(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
    portfolio: PortfolioSnapshot,
) -> None:
    gate = RiskEnforcedExecutionGate(risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1401,
        intent=TradeIntent.INCREASE,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    evaluation = risk_manager.evaluate(
        proposal,
        RiskContext(
            evaluated_at=now,
            portfolio=portfolio,
            price=market_quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            news_data_available=True,
            daily_new_trade_count=0,
        ),
    )
    assert evaluation.authorization is not None
    admitted = gate.admit(proposal, evaluation.authorization, at=now)
    poor_engine = PaperTradingEngine(
        initial_state=paper_state(now, cash=Decimal("5")), admission_gate=gate
    )

    with pytest.raises(InsufficientPaperCashError):
        poor_engine.execute(admitted, market_quote, at=now)
    assert poor_engine.state.cash == Decimal("5")


def test_negative_position_is_defensively_prevented(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
    portfolio: PortfolioSnapshot,
) -> None:
    gate = RiskEnforcedExecutionGate(risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1501,
        intent=TradeIntent.REDUCE,
        side=TradeSide.SELL,
        amount=Decimal("10"),
    )
    evaluation = risk_manager.evaluate(
        proposal,
        RiskContext(
            evaluated_at=now,
            portfolio=portfolio,
            price=market_quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            news_data_available=True,
            daily_new_trade_count=0,
        ),
    )
    assert evaluation.authorization is not None
    admitted = gate.admit(proposal, evaluation.authorization, at=now)
    small_position = PaperPosition(
        position_id="paper-small",
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        units=Decimal("0.5"),
        average_entry_price=Decimal("10"),
        market_price=Decimal("10"),
    )
    engine = PaperTradingEngine(
        initial_state=paper_state(now, cash=Decimal("195"), position=small_position),
        admission_gate=gate,
    )

    with pytest.raises(InvalidPaperPositionError, match="short"):
        engine.execute(admitted, market_quote, at=now)
    assert engine.state.positions[0].units == Decimal("0.5")


def test_expired_or_altered_authorization_never_reaches_paper_engine(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    engine, gate = engine_for(now, risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1601,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    portfolio = engine.portfolio_snapshot(as_of=now)
    evaluation = risk_manager.evaluate(
        proposal,
        RiskContext(
            evaluated_at=now,
            portfolio=portfolio,
            price=market_quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            news_data_available=True,
            daily_new_trade_count=0,
        ),
    )
    assert evaluation.authorization is not None

    with pytest.raises(PermissionError, match="expired"):
        gate.admit(proposal, evaluation.authorization, at=now + timedelta(seconds=121))
    with pytest.raises(PermissionError, match="changed"):
        gate.admit(
            proposal.model_copy(update={"amount": Decimal("11")}),
            evaluation.authorization,
            at=now,
        )
    assert engine.state.trade_history == ()


def test_admitted_trade_is_rechecked_for_expiry_at_fill_time(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    engine, gate = engine_for(now, risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1651,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    admitted = approve(risk_manager, gate, engine, proposal, market_quote, instrument, now)

    with pytest.raises(PermissionError, match="expired"):
        engine.execute(
            admitted,
            market_quote.model_copy(update={"as_of": now + timedelta(seconds=121)}),
            at=now + timedelta(seconds=121),
        )
    assert engine.state.trade_history == ()


def test_kill_switch_activation_after_admission_blocks_fill(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    engine, gate = engine_for(now, risk_manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1661,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    admitted = approve(risk_manager, gate, engine, proposal, market_quote, instrument, now)
    kill_switch.activate("halt after risk approval")

    with pytest.raises(PermissionError, match="kill switch"):
        engine.execute(admitted, market_quote, at=now)
    assert engine.state.trade_history == ()


def test_kill_switch_and_risk_limits_prevent_any_paper_admission(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
) -> None:
    switch = KillSwitch(active=True, reason="safe test default", clock=lambda: now)
    manager = RiskManager(
        policy=RiskPolicyConfig(),
        kill_switch=switch,
        authorization_key=b"test-risk-authorization-key-32b!",
        clock=lambda: now,
    )
    engine, _ = engine_for(now, manager)
    proposal = proposal_for(
        now,
        instrument,
        number=1701,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("25"),
    )
    evaluation = manager.evaluate(
        proposal,
        RiskContext(
            evaluated_at=now,
            portfolio=engine.portfolio_snapshot(as_of=now),
            price=market_quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            news_data_available=True,
            daily_new_trade_count=0,
        ),
    )

    codes = {violation.code for violation in evaluation.decision.violations}
    assert RiskViolationCode.KILL_SWITCH_ACTIVE in codes
    assert RiskViolationCode.MAX_TRADE_SIZE_EXCEEDED in codes
    assert evaluation.authorization is None
    assert engine.state.trade_history == ()


def test_fees_slippage_and_pnl_are_explicitly_tracked(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    risk_manager: RiskManager,
) -> None:
    gate = RiskEnforcedExecutionGate(risk_manager)
    engine = PaperTradingEngine(
        initial_state=paper_state(now),
        admission_gate=gate,
        fee_rate=Decimal("0.01"),
        slippage_rate=Decimal("0.01"),
    )
    open_proposal = proposal_for(
        now,
        instrument,
        number=1801,
        intent=TradeIntent.OPEN,
        side=TradeSide.BUY,
        amount=Decimal("10"),
    )
    opened = engine.execute(
        approve(risk_manager, gate, engine, open_proposal, market_quote, instrument, now),
        market_quote,
        at=now,
    )
    assert opened.fill.fees == Decimal("0.10")
    assert opened.fill.simulated_fill_price == Decimal("10.10")
    assert opened.fill.slippage == Decimal("0.10")

    higher_quote = market_quote.model_copy(update={"price": Decimal("12")})
    position_value = opened.portfolio.positions[0].units * higher_quote.price
    close = proposal_for(
        now,
        instrument,
        number=1802,
        intent=TradeIntent.CLOSE,
        side=TradeSide.SELL,
        amount=position_value,
    )
    closed = engine.execute(
        approve(risk_manager, gate, engine, close, higher_quote, instrument, now),
        higher_quote,
        at=now,
    )

    assert closed.fill.realized_pnl > 0
    assert closed.portfolio.realized_pnl == closed.fill.realized_pnl
    assert closed.portfolio.positions == ()
    assert closed.portfolio.cash > Decimal("200")
