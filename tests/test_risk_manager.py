from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.config.models import RiskPolicyConfig
from app.domain.enums import (
    RiskDecisionStatus,
    RiskViolationCode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import InstrumentMetadata, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext, RiskEvaluation
from app.intelligence.confidence import V2_B_THRESHOLD, V2_B_THRESHOLD_PROVENANCE
from app.reporting.audit import InMemoryAuditSink
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager

from .conftest import OTHER_TEST_INSTRUMENT_ID


def violation_codes(evaluation: RiskEvaluation) -> set[RiskViolationCode]:
    return {violation.code for violation in evaluation.decision.violations}


def test_valid_proposal_is_approved_and_audited(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    audit_sink: InMemoryAuditSink,
) -> None:
    evaluation = risk_manager.evaluate(proposal, context)

    assert evaluation.decision.status is RiskDecisionStatus.APPROVED
    assert evaluation.authorization is not None
    assert evaluation.decision.violations == ()
    assert len(audit_sink.events) == 1
    assert audit_sink.events[0].result == "risk-approved"


def test_no_leverage(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    leveraged = proposal.model_copy(update={"leverage": 2})

    evaluation = risk_manager.evaluate(leveraged, context)

    assert RiskViolationCode.LEVERAGE_NOT_ALLOWED in violation_codes(evaluation)


def test_no_short_selling(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    short = proposal.model_copy(update={"side": TradeSide.SELL, "intent": TradeIntent.OPEN})

    evaluation = risk_manager.evaluate(short, context)

    assert RiskViolationCode.SHORT_SELLING_NOT_ALLOWED in violation_codes(evaluation)


def test_no_cfd_execution(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    instrument: InstrumentMetadata,
) -> None:
    cfd = proposal.model_copy(update={"settlement_type": SettlementType.CFD})
    cfd_context = context.model_copy(
        update={"instrument": instrument.model_copy(update={"settlement_type": SettlementType.CFD})}
    )

    evaluation = risk_manager.evaluate(cfd, cfd_context)

    assert RiskViolationCode.CFD_NOT_ALLOWED in violation_codes(evaluation)


def test_no_other_derivative_execution(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    instrument: InstrumentMetadata,
) -> None:
    derivative = proposal.model_copy(update={"settlement_type": SettlementType.REAL_FUTURES})
    derivative_context = context.model_copy(
        update={
            "instrument": instrument.model_copy(
                update={"settlement_type": SettlementType.REAL_FUTURES}
            )
        }
    )

    evaluation = risk_manager.evaluate(derivative, derivative_context)

    assert RiskViolationCode.DERIVATIVE_NOT_ALLOWED in violation_codes(evaluation)


def test_no_martingale(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    martingale = proposal.model_copy(update={"is_martingale": True})

    evaluation = risk_manager.evaluate(martingale, context)

    assert RiskViolationCode.MARTINGALE_NOT_ALLOWED in violation_codes(evaluation)


def test_no_blind_averaging_down(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    portfolio: PortfolioSnapshot,
) -> None:
    losing_target = portfolio.positions[0].model_copy(update={"average_entry_price": Decimal("12")})
    losing_portfolio = portfolio.model_copy(
        update={"positions": (losing_target, portfolio.positions[1])}
    )
    blind = proposal.model_copy(
        update={
            "is_averaging_down": True,
            "thesis_revalidated": False,
            "evidence": (),
            "invalidation_conditions": (),
        }
    )

    evaluation = risk_manager.evaluate(
        blind, context.model_copy(update={"portfolio": losing_portfolio})
    )

    assert RiskViolationCode.BLIND_AVERAGING_DOWN in violation_codes(evaluation)


def test_revalidated_averaging_down_can_continue_to_other_risk_checks(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    portfolio: PortfolioSnapshot,
) -> None:
    losing_target = portfolio.positions[0].model_copy(update={"average_entry_price": Decimal("12")})
    losing_portfolio = portfolio.model_copy(
        update={"positions": (losing_target, portfolio.positions[1])}
    )
    revalidated = proposal.model_copy(
        update={"is_averaging_down": True, "thesis_revalidated": True}
    )

    evaluation = risk_manager.evaluate(
        revalidated, context.model_copy(update={"portfolio": losing_portfolio})
    )

    assert RiskViolationCode.BLIND_AVERAGING_DOWN not in violation_codes(evaluation)


def test_no_all_in_trade(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    all_in = proposal.model_copy(update={"amount": Decimal("160")})

    evaluation = risk_manager.evaluate(all_in, context)

    assert RiskViolationCode.ALL_IN_NOT_ALLOWED in violation_codes(evaluation)


def test_maximum_new_trade_size_is_ten_percent(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    too_large = proposal.model_copy(update={"amount": Decimal("20.01")})

    evaluation = risk_manager.evaluate(too_large, context)

    assert RiskViolationCode.MAX_TRADE_SIZE_EXCEEDED in violation_codes(evaluation)


def test_trade_at_exact_ten_percent_limit_is_allowed(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    at_limit = proposal.model_copy(update={"amount": Decimal("20")})

    evaluation = risk_manager.evaluate(at_limit, context)

    assert RiskViolationCode.MAX_TRADE_SIZE_EXCEEDED not in violation_codes(evaluation)


def test_maximum_single_position_exposure_is_twenty_five_percent(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    portfolio: PortfolioSnapshot,
) -> None:
    target = portfolio.positions[0].model_copy(update={"units": Decimal("4.5")})
    concentrated = portfolio.model_copy(
        update={
            "cash": Decimal("135"),
            "positions": (target, portfolio.positions[1]),
            "reported_total_value": Decimal("200"),
        }
    )

    evaluation = risk_manager.evaluate(
        proposal, context.model_copy(update={"portfolio": concentrated})
    )

    assert RiskViolationCode.MAX_POSITION_EXPOSURE_EXCEEDED in violation_codes(evaluation)


def test_minimum_cash_reserve_is_ten_percent(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    portfolio: PortfolioSnapshot,
) -> None:
    large_other = Position(
        position_id="synthetic-position-large-other",
        instrument_id=OTHER_TEST_INSTRUMENT_ID,
        symbol="OTHER",
        settlement_type=SettlementType.REAL,
        units=Decimal("15.5"),
        average_entry_price=Decimal("10"),
        market_price=Decimal("10"),
    )
    low_cash = portfolio.model_copy(
        update={
            "cash": Decimal("25"),
            "positions": (portfolio.positions[0], large_other),
            "reported_total_value": Decimal("200"),
        }
    )

    evaluation = risk_manager.evaluate(proposal, context.model_copy(update={"portfolio": low_cash}))

    assert RiskViolationCode.MIN_CASH_RESERVE_BREACHED in violation_codes(evaluation)


def test_maximum_daily_new_trades_is_three(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    limit_reached = context.model_copy(update={"daily_new_trade_count": 3})

    evaluation = risk_manager.evaluate(proposal, limit_reached)

    assert RiskViolationCode.MAX_DAILY_TRADES_EXCEEDED in violation_codes(evaluation)


def test_stale_price_is_rejected(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    price: PriceSnapshot,
) -> None:
    stale = price.model_copy(update={"as_of": context.evaluated_at - timedelta(seconds=301)})

    evaluation = risk_manager.evaluate(proposal, context.model_copy(update={"price": stale}))

    assert RiskViolationCode.STALE_PRICE in violation_codes(evaluation)


def test_future_price_is_rejected(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    price: PriceSnapshot,
) -> None:
    future = price.model_copy(update={"as_of": context.evaluated_at + timedelta(seconds=1)})

    evaluation = risk_manager.evaluate(proposal, context.model_copy(update={"price": future}))

    assert RiskViolationCode.STALE_PRICE in violation_codes(evaluation)


def test_missing_market_data_is_rejected(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    unavailable = context.model_copy(update={"market_data_available": False, "price": None})

    evaluation = risk_manager.evaluate(proposal, unavailable)

    assert RiskViolationCode.MARKET_DATA_UNAVAILABLE in violation_codes(evaluation)


def test_missing_news_data_is_rejected(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    evaluation = risk_manager.evaluate(
        proposal, context.model_copy(update={"news_data_available": False})
    )

    assert RiskViolationCode.NEWS_DATA_UNAVAILABLE in violation_codes(evaluation)


def test_invalid_instrument_metadata_is_rejected(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    instrument: InstrumentMetadata,
) -> None:
    invalid = instrument.model_copy(update={"is_valid": False})

    evaluation = risk_manager.evaluate(proposal, context.model_copy(update={"instrument": invalid}))

    assert RiskViolationCode.INVALID_INSTRUMENT_METADATA in violation_codes(evaluation)


def test_duplicate_order_is_rejected(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    duplicate = context.model_copy(
        update={"recent_idempotency_keys": frozenset({proposal.idempotency_key})}
    )

    evaluation = risk_manager.evaluate(proposal, duplicate)

    assert RiskViolationCode.DUPLICATE_ORDER in violation_codes(evaluation)


def test_inconsistent_portfolio_state_is_rejected(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    portfolio: PortfolioSnapshot,
) -> None:
    inconsistent = portfolio.model_copy(update={"reported_total_value": Decimal("201")})

    evaluation = risk_manager.evaluate(
        proposal, context.model_copy(update={"portfolio": inconsistent})
    )

    assert RiskViolationCode.INCONSISTENT_PORTFOLIO_STATE in violation_codes(evaluation)


def test_inconsistent_api_state_is_rejected(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    evaluation = risk_manager.evaluate(
        proposal, context.model_copy(update={"api_state_consistent": False})
    )

    assert RiskViolationCode.INCONSISTENT_API_STATE in violation_codes(evaluation)


def test_critical_error_halts_trading_and_activates_kill_switch(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    kill_switch: KillSwitch,
) -> None:
    evaluation = risk_manager.evaluate(
        proposal,
        context.model_copy(update={"critical_operational_error": "synthetic outage"}),
    )

    codes = violation_codes(evaluation)
    assert RiskViolationCode.CRITICAL_OPERATIONAL_ERROR in codes
    assert RiskViolationCode.KILL_SWITCH_ACTIVE in codes
    assert kill_switch.state.active is True


@pytest.mark.parametrize(
    ("intent", "side"),
    [
        (TradeIntent.OPEN, TradeSide.BUY),
        (TradeIntent.INCREASE, TradeSide.BUY),
        (TradeIntent.REDUCE, TradeSide.SELL),
        (TradeIntent.CLOSE, TradeSide.SELL),
    ],
)
def test_kill_switch_blocks_every_new_order(
    now: datetime,
    proposal: TradeProposal,
    context: RiskContext,
    intent: TradeIntent,
    side: TradeSide,
) -> None:
    switch = KillSwitch(active=True, reason="synthetic halt", clock=lambda: now)
    manager = RiskManager(
        policy=RiskPolicyConfig(),
        kill_switch=switch,
        authorization_key=b"test-risk-authorization-key-32b!",
        clock=lambda: now,
    )
    candidate = proposal.model_copy(update={"intent": intent, "side": side})

    evaluation = manager.evaluate(candidate, context)

    assert RiskViolationCode.KILL_SWITCH_ACTIVE in violation_codes(evaluation)


def test_confidence_below_threshold_is_rejected(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    low_confidence = proposal.model_copy(update={"confidence": Decimal("0.69")})

    evaluation = risk_manager.evaluate(low_confidence, context)

    assert RiskViolationCode.CONFIDENCE_BELOW_MINIMUM in violation_codes(evaluation)


def test_v1_legacy_confidence_threshold_is_unchanged(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    legacy = proposal.model_copy(
        update={
            "confidence": Decimal("0.69"),
            "confidence_model_version": "V1_LEGACY",
            "confidence_semantics_version": "MIXED_SIGNAL_AND_MARKET_QUALITY_V1",
        }
    )

    evaluation = risk_manager.evaluate(legacy, context)

    assert RiskViolationCode.CONFIDENCE_BELOW_MINIMUM in violation_codes(evaluation)


def test_v2b_signal_reliability_does_not_use_duplicate_legacy_floor(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    guarded = proposal.model_copy(
        update={
            "confidence": Decimal("0.5550"),
            "confidence_model_version": "V2_B_GUARDED_V1",
            "confidence_semantics_version": "SIGNAL_RELIABILITY_V2",
            "confidence_threshold": V2_B_THRESHOLD,
            "confidence_threshold_provenance": V2_B_THRESHOLD_PROVENANCE,
        }
    )

    evaluation = risk_manager.evaluate(guarded, context)

    assert RiskViolationCode.CONFIDENCE_BELOW_MINIMUM not in violation_codes(evaluation)
    assert evaluation.decision.status is RiskDecisionStatus.APPROVED


def test_v2b_below_guarded_threshold_still_fails_closed(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    below = proposal.model_copy(
        update={
            "confidence": Decimal("0.5474"),
            "confidence_model_version": "V2_B_GUARDED_V1",
            "confidence_semantics_version": "SIGNAL_RELIABILITY_V2",
            "confidence_threshold": V2_B_THRESHOLD,
            "confidence_threshold_provenance": V2_B_THRESHOLD_PROVENANCE,
        }
    )

    evaluation = risk_manager.evaluate(below, context)

    assert RiskViolationCode.CONFIDENCE_BELOW_MINIMUM in violation_codes(evaluation)


def test_unknown_or_mismatched_confidence_semantics_fail_closed(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    mismatched = proposal.model_copy(
        update={
            "confidence": Decimal("0.90"),
            "confidence_model_version": "V2_B_GUARDED_V1",
            "confidence_semantics_version": "MIXED_SIGNAL_AND_MARKET_QUALITY_V1",
            "confidence_threshold": V2_B_THRESHOLD,
            "confidence_threshold_provenance": V2_B_THRESHOLD_PROVENANCE,
        }
    )
    missing_threshold = proposal.model_copy(
        update={
            "confidence": Decimal("0.90"),
            "confidence_model_version": "V2_B_GUARDED_V1",
            "confidence_semantics_version": "SIGNAL_RELIABILITY_V2",
            "confidence_threshold": None,
            "confidence_threshold_provenance": None,
        }
    )

    first = risk_manager.evaluate(mismatched, context)
    second = risk_manager.evaluate(missing_threshold, context)

    assert RiskViolationCode.INVALID_CONFIDENCE_SEMANTICS in violation_codes(first)
    assert RiskViolationCode.INVALID_CONFIDENCE_SEMANTICS in violation_codes(second)


def test_v2b_confidence_alignment_keeps_other_risk_checks_active(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    too_large = proposal.model_copy(
        update={
            "amount": Decimal("20.01"),
            "confidence": Decimal("0.5550"),
            "confidence_model_version": "V2_B_GUARDED_V1",
            "confidence_semantics_version": "SIGNAL_RELIABILITY_V2",
            "confidence_threshold": V2_B_THRESHOLD,
            "confidence_threshold_provenance": V2_B_THRESHOLD_PROVENANCE,
        }
    )

    evaluation = risk_manager.evaluate(too_large, context)

    assert RiskViolationCode.CONFIDENCE_BELOW_MINIMUM not in violation_codes(evaluation)
    assert RiskViolationCode.MAX_TRADE_SIZE_EXCEEDED in violation_codes(evaluation)


def test_currency_mismatch_is_rejected(
    risk_manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> None:
    from app.domain.enums import Currency

    mismatch = proposal.model_copy(update={"currency": Currency.EUR})

    evaluation = risk_manager.evaluate(mismatch, context)

    assert RiskViolationCode.CURRENCY_MISMATCH in violation_codes(evaluation)
