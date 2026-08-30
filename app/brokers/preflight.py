"""Broker-neutral, fail-closed Demo feasibility checks."""

from datetime import datetime, timedelta

from app.brokers.market_validation import MarketObservationError, validate_market_observation
from app.brokers.models import DemoEligibility, DemoPortfolioSnapshot, FxRate, PreflightDecision
from app.domain.enums import MarketStatus, SettlementType
from app.domain.market import InstrumentMetadata, MarketQuote
from app.domain.proposals import TradeProposal
from app.risk.kill_switch import KillSwitch


def evaluate_demo_preflight(
    *,
    proposal: TradeProposal,
    portfolio: DemoPortfolioSnapshot,
    eligibility: DemoEligibility,
    quote: MarketQuote,
    instrument: InstrumentMetadata,
    kill_switch: KillSwitch,
    now: datetime,
    maximum_age_seconds: int,
    fx_rate: FxRate | None = None,
) -> PreflightDecision:
    reasons: list[str] = []
    if kill_switch.state.active:
        reasons.append("kill switch is active")
    try:
        validate_market_observation(
            quote, instrument, now=now, maximum_age_seconds=maximum_age_seconds
        )
    except MarketObservationError as exc:
        reasons.append(str(exc))
    if quote.market_status is not MarketStatus.OPEN:
        reasons.append("market is not verified open")
    if proposal.instrument_id != eligibility.instrument_id or proposal.symbol != eligibility.symbol:
        reasons.append("eligibility does not match proposal")
    if not eligibility.verified or not eligibility.allow_open:
        reasons.append("Demo open eligibility is not verified")
    if eligibility.settlement_type is not SettlementType.REAL or eligibility.leverage != 1:
        reasons.append("unleveraged real-asset eligibility is unavailable")
    if proposal.amount < eligibility.minimum_position:
        reasons.append("proposal is below verified minimum position exposure")
    amount_in_account_currency = proposal.amount
    if proposal.currency is not portfolio.currency:
        if (
            fx_rate is None
            or fx_rate.base_currency is not proposal.currency
            or fx_rate.quote_currency is not portfolio.currency
            or now - fx_rate.as_of > timedelta(seconds=maximum_age_seconds)
            or now < fx_rate.as_of
        ):
            reasons.append("fresh verified FX conversion is unavailable")
        else:
            amount_in_account_currency = proposal.amount * fx_rate.rate
    if amount_in_account_currency > portfolio.cash:
        reasons.append("Demo buying power is insufficient")
    minimum = eligibility.minimum_position if eligibility.verified else None
    return PreflightDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        minimum_trade_amount=minimum,
    )
