from datetime import datetime

import pytest

from app.domain.market import InstrumentMetadata, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot
from app.etoro.errors import EtoroDataError
from app.etoro.fake import FakeEtoroReadClient

from .conftest import TEST_INSTRUMENT_ID


def test_fake_client_is_read_only_and_returns_seeded_data(
    portfolio: PortfolioSnapshot,
    instrument: InstrumentMetadata,
    price: PriceSnapshot,
    now: datetime,
) -> None:
    del now
    client = FakeEtoroReadClient(
        portfolio=portfolio,
        instruments={TEST_INSTRUMENT_ID: instrument},
        prices={TEST_INSTRUMENT_ID: price},
    )

    assert client.get_portfolio() == portfolio
    assert client.get_instrument_metadata(TEST_INSTRUMENT_ID) == instrument
    assert client.get_price(TEST_INSTRUMENT_ID) == price
    assert not hasattr(client, "submit_order")
    assert not hasattr(client, "place_trade")


def test_fake_client_normalizes_missing_data(portfolio: PortfolioSnapshot) -> None:
    client = FakeEtoroReadClient(
        portfolio=portfolio,
        instruments={},
        prices={},
    )
    with pytest.raises(EtoroDataError, match="metadata is unavailable"):
        client.get_instrument_metadata(TEST_INSTRUMENT_ID)
