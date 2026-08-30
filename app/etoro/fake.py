"""Legacy deterministic read-only fake retained for compatibility tests."""

from collections.abc import Mapping

from app.domain.market import InstrumentMetadata, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot
from app.etoro.errors import EtoroDataError


class FakeEtoroReadClient:
    def __init__(
        self,
        *,
        portfolio: PortfolioSnapshot,
        instruments: Mapping[int, InstrumentMetadata],
        prices: Mapping[int, PriceSnapshot],
    ) -> None:
        self._portfolio = portfolio
        self._instruments = dict(instruments)
        self._prices = dict(prices)

    def get_portfolio(self) -> PortfolioSnapshot:
        return self._portfolio

    def get_instrument_metadata(self, instrument_id: int) -> InstrumentMetadata:
        try:
            return self._instruments[instrument_id]
        except KeyError as exc:
            raise EtoroDataError("instrument metadata is unavailable") from exc

    def get_price(self, instrument_id: int) -> PriceSnapshot:
        try:
            return self._prices[instrument_id]
        except KeyError as exc:
            raise EtoroDataError("instrument price is unavailable") from exc
