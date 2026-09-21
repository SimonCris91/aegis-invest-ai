import pytest

from app.brokers.etoro.demo_preflight import (
    _preflight_market_status,
    build_first_demo_preflight_report,
)
from app.brokers.etoro.mapping import map_instrument_resolution
from app.domain.enums import MarketStatus
from tests.test_crypto_live_tradability import PAYLOAD
from tests.test_step75 import FakeReadClient, _config, _now, _values


@pytest.mark.parametrize("symbol", ["BTC", "API3", "BCH"])
def test_verified_crypto_payload_passes_preflight_market_gate(symbol):
    values = _values()
    raw = {
        **PAYLOAD,
        "instrumentId": int(values["AEGIS_ETORO_READINESS_INSTRUMENT_ID"]),
        "internalSymbolFull": symbol,
        "internalAssetClassName": "Crypto",
    }
    resolution = map_instrument_resolution({"items": [raw]}, symbol=symbol, as_of=_now())
    assert resolution.market_status is MarketStatus.CLOSED
    assert _preflight_market_status(resolution) is MarketStatus.OPEN

    class Client(FakeReadClient):
        last_server_now = _now()

    client = Client(resolution=resolution)
    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values={**values, "AEGIS_ETORO_READINESS_SYMBOL": symbol},
        client=client,
        clock=_now,
    )
    check = next(c for c in report.checks if c.name == "market_state")
    assert check.status == "PASS"
    assert report.market_status == "OPEN"


@pytest.mark.parametrize(
    "field",
    [
        "is_currently_tradable",
        "is_buy_enabled",
        "is_active_in_platform",
        "is_internal_instrument",
        "is_hidden_from_client",
        "is_delisted",
    ],
)
@pytest.mark.parametrize("invalid", [None, "opposite"])
def test_preflight_crypto_required_fields_fail_closed(field, invalid):
    resolution = map_instrument_resolution(
        {"items": [{**PAYLOAD, "internalAssetClassName": "Crypto"}]}, symbol="BTC", as_of=_now()
    )
    value = None if invalid is None else not getattr(resolution, field)
    assert (
        _preflight_market_status(resolution.model_copy(update={field: value}))
        is not MarketStatus.OPEN
    )


@pytest.mark.parametrize("kind", ["Stock", "ETF"])
def test_non_crypto_market_state_unchanged(kind):
    resolution = map_instrument_resolution(
        {"items": [{**PAYLOAD, "instrumentType": kind}]}, symbol="BTC", as_of=_now()
    )
    assert _preflight_market_status(resolution) is MarketStatus.CLOSED


def test_dash_buy_disabled_fails_preflight_market_gate():
    values = _values()
    raw = {
        **PAYLOAD,
        "internalSymbolFull": "DASH",
        "isBuyEnabled": False,
        "internalAssetClassName": "Crypto",
        "instrumentId": int(values["AEGIS_ETORO_READINESS_INSTRUMENT_ID"]),
    }
    resolution = map_instrument_resolution({"items": [raw]}, symbol="DASH", as_of=_now())
    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=values,
        client=FakeReadClient(resolution=resolution),
        clock=_now,
    )
    check = next(c for c in report.checks if c.name == "market_state")
    assert check.status == "MARKET_CLOSED"
