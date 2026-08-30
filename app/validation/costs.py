"""Broker-neutral transaction cost and slippage modeling."""

from decimal import Decimal

from app.domain.enums import Currency
from app.domain.market import MarketQuote
from app.validation.models import (
    SlippageScenario,
    TransactionCostAssumptions,
    TransactionCostEstimate,
)


class TransactionCostModel:
    def __init__(self, assumptions: TransactionCostAssumptions | None = None) -> None:
        self._assumptions = assumptions or TransactionCostAssumptions()

    @property
    def assumptions(self) -> TransactionCostAssumptions:
        return self._assumptions

    def estimate(
        self,
        *,
        quote: MarketQuote,
        gross_value: Decimal,
        portfolio_currency: Currency,
        holding_days: int = 0,
    ) -> TransactionCostEstimate:
        if gross_value < 0:
            raise ValueError("gross value cannot be negative")
        spread_rate = _observed_spread_rate(quote) or self._assumptions.spread_assumption
        slippage_rate = _scenario_slippage_rate(self._assumptions)
        unknowns: list[str] = []
        fx_rate = self._assumptions.fx_conversion_rate
        if quote.currency is not portfolio_currency and fx_rate is None:
            unknowns.append("FX_CONVERSION_COST")
            fx_rate = Decimal("0")
        overnight_rate = self._assumptions.overnight_rate
        if holding_days > 0 and overnight_rate is None:
            unknowns.append("OVERNIGHT_OR_FINANCING_COST")
            overnight_rate = Decimal("0")
        spread_cost = gross_value * spread_rate / Decimal("2")
        slippage_cost = gross_value * slippage_rate
        broker_fee = gross_value * self._assumptions.broker_fee_rate
        fx_cost = gross_value * (fx_rate or Decimal("0"))
        overnight_cost = gross_value * (overnight_rate or Decimal("0")) * Decimal(holding_days)
        return TransactionCostEstimate(
            gross_value=gross_value,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            broker_fee=broker_fee,
            fx_cost=fx_cost,
            overnight_cost=overnight_cost,
            total_cost=spread_cost + slippage_cost + broker_fee + fx_cost + overnight_cost,
            complete=not unknowns,
            unknown_material_costs=tuple(unknowns),
            provenance=self._assumptions.provenance,
            scenario=self._assumptions.scenario,
        )


def _observed_spread_rate(quote: MarketQuote) -> Decimal | None:
    if quote.bid is None or quote.ask is None:
        return None
    mid = (quote.bid + quote.ask) / Decimal("2")
    if mid <= 0:
        return None
    return (quote.ask - quote.bid) / mid


def _scenario_slippage_rate(assumptions: TransactionCostAssumptions) -> Decimal:
    multiplier = {
        SlippageScenario.IDEAL: Decimal("0"),
        SlippageScenario.BASE: Decimal("1"),
        SlippageScenario.STRESSED: Decimal("2"),
        SlippageScenario.SEVERE: Decimal("4"),
    }[assumptions.scenario]
    return assumptions.slippage_rate * multiplier
