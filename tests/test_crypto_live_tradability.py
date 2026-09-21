import pytest

from app.brokers.etoro import live_candidates as module
from tests.test_live_catalog_candidates import NOW, Client, setup_catalog

PAYLOAD = {
    "instrumentId": 1,
    "internalSymbolFull": "BTC",
    "isCurrentlyTradable": True,
    "isBuyEnabled": True,
    "isActiveInPlatform": True,
    "isInternalInstrument": False,
    "isHiddenFromClient": False,
    "isDelisted": False,
    "isExchangeOpen": False,
}


@pytest.mark.parametrize("is_open", ["ABSENT", None, False])
def test_live_btc_style_crypto_is_open(monkeypatch, is_open):
    setup_catalog(monkeypatch)
    payload = dict(PAYLOAD)
    if is_open != "ABSENT":
        payload["isOpen"] = is_open
    result = module.current_catalog_candidates(
        Client([{"items": [payload]}]), clock=lambda: NOW, live_get_cap=1
    )
    assert len(result) == 1
    assert result[0].market_status.value == "OPEN"
    assert result[0].tradeable is True


@pytest.mark.parametrize(
    "field",
    [
        "isCurrentlyTradable",
        "isBuyEnabled",
        "isActiveInPlatform",
        "isInternalInstrument",
        "isHiddenFromClient",
        "isDelisted",
    ],
)
@pytest.mark.parametrize("invalid", ["ABSENT", None, "true", "false", 0, 1, {}, "OPPOSITE"])
def test_each_required_crypto_field_fails_closed(monkeypatch, field, invalid):
    setup_catalog(monkeypatch)
    payload = dict(PAYLOAD)
    if invalid == "ABSENT":
        del payload[field]
    else:
        payload[field] = not PAYLOAD[field] if invalid == "OPPOSITE" else invalid
    assert (
        module.current_catalog_candidates(
            Client([{"items": [payload]}]), clock=lambda: NOW, live_get_cap=1
        )
        == ()
    )


def test_live_dash_style_buy_disabled_stays_excluded(monkeypatch):
    setup_catalog(monkeypatch)
    payload = {**PAYLOAD, "internalSymbolFull": "DASH", "isBuyEnabled": False}
    assert (
        module.current_catalog_candidates(
            Client([{"items": [payload]}]), clock=lambda: NOW, live_get_cap=1
        )
        == ()
    )


@pytest.mark.parametrize("asset_type", [5, 6])
def test_equity_etf_exchange_closed_remains_closed(monkeypatch, asset_type):
    setup_catalog(monkeypatch, first_type=asset_type)
    assert (
        module.current_catalog_candidates(
            Client([{"items": [PAYLOAD]}]), clock=lambda: NOW, live_get_cap=1
        )
        == ()
    )
