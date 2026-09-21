"""Provider-neutral validation for live market observations."""

from datetime import datetime, timedelta

from app.domain.market import InstrumentMetadata, MarketQuote


class MarketObservationError(ValueError):
    pass


def validate_market_observation(
    quote: MarketQuote,
    instrument: InstrumentMetadata,
    *,
    now: datetime,
    maximum_age_seconds: int,
    maximum_future_skew_seconds: int = 0,
) -> None:
    if quote.instrument_id != instrument.instrument_id or quote.symbol != instrument.symbol:
        raise MarketObservationError("quote and instrument mapping do not match")
    if not instrument.is_valid or not instrument.is_tradable:
        raise MarketObservationError("instrument mapping is not verified and tradeable")
    age = now - quote.as_of
    if age < -timedelta(seconds=maximum_future_skew_seconds):
        raise MarketObservationError("quote timestamp is in the future")
    if age > timedelta(seconds=maximum_age_seconds):
        raise MarketObservationError("quote is stale")
    if quote.bid is None or quote.ask is None:
        raise MarketObservationError("bid and ask are required")
