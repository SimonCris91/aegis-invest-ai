"""Deterministic broker fake for offline tests."""

from datetime import datetime

from app.brokers.models import (
    BrokerCapabilities,
    BrokerIdentity,
    BrokerSubmission,
    PreflightDecision,
)
from app.domain.market import InstrumentMetadata, MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import BrokerEligibilitySnapshot, UniversalInstrument
from app.execution.gate import AuthorizedTrade


class FakeBroker:
    def __init__(
        self,
        *,
        capabilities: BrokerCapabilities,
        identity: BrokerIdentity,
        portfolio: PortfolioSnapshot,
        quote: MarketQuote,
        instrument: InstrumentMetadata,
        submission: BrokerSubmission,
    ) -> None:
        self.capabilities = capabilities
        self._identity = identity
        self._portfolio = portfolio
        self._quote = quote
        self._instrument = instrument
        self._submission = submission
        self.demo_calls = 0

    def identity(self) -> BrokerIdentity:
        return self._identity

    def portfolio(self) -> PortfolioSnapshot:
        return self._portfolio

    def quote(self, instrument_id: int, symbol: str) -> MarketQuote:
        return self._quote

    def instrument(self, instrument_id: int, symbol: str) -> InstrumentMetadata:
        return self._instrument

    def submit_demo(self, trade: AuthorizedTrade, preflight: PreflightDecision) -> BrokerSubmission:
        if not preflight.allowed:
            raise PermissionError("fake Demo preflight was rejected")
        self.demo_calls += 1
        return self._submission


class FakeMarketScannerAdapter:
    def __init__(
        self,
        *,
        capabilities: BrokerCapabilities,
        instruments: tuple[UniversalInstrument, ...],
        quotes: dict[str, MarketQuote] | None = None,
        eligibilities: dict[str, BrokerEligibilitySnapshot] | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._instruments = instruments
        self._quotes = quotes or {}
        self._eligibilities = eligibilities or {}
        self.write_calls = 0

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    def discover_instruments(
        self, *, as_of: datetime, limit: int
    ) -> tuple[UniversalInstrument, ...]:
        return self._instruments[:limit]

    def quote(self, instrument: UniversalInstrument, *, as_of: datetime) -> MarketQuote | None:
        return self._quotes.get(instrument.key)

    def eligibility(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> BrokerEligibilitySnapshot:
        try:
            return self._eligibilities[instrument.key]
        except KeyError:
            return BrokerEligibilitySnapshot(
                broker=instrument.broker,
                broker_instrument_id=instrument.broker_instrument_id,
                symbol=instrument.symbol,
                checked_at=instrument.metadata_timestamp,
                currency=instrument.currency,
                verified=False,
                allow_open=False,
                reason="not configured",
            )
