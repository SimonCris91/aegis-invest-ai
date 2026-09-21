import ast
import inspect

import pytest

from app.brokers.etoro import demo_execution
from app.brokers.etoro.demo_preflight import _preflight_market_status
from app.brokers.etoro.mapping import map_instrument_resolution
from app.brokers.etoro.tradability_revalidation import fresh_demo_tradability
from app.domain.enums import MarketStatus
from app.orchestration.active_runtime import _fresh_demo_tradability
from tests.test_crypto_live_tradability import PAYLOAD
from tests.test_step75 import _now


class Client:
    def __init__(self, resolution):
        self.resolution = resolution
        self.calls = []

    def resolve_instrument_id(self, instrument_id, *, symbol, as_of):
        self.calls.append((instrument_id, symbol, as_of))
        return self.resolution


@pytest.mark.parametrize("symbol,buy,expected", [("BTC", True, True), ("DASH", False, False)])
@pytest.mark.parametrize("validate", [fresh_demo_tradability, _fresh_demo_tradability])
def test_crypto_preflight_and_immediate_revalidation_agree(symbol, buy, expected, validate):
    raw = {
        **PAYLOAD,
        "internalSymbolFull": symbol,
        "internalAssetClassName": "Crypto",
        "isBuyEnabled": buy,
    }
    resolution = map_instrument_resolution({"items": [raw]}, symbol=symbol, as_of=_now())
    client = Client(resolution)
    assert (_preflight_market_status(resolution) is MarketStatus.OPEN) is expected
    assert validate(client, resolution.instrument_id, symbol, _now()) is expected
    assert client.calls == [(resolution.instrument_id, symbol, _now())]


@pytest.mark.parametrize("kind", ["Stock", "ETF"])
@pytest.mark.parametrize("opened", [True, False])
def test_equity_etf_session_requirement_unchanged(kind, opened):
    raw = {**PAYLOAD, "instrumentType": kind, "isExchangeOpen": opened, "isOpen": opened}
    resolution = map_instrument_resolution({"items": [raw]}, symbol="BTC", as_of=_now())
    assert (
        fresh_demo_tradability(Client(resolution), resolution.instrument_id, "BTC", _now())
        is opened
    )


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
def test_missing_crypto_field_fails_closed(field):
    resolution = map_instrument_resolution(
        {"items": [{**PAYLOAD, "internalAssetClassName": "Crypto"}]}, symbol="BTC", as_of=_now()
    ).model_copy(update={field: None})
    assert not fresh_demo_tradability(Client(resolution), resolution.instrument_id, "BTC", _now())


def test_one_shot_adapter_wires_shared_revalidator():
    tree = ast.parse(inspect.getsource(demo_execution.arm_and_submit_confirmed_demo_once))
    adapters = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "EtoroDemoAdapter"
    ]
    assert len(adapters) == 1
    callback = next(k.value for k in adapters[0].keywords if k.arg == "tradability_revalidator")
    assert isinstance(callback, ast.Lambda)
    calls = []
    client = object()
    function = eval(
        compile(ast.Expression(callback), "callback", "eval"),
        {
            "read_client": client,
            "fresh_demo_tradability": lambda *args: calls.append(args) or False,
        },
    )
    assert function(1, "BTC", _now()) is False
    assert calls == [(client, 1, "BTC", _now())]
