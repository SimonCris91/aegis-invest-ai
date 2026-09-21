import json
from datetime import UTC, datetime

import pytest

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse
from app.brokers.etoro.mapping import EtoroMappingError
from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.universe import UniversalInstrument
from app.orchestration.session_state import enrich_instrument_session_state
from app.storage.sqlite import SqliteRecordStore

NOW = datetime(2026, 9, 13, 10, tzinfo=UTC)


class Transport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request(self, method, url, headers, body=None):
        assert method == "GET"
        self.calls.append(url)
        return self.payloads.pop(0)


def response(items):
    return HttpResponse(200, {}, json.dumps({"items": items}).encode())


def row(**changes):
    return {"instrumentId": 1124, "internalSymbolFull": "EA", "isDelisted": True,
            "isExchangeOpen": False, "isCurrentlyTradable": True,
            "isBuyEnabled": True, **changes}


def client(transport):
    return EtoroReadClient(EtoroCredentials(api_key="fixture", user_key="fixture"),
                           DisciplinedHttpClient(transport, max_read_attempts=1))


@pytest.mark.parametrize("exchange_open", [False, True])
def test_recovers_exact_delisted_id_but_never_admits_trading(tmp_path, exchange_open):
    transport = Transport([response([]), response([row(isExchangeOpen=exchange_open)])])
    store = SqliteRecordStore(tmp_path / "session.sqlite3")
    instrument = UniversalInstrument(broker="etoro", broker_instrument_id="1124", symbol="EA",
        asset_class=AssetClass.EQUITY, currency=Currency.USD,
        market_status=MarketStatus.UNKNOWN, metadata_timestamp=NOW)
    result = enrich_instrument_session_state(client=client(transport), store=store,
        instruments=(instrument,), as_of=NOW, concurrency=1)
    assert result[0].tradeable is False
    assert result[0].buy_allowed is False
    state = store.etoro_session_states(as_of=NOW)["1124"]
    assert state["session_state"] == ("OPEN_NOT_TRADABLE" if exchange_open else "CLOSED")
    assert state["error_class"] == "DELISTED"
    assert len(transport.calls) == 2
    assert "isDelisted=true" in transport.calls[1]


@pytest.mark.parametrize("item", [row(instrumentId=999), row(isDelisted=False)])
def test_fallback_rejects_wrong_id_or_unconfirmed_delisting(item):
    transport = Transport([response([]), response([item])])
    with pytest.raises(EtoroMappingError):
        client(transport).resolve_session_instrument_id(1124, symbol="EA", as_of=NOW)


def test_fallback_rate_limit_is_not_retried():
    transport = Transport([response([]), HttpResponse(429, {}, b"{}")])
    with pytest.raises(EtoroApiError) as error:
        client(transport).resolve_session_instrument_id(1124, symbol="EA", as_of=NOW)
    assert error.value.status == 429
    assert len(transport.calls) == 2


def test_execution_lookup_does_not_use_diagnostic_fallback():
    transport = Transport([response([])])
    with pytest.raises(EtoroMappingError):
        client(transport).resolve_instrument_id(1124, symbol="EA", as_of=NOW)
    assert len(transport.calls) == 1
