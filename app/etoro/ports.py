"""Legacy read-only eToro protocol retained for Step 3 compatibility."""

from typing import Protocol

from app.domain.market import InstrumentMetadata, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot


class EtoroReadClient(Protocol):
    def get_portfolio(self) -> PortfolioSnapshot: ...

    def get_instrument_metadata(self, instrument_id: int) -> InstrumentMetadata: ...

    def get_price(self, instrument_id: int) -> PriceSnapshot: ...
