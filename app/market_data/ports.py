"""Market-data abstraction independent of any vendor endpoint."""

from datetime import datetime
from typing import Protocol

from app.domain.enums import Currency
from app.domain.market import InstrumentMetadata, MarketQuote, PriceSnapshot


class MarketDataProvider(Protocol):
    def get_quote(
        self,
        symbol: str,
        *,
        as_of: datetime,
        expected_currency: Currency | None = None,
    ) -> MarketQuote: ...

    def resolve_instrument(self, symbol: str) -> InstrumentMetadata: ...


class MarketDataService(Protocol):
    def resolve_instrument(self, symbol: str) -> InstrumentMetadata: ...

    def current_price(self, instrument_id: int) -> PriceSnapshot: ...

    def prices_are_fresh(self, *, as_of: datetime) -> bool: ...
