from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.brokers.etoro.mapping import map_quote
from app.data.historical.etoro import _normalize_etoro_candles
from app.data.historical.cache import HistoricalDataCache
from app.data.models import ProviderInstrumentReference
from app.domain.enums import AssetClass, Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import TimeFrame

NOW = datetime(2026, 9, 14, 4, tzinfo=UTC)


@pytest.mark.parametrize("volume", [None, "0", "12.345"])
def test_candle_volume_is_preserved_through_cache(tmp_path, volume):
    instrument = UniversalInstrument(broker="etoro", broker_instrument_id="100000",
        symbol="BTC", asset_class=AssetClass.CRYPTO, currency=Currency.USD, metadata_timestamp=NOW)
    raw = {"candles": [{"volume": "999", "candles": [{"fromDate": NOW.isoformat(),
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": volume}]}]}
    bars = _normalize_etoro_candles(raw, instrument=instrument, timeframe=TimeFrame.ONE_HOUR)
    expected = None if volume is None else Decimal(volume)
    assert bars[0].volume == expected  # group volume must never fill a missing candle volume
    cache = HistoricalDataCache(tmp_path / "bars.sqlite3")
    mapping = ProviderInstrumentReference(provider="etoro", provider_symbol="BTC", broker="etoro",
        broker_symbol="BTC", broker_instrument_id="100000", asset_class=AssetClass.CRYPTO,
        currency=Currency.USD, mapping_confidence=Decimal("1"), mapping_source="test", verified=True)
    cache.upsert_bars(provider="etoro", bars=bars, fetched_at=NOW, mapping=mapping)
    restored = cache.get_bars(provider="etoro", instrument_key=("etoro", "100000"),
        timeframe=TimeFrame.ONE_HOUR, as_of=NOW, limit=1, instrument_factory=instrument.model_dump())
    assert restored[0].volume == expected
    assert restored[0].instrument.bid is None and restored[0].instrument.ask is None


def test_quote_mapping_preserves_bid_ask_and_provider_timestamp():
    quote = map_quote({"rates": [{"instrumentID": 100000, "date": NOW.isoformat(),
        "bid": "100", "ask": "100.01", "lastExecution": "100"}]},
        instrument_id=100000, symbol="BTC", currency=Currency.USD)
    assert (quote.bid, quote.ask, quote.as_of) == (Decimal("100"), Decimal("100.01"), NOW)
