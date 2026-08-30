"""Ports for broker-neutral market scanning."""

from datetime import datetime
from typing import Protocol

from app.brokers.models import BrokerCapabilities
from app.domain.market import MarketQuote
from app.domain.universe import BrokerEligibilitySnapshot, UniversalInstrument


class InstrumentDiscoveryProvider(Protocol):
    def discover_instruments(
        self, *, as_of: datetime, limit: int
    ) -> tuple[UniversalInstrument, ...]: ...


class MarketDataProvider(Protocol):
    def quote(self, instrument: UniversalInstrument, *, as_of: datetime) -> MarketQuote | None: ...


class BrokerEligibilityProvider(Protocol):
    def eligibility(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> BrokerEligibilitySnapshot: ...


class BrokerCapabilitiesProvider(Protocol):
    @property
    def capabilities(self) -> BrokerCapabilities: ...


class MarketScannerAdapter(
    InstrumentDiscoveryProvider,
    MarketDataProvider,
    BrokerEligibilityProvider,
    BrokerCapabilitiesProvider,
    Protocol,
):
    """Read-only broker adapter used by the scanner.

    The protocol intentionally has no execution method.
    """
