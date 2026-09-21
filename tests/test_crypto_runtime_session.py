from datetime import UTC, datetime

import pytest

from app.brokers.etoro.mapping import map_instrument_resolution
from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.universe import UniversalInstrument
from app.orchestration.session_state import enrich_instrument_session_state
from app.storage.sqlite import SqliteRecordStore

NOW = datetime(2026, 9, 13, 18, tzinfo=UTC)


@pytest.mark.parametrize("blocked_field", [None, "isBuyEnabled", "isCurrentlyTradable", "isActiveInPlatform", "isDelisted", "isHiddenFromClient", "isInternalInstrument"])
def test_runtime_crypto_uses_same_authoritative_flags_as_preflight(tmp_path, blocked_field):
    raw = {"instrumentId": 100000, "internalSymbolFull": "BTC", "internalAssetClassName": "Crypto",
           "isExchangeOpen": False, "isCurrentlyTradable": True, "isBuyEnabled": True,
           "isActiveInPlatform": True, "isDelisted": False, "isHiddenFromClient": False,
           "isInternalInstrument": False}
    if blocked_field:
        raw[blocked_field] = not raw[blocked_field]
    resolution = map_instrument_resolution({"items": [raw]}, symbol="BTC", as_of=NOW)

    class Client:
        def resolve_instrument_id(self, instrument_id, *, symbol, as_of):
            assert instrument_id == 100000
            return resolution

    instrument = UniversalInstrument(broker="etoro", broker_instrument_id="100000", symbol="BTC",
        asset_class=AssetClass.CRYPTO, currency=Currency.USD, market_status=MarketStatus.CONTINUOUS_24_7,
        metadata_timestamp=NOW)
    result = enrich_instrument_session_state(client=Client(), store=SqliteRecordStore(tmp_path / "sessions.sqlite3"),
        instruments=(instrument,), as_of=NOW, concurrency=1)
    assert result[0].market_status is (MarketStatus.UNKNOWN if blocked_field else MarketStatus.OPEN)
    states = SqliteRecordStore(tmp_path / "sessions.sqlite3").etoro_session_states(as_of=NOW)
    assert states["100000"]["error_class"] == (
        "CRYPTO_BUY_DISABLED" if blocked_field == "isBuyEnabled"
        else "CRYPTO_TRADABILITY_NOT_CONFIRMED" if blocked_field
        else None
    )
