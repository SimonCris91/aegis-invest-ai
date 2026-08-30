"""Narrow broker ports. There is deliberately no real execution port."""

from typing import Protocol

from app.brokers.models import (
    BrokerCapabilities,
    BrokerIdentity,
    BrokerSubmission,
    DemoEligibility,
    DemoPortfolioSnapshot,
    ExecutionState,
    FxRate,
    PreflightDecision,
)
from app.domain.enums import Currency
from app.domain.market import InstrumentMetadata, MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.execution.gate import AuthorizedTrade


class BrokerReadPort(Protocol):
    @property
    def capabilities(self) -> BrokerCapabilities: ...
    def identity(self) -> BrokerIdentity: ...
    def portfolio(self) -> PortfolioSnapshot: ...
    def quote(self, instrument_id: int, symbol: str) -> MarketQuote: ...
    def instrument(self, instrument_id: int, symbol: str) -> InstrumentMetadata: ...


class DemoReadPort(Protocol):
    def demo_portfolio(self) -> DemoPortfolioSnapshot: ...
    def demo_eligibility(self, instrument_id: int, symbol: str) -> DemoEligibility: ...
    def demo_order_state(self, instrument_id: int, order_id: str) -> ExecutionState: ...


class FxRateProvider(Protocol):
    def get_rate(self, base: Currency, quote: Currency) -> FxRate: ...


class DemoExecutionPort(Protocol):
    @property
    def capabilities(self) -> BrokerCapabilities: ...
    def submit_demo(
        self, trade: AuthorizedTrade, preflight: PreflightDecision
    ) -> BrokerSubmission: ...
