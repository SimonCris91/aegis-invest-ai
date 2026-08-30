"""Deterministic offline market-data providers and fail-closed validation."""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError

from app.domain.enums import Currency
from app.domain.market import InstrumentMetadata, MarketQuote


class MarketDataError(RuntimeError):
    """Base error for normalized market-data failures."""


class MarketDataUnavailableError(MarketDataError):
    pass


class UnsupportedSymbolError(MarketDataError):
    pass


class StaleMarketDataError(MarketDataError):
    pass


class MarketDataCurrencyMismatchError(MarketDataError):
    pass


class MalformedMarketDataError(MarketDataError):
    pass


class FakeMarketDataProvider:
    def __init__(
        self,
        *,
        quotes: Mapping[str, MarketQuote],
        instruments: Mapping[str, InstrumentMetadata],
        max_age_seconds: int = 300,
        unavailable: bool = False,
    ) -> None:
        self._quotes = {symbol.casefold(): quote for symbol, quote in quotes.items()}
        self._instruments = {
            symbol.casefold(): instrument for symbol, instrument in instruments.items()
        }
        self._max_age = timedelta(seconds=max_age_seconds)
        self._unavailable = unavailable

    def get_quote(
        self,
        symbol: str,
        *,
        as_of: datetime,
        expected_currency: Currency | None = None,
    ) -> MarketQuote:
        if self._unavailable:
            raise MarketDataUnavailableError("market-data provider is unavailable")
        try:
            quote = self._quotes[symbol.casefold()]
        except KeyError as exc:
            raise UnsupportedSymbolError(f"unsupported market symbol: {symbol}") from exc
        quote_age = as_of - quote.as_of
        if quote_age < timedelta(0) or quote_age > self._max_age:
            raise StaleMarketDataError(f"stale quote for symbol: {symbol}")
        if expected_currency is not None and quote.currency is not expected_currency:
            raise MarketDataCurrencyMismatchError(
                f"quote currency for {symbol} does not match the portfolio"
            )
        return quote

    def resolve_instrument(self, symbol: str) -> InstrumentMetadata:
        if self._unavailable:
            raise MarketDataUnavailableError("market-data provider is unavailable")
        try:
            return self._instruments[symbol.casefold()]
        except KeyError as exc:
            raise UnsupportedSymbolError(f"unsupported instrument symbol: {symbol}") from exc


class FixtureMarketDataProvider(FakeMarketDataProvider):
    """Normalizes supplied fixture dictionaries; it never performs network I/O."""

    def __init__(
        self,
        *,
        quote_records: Iterable[Mapping[str, object]],
        instrument_records: Iterable[Mapping[str, object]],
        max_age_seconds: int = 300,
    ) -> None:
        try:
            quotes = tuple(MarketQuote.model_validate(record) for record in quote_records)
            instruments = tuple(
                InstrumentMetadata.model_validate(record) for record in instrument_records
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise MalformedMarketDataError("malformed market-data fixture") from exc
        super().__init__(
            quotes={quote.symbol: quote for quote in quotes},
            instruments={instrument.symbol: instrument for instrument in instruments},
            max_age_seconds=max_age_seconds,
        )
