from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.brokers.market_validation import MarketObservationError, validate_market_observation
from app.domain.enums import Currency, MarketStatus
from app.domain.market import InstrumentMetadata, MarketQuote
from app.market_data.providers import (
    FakeMarketDataProvider,
    FixtureMarketDataProvider,
    MalformedMarketDataError,
    MarketDataCurrencyMismatchError,
    MarketDataUnavailableError,
    StaleMarketDataError,
    UnsupportedSymbolError,
)


def provider(quote: MarketQuote, instrument: InstrumentMetadata) -> FakeMarketDataProvider:
    return FakeMarketDataProvider(
        quotes={quote.symbol: quote}, instruments={quote.symbol: instrument}
    )


def test_valid_quote_is_normalized(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    result = provider(market_quote, instrument).get_quote(
        "test", as_of=now, expected_currency=Currency.USD
    )

    assert result == market_quote
    assert result.market_status is MarketStatus.OPEN


def test_stale_quote_fails_closed(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    stale = market_quote.model_copy(update={"as_of": now - timedelta(seconds=301)})

    with pytest.raises(StaleMarketDataError):
        provider(stale, instrument).get_quote("TEST", as_of=now)


def test_future_quote_fails_closed(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    future = market_quote.model_copy(update={"as_of": now + timedelta(seconds=1)})

    with pytest.raises(StaleMarketDataError):
        provider(future, instrument).get_quote("TEST", as_of=now)


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
def test_non_positive_quote_is_invalid(now: datetime, price: Decimal) -> None:
    with pytest.raises(ValidationError):
        MarketQuote(
            instrument_id=1,
            symbol="TEST",
            price=price,
            as_of=now,
            currency=Currency.USD,
            source="fixture",
        )


def test_missing_or_malformed_timestamp_fails_fixture_normalization() -> None:
    record: dict[str, object] = {
        "instrument_id": 1,
        "symbol": "TEST",
        "price": "10",
        "currency": "USD",
        "source": "fixture",
    }

    with pytest.raises(MalformedMarketDataError):
        FixtureMarketDataProvider(quote_records=(record,), instrument_records=())

    record["as_of"] = "not-a-timestamp"
    with pytest.raises(MalformedMarketDataError):
        FixtureMarketDataProvider(quote_records=(record,), instrument_records=())


def test_provider_failure_is_explicit(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    offline = FakeMarketDataProvider(
        quotes={"TEST": market_quote}, instruments={"TEST": instrument}, unavailable=True
    )

    with pytest.raises(MarketDataUnavailableError):
        offline.get_quote("TEST", as_of=now)


def test_unsupported_symbol_is_rejected(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    with pytest.raises(UnsupportedSymbolError):
        provider(market_quote, instrument).get_quote("UNKNOWN", as_of=now)


def test_currency_mismatch_is_rejected(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    with pytest.raises(MarketDataCurrencyMismatchError):
        provider(market_quote, instrument).get_quote(
            "TEST", as_of=now, expected_currency=Currency.EUR
        )


def test_inverted_bid_ask_is_invalid(now: datetime) -> None:
    with pytest.raises(ValidationError, match="bid cannot exceed ask"):
        MarketQuote(
            instrument_id=1,
            symbol="TEST",
            price=Decimal("10"),
            as_of=now,
            currency=Currency.USD,
            source="fixture",
            bid=Decimal("11"),
            ask=Decimal("10"),
        )


def test_quote_freshness_is_deterministic(now: datetime, market_quote: MarketQuote) -> None:
    assert market_quote.is_fresh(as_of=now, max_age_seconds=300) is True
    assert market_quote.is_fresh(as_of=now + timedelta(seconds=301), max_age_seconds=300) is False
    with pytest.raises(ValueError, match="timezone"):
        market_quote.is_fresh(as_of=now.replace(tzinfo=None), max_age_seconds=300)


def test_small_future_quote_skew_can_be_tolerated(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    future = market_quote.model_copy(update={"as_of": now + timedelta(seconds=3)})

    validate_market_observation(
        future,
        instrument,
        now=now,
        maximum_age_seconds=300,
        maximum_future_skew_seconds=5,
    )


def test_large_future_quote_skew_is_rejected_as_future(
    now: datetime, market_quote: MarketQuote, instrument: InstrumentMetadata
) -> None:
    future = market_quote.model_copy(update={"as_of": now + timedelta(seconds=6)})

    with pytest.raises(MarketObservationError, match="future"):
        validate_market_observation(
            future,
            instrument,
            now=now,
            maximum_age_seconds=300,
            maximum_future_skew_seconds=5,
        )
