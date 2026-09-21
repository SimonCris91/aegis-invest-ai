from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.brokers.models import InstrumentResolution
from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.universe import UniversalInstrument
from app.orchestration.session_state import (
    enrich_instrument_session_state,
    session_state_for,
    session_state_reconciliation,
)
from app.storage.sqlite import SqliteRecordStore

NOW = datetime(2026, 9, 2, 10, tzinfo=UTC)


def test_session_batches_persist_and_progress(tmp_path: Path) -> None:
    instruments = tuple(UniversalInstrument(
        broker="etoro", broker_instrument_id=str(i), symbol=f"T{i}",
        asset_class=AssetClass.EQUITY, currency=Currency.USD,
        market_status=MarketStatus.UNKNOWN, metadata_timestamp=NOW,
    ) for i in range(1, 6))
    store = SqliteRecordStore(tmp_path / "batches.sqlite3")

    class Client:
        calls = 0

        def resolve_instrument(self, symbol, *, as_of):
            # Each previous tranche is durable before the next starts.
            import sqlite3
            with sqlite3.connect(tmp_path / "batches.sqlite3") as connection:
                assert connection.execute("SELECT COUNT(*) FROM etoro_session_state").fetchone()[0] == self.calls
            self.calls += 1
            return InstrumentResolution(
                instrument_id=int(symbol[1:]), symbol=symbol, internal_symbol_full=symbol,
                market_status=MarketStatus.CLOSED, is_currently_tradable=True,
                is_buy_enabled=True, is_hidden_from_client=False,
                is_delisted=False, is_active_in_platform=True,
                resolved=True, structurally_supported=True,
                structural_status="SUPPORTED", verified=True, as_of=as_of,
            )

    client = Client()
    for expected in (2, 4, 5):
        result = enrich_instrument_session_state(
            client=client, store=store, instruments=instruments, as_of=NOW,
            concurrency=1, batch_size=2,
        )
        assert client.calls == expected
        assert sum(item.market_status is MarketStatus.CLOSED for item in result) == expected
        assert len(result) == 5


def test_throttle_stops_new_tranches_and_next_poll(tmp_path: Path) -> None:
    from app.brokers.etoro.client import EtoroApiError

    instruments = tuple(UniversalInstrument(
        broker="etoro", broker_instrument_id=str(i), symbol=f"T{i}",
        asset_class=AssetClass.EQUITY, currency=Currency.USD,
        market_status=MarketStatus.UNKNOWN, metadata_timestamp=NOW,
    ) for i in range(5))
    store = SqliteRecordStore(tmp_path / "throttle.sqlite3")

    class Client:
        calls = 0

        def resolve_instrument(self, symbol, *, as_of):
            self.calls += 1
            raise EtoroApiError("limited", endpoint="fixture", status=429)

    client = Client()
    for minutes in (0, 1, 14):
        result = enrich_instrument_session_state(
            client=client, store=store, instruments=instruments,
            as_of=NOW + timedelta(minutes=minutes), concurrency=1, batch_size=5,
        )
        assert client.calls == 1
        assert all(item.market_status is MarketStatus.UNKNOWN for item in result)
    enrich_instrument_session_state(
        client=client, store=store, instruments=instruments,
        as_of=NOW + timedelta(minutes=16), concurrency=1, batch_size=5,
    )
    assert client.calls == 2


def test_rate_limit_waits_for_session_ttl(tmp_path: Path) -> None:
    from app.brokers.etoro.client import EtoroApiError

    class LimitedClient:
        calls = 0

        def resolve_instrument(self, symbol, *, as_of):
            self.calls += 1
            raise EtoroApiError('rate limited', endpoint='fixture', status=429)

    client = LimitedClient()
    store = SqliteRecordStore(tmp_path / 'rate.sqlite3')
    instrument = UniversalInstrument(
        broker='etoro', broker_instrument_id='1', symbol='TEST',
        display_name='Test', asset_class=AssetClass.EQUITY, currency=Currency.USD,
        market_status=MarketStatus.UNKNOWN, metadata_timestamp=NOW,
    )
    for minutes in (0, 1, 14):
        result = enrich_instrument_session_state(
            client=client, store=store, instruments=(instrument,),
            as_of=NOW + timedelta(minutes=minutes), concurrency=1,
        )
        assert result[0].market_status is MarketStatus.UNKNOWN
    assert client.calls == 1
    assert store.etoro_session_states(as_of=NOW)['1']['error_class'] == 'RATE_LIMITED'
    enrich_instrument_session_state(
        client=client, store=store, instruments=(instrument,),
        as_of=NOW + timedelta(minutes=16), concurrency=1,
    )
    assert client.calls == 2


def test_session_state_precedence_is_explicit() -> None:
    assert session_state_for(market_status=MarketStatus.OPEN, tradable=True) == "OPEN_TRADABLE"
    assert session_state_for(market_status=MarketStatus.OPEN, tradable=False) == "OPEN_NOT_TRADABLE"
    assert session_state_for(market_status=MarketStatus.CLOSED, tradable=True) == "CLOSED"
    assert session_state_for(market_status=MarketStatus.UNKNOWN, tradable=True) == "UNKNOWN"
    assert session_state_for(market_status=MarketStatus.OPEN, tradable=None) == "UNKNOWN"


def test_expired_session_observation_is_not_reused(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "state.sqlite3")
    store.upsert_etoro_session_state(
        state={
            "instrument_id": "1",
            "exchange_id": "NYSE",
            "is_exchange_open": True,
            "is_open": True,
            "is_currently_tradable": True,
            "is_buy_enabled": True,
            "session_state": "OPEN_TRADABLE",
            "observed_at": (NOW - timedelta(minutes=16)).isoformat(),
            "source": "test",
            "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
            "error_class": None,
        }
    )
    assert store.etoro_session_states(as_of=NOW) == {}


class ResolutionClient:
    def __init__(self, resolution: InstrumentResolution) -> None:
        self.resolution = resolution

    def resolve_instrument(self, symbol: str, *, as_of: datetime) -> InstrumentResolution:
        assert symbol == self.resolution.symbol
        assert as_of == NOW
        return self.resolution


def test_live_resolution_is_persisted_and_applied(tmp_path: Path) -> None:
    instrument = UniversalInstrument(
        broker="etoro",
        broker_instrument_id="1",
        symbol="TEST",
        display_name="Test",
        asset_class=AssetClass.EQUITY,
        currency=Currency.USD,
        exchange="NYSE",
        market_status=MarketStatus.UNKNOWN,
        metadata_timestamp=NOW,
    )
    resolution = InstrumentResolution(
        instrument_id=1,
        symbol="TEST",
        internal_symbol_full="TEST",
        display_name="Test",
        instrument_type="Stocks",
        classification_metadata={"exchangeID": "NYSE"},
        classification_evidence_source="search",
        classification_status="RAW_METADATA_RETAINED",
        market_status=MarketStatus.OPEN,
        is_currently_tradable=True,
        is_buy_enabled=True,
        is_hidden_from_client=False,
        is_delisted=False,
        is_active_in_platform=True,
        resolved=True,
        structurally_supported=True,
        structural_status="SUPPORTED",
        verified=True,
        as_of=NOW,
    )
    store = SqliteRecordStore(tmp_path / "state.sqlite3")
    enriched = enrich_instrument_session_state(
        client=ResolutionClient(resolution),
        store=store,
        instruments=(instrument,),
        as_of=NOW,
        concurrency=1,
    )
    assert enriched[0].market_status is MarketStatus.OPEN
    assert enriched[0].tradeable is True
    assert store.etoro_session_states(as_of=NOW)["1"]["session_state"] == "OPEN_TRADABLE"


def test_provider_error_state_is_retried_before_ttl_expires(tmp_path: Path) -> None:
    """A transient transport error must not become a valid session cache hit."""
    as_of = NOW
    store = SqliteRecordStore(tmp_path / "session.sqlite3")
    instrument = UniversalInstrument(
        broker="etoro",
        broker_instrument_id="1",
        symbol="TEST",
        display_name="Test",
        asset_class=AssetClass.EQUITY,
        currency=Currency.USD,
        exchange="NYSE",
        market_status=MarketStatus.UNKNOWN,
        metadata_timestamp=NOW,
    )
    store.upsert_etoro_session_state(
        state={
            "instrument_id": instrument.broker_instrument_id,
            "exchange_id": "NYSE",
            "is_exchange_open": None,
            "is_open": None,
            "is_currently_tradable": None,
            "is_buy_enabled": None,
            "session_state": "UNKNOWN",
            "observed_at": as_of.isoformat(),
            "source": "etoro-market-data-search",
            "expires_at": (as_of + timedelta(minutes=15)).isoformat(),
            "error_class": "NETWORK_TRANSPORT_ERROR",
        }
    )
    client = ResolutionClient(
        InstrumentResolution(
            instrument_id=1,
            symbol="TEST",
            internal_symbol_full="TEST",
            display_name="Test",
            instrument_type="Stocks",
            classification_metadata={"exchangeID": "NYSE"},
            classification_evidence_source="search",
            classification_status="RAW_METADATA_RETAINED",
            market_status=MarketStatus.OPEN,
            is_currently_tradable=True,
            is_buy_enabled=True,
            is_hidden_from_client=False,
            is_delisted=False,
            is_active_in_platform=True,
            resolved=True,
            structurally_supported=True,
            structural_status="SUPPORTED",
            verified=True,
            as_of=NOW,
        )
    )

    result = enrich_instrument_session_state(
        client=client, store=store, instruments=(instrument,), as_of=as_of, concurrency=1
    )

    assert result[0].market_status is MarketStatus.OPEN
    assert result[0].tradeable is True


def test_session_reconciliation_accounts_for_every_instrument_and_unknown_cause() -> None:
    instruments = tuple(
        UniversalInstrument(
            broker="etoro",
            broker_instrument_id=str(index),
            symbol=f"T{index}",
            asset_class=AssetClass.EQUITY,
            currency=Currency.USD,
            market_status=MarketStatus.UNKNOWN,
            metadata_timestamp=NOW,
        )
        for index in range(1, 5)
    )
    states = {
        "1": {
            "session_state": "OPEN_TRADABLE",
            "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
        "2": {
            "session_state": "UNKNOWN",
            "error_class": "NETWORK_TRANSPORT_ERROR",
            "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
        "3": {
            "session_state": "CLOSED",
            "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
    }

    counts, reasons = session_state_reconciliation(
        instruments=instruments, states=states, as_of=NOW
    )

    assert counts == {"OPEN_TRADABLE": 1, "UNKNOWN": 2, "CLOSED": 1}
    assert reasons == {
        "NETWORK_TRANSPORT_ERROR": 1,
        "STALE_OR_NOT_OBSERVED": 1,
    }
    assert sum(counts.values()) == len(instruments) == 4


def test_internal_instrument_is_explicitly_unknown_not_closed() -> None:
    from app.orchestration.session_state import _unknown_resolution_reason

    resolution = InstrumentResolution(
        instrument_id=610,
        symbol="ETORIAN610",
        internal_symbol_full="ETORIAN610",
        market_status=MarketStatus.UNKNOWN,
        is_exchange_open=None,
        is_open=None,
        is_currently_tradable=None,
        is_buy_enabled=None,
        is_internal_instrument=True,
        is_hidden_from_client=None,
        is_delisted=None,
        is_active_in_platform=None,
        resolved=True,
        structurally_supported=True,
        structural_status="SUPPORTED",
        verified=True,
        as_of=NOW,
    )

    assert _unknown_resolution_reason(resolution) == "UNSUPPORTED_INTERNAL_INSTRUMENT"


def test_exchange_state_prevents_false_missing_is_open_diagnostic() -> None:
    from app.orchestration.session_state import _unknown_resolution_reason

    resolution = InstrumentResolution(
        instrument_id=42,
        symbol="CLOSED42",
        internal_symbol_full="CLOSED42",
        market_status=MarketStatus.CLOSED,
        is_exchange_open=False,
        is_open=None,
        is_currently_tradable=True,
        is_buy_enabled=True,
        is_internal_instrument=False,
        is_hidden_from_client=False,
        is_delisted=False,
        is_active_in_platform=True,
        resolved=True,
        structurally_supported=True,
        structural_status="SUPPORTED",
        verified=True,
        as_of=NOW,
    )

    assert _unknown_resolution_reason(resolution) is None


def test_unsupported_internal_is_reconciled_separately_from_genuine_unknown() -> None:
    instruments = tuple(
        UniversalInstrument(
            broker="etoro",
            broker_instrument_id=str(index),
            symbol=f"T{index}",
            asset_class=AssetClass.EQUITY,
            currency=Currency.USD,
            market_status=MarketStatus.UNKNOWN,
            metadata_timestamp=NOW,
            tags=("unsupported-internal",) if index == 1 else (),
        )
        for index in range(1, 3)
    )

    counts, reasons = session_state_reconciliation(
        instruments=instruments,
        states={
            "1": {
                "session_state": "UNKNOWN",
                "error_class": "UNSUPPORTED_INTERNAL_INSTRUMENT",
                "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
            },
            "2": {
                "session_state": "UNKNOWN",
                "error_class": "MISSING_SESSION_FIELDS",
                "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
            },
        },
        as_of=NOW,
    )

    assert counts == {"UNSUPPORTED_INTERNAL": 1, "UNKNOWN": 1}
    assert reasons == {"MISSING_SESSION_FIELDS": 1}
