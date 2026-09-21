import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace as NS

import pytest

from app.brokers.etoro.http import HttpResponse
from app.domain.enums import Currency
from app.web.manual_demo import ManualDemoOrders, ManualOrderError
from app.web.server import AegisHomeHandler


@pytest.fixture
def rig(tmp_path):
    now = datetime.now(UTC)
    identity = NS(
        stable_user_id="test",
        username="owner",
        demo_account_id=12,
        scopes=("etoro-public:trade.demo:write",),
    )
    resolution = NS(
        resolved=True,
        instrument_id=42,
        internal_symbol_full="TEST",
        is_currently_tradable=True,
        is_active_in_platform=True,
        is_delisted=False,
        is_hidden_from_client=False,
        is_buy_enabled=True,
    )
    eligibility = NS(
        verified=True,
        currency=Currency.USD,
        allow_open=True,
        allow_close=True,
        minimum_position=Decimal("10"),
        max_units_per_order=None,
    )
    quote = NS(as_of=now, ask=Decimal("100"), bid=Decimal("99"), price=Decimal("100"))
    portfolio = {
        "clientPortfolio": {
            "credit": 1000,
            "ordersForOpen": [],
            "orders": [],
            "positions": [
                {
                    "positionID": 7,
                    "instrumentID": 42,
                    "isBuy": True,
                    "units": 5,
                    "openRate": 100,
                    "mirrorID": 0,
                }
            ],
        }
    }
    client = NS(
        identity=lambda: identity,
        resolve_instrument_id=lambda *a, **k: resolution,
        demo_eligibility=lambda *a, **k: eligibility,
        quote_with_diagnostics=lambda *a: (quote, (), now),
        demo_portfolio_payload=lambda: portfolio,
        demo_order_state=lambda *a: NS(value="FILLED"),
    )
    calls = []

    def post(url, headers, payload):
        calls.append((url, payload))
        return HttpResponse(200, {}, b'{"orderId": 123,"token":"not-for-browser"}')

    http = NS(post_once=post)
    creds = NS(headers=lambda: {"x-request-id": "test"})
    settings = NS(
        expected_username="owner",
        expected_gcid="test",
        maximum_quote_age_seconds=300,
        maximum_future_quote_skew_seconds=5,
    )
    service = ManualDemoOrders(
        tmp_path / "orders.db", factory=lambda: (client, http, creds, settings), clock=lambda: now
    )
    ticket = dict(
        mode="MANUAL", instrument_id="42", symbol="TEST", side="BUY", amount="200", position_id=""
    )
    return NS(
        service=service,
        ticket=ticket,
        calls=calls,
        http=http,
        quote=quote,
        identity=identity,
        eligibility=eligibility,
        portfolio=portfolio,
        now=now,
    )


def test_readonly_credentials_never_submit(rig):
    rig.identity.scopes = ("etoro-public:trade.demo:read",)
    with pytest.raises(ManualOrderError, match="Permissions Write"):
        rig.service.preview(rig.ticket)
    assert not rig.calls


def test_selected_order_and_replay_after_restart(rig):
    p = rig.service.preview(rig.ticket)
    assert not rig.calls
    result = rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True))
    assert result["status"] == "FILLED"
    assert rig.calls[0][1]["instrumentId"] == 42
    assert rig.calls[0][1]["amount"] == 100
    assert rig.service.status({"preview_id": p["preview_id"]})["status"] == "FILLED"
    assert "/demo/" in rig.calls[0][0]
    assert "token" not in json.dumps(result)
    again = ManualDemoOrders(rig.service.path, factory=rig.service.factory)
    assert again.submit(dict(preview_id=p["preview_id"], confirmed=True)) == result
    assert len(rig.calls) == 1


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "1.001", "100000001"])
def test_invalid_amounts(rig, amount):
    with pytest.raises(ManualOrderError):
        rig.service.preview({**rig.ticket, "amount": amount})
    assert not rig.calls


def test_revalidates_balance_and_quote_at_confirmation(rig):
    p = rig.service.preview(rig.ticket)
    rig.portfolio["clientPortfolio"]["orders"] = [{"amount": 901}]
    with pytest.raises(ManualOrderError, match="Saldo"):
        rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True))
    rig.portfolio["clientPortfolio"]["orders"] = []
    rig.quote.as_of -= timedelta(hours=1)
    with pytest.raises(ManualOrderError, match="Prezzo"):
        rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True))
    assert not rig.calls


def test_expiry_and_tampering(rig):
    p = rig.service.preview(rig.ticket)
    with pytest.raises(ManualOrderError):
        rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True, amount=2))
    rig.service.clock = lambda: rig.now + timedelta(seconds=121)
    with pytest.raises(ManualOrderError, match="scaduta"):
        rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True))
    assert not rig.calls


def test_ambiguous_write_is_never_retried(rig):
    p = rig.service.preview(rig.ticket)

    def fail(*args):
        rig.calls.append(args)
        raise TimeoutError()

    rig.http.post_once = fail
    for _ in range(2):
        assert (
            rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True))["status"]
            == "UNKNOWN"
        )
    assert len(rig.calls) == 1
    next_preview = rig.service.preview(rig.ticket)
    with pytest.raises(ManualOrderError, match="precedente"):
        rig.service.submit(dict(preview_id=next_preview["preview_id"], confirmed=True))


def test_concurrent_double_click(rig):
    p = rig.service.preview(rig.ticket)
    with ThreadPoolExecutor(2) as executor:
        list(
            executor.map(
                lambda _: rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True)),
                range(2),
            )
        )
    assert len(rig.calls) == 1


def test_sell_owned_position_only(rig):
    ticket = {**rig.ticket, "side": "SELL", "position_id": "7"}
    p = rig.service.preview(ticket)
    result = rig.service.submit(dict(preview_id=p["preview_id"], confirmed=True))
    assert result["status"] == "FILLED"
    assert rig.calls[0][0].endswith("/demo/market-close-orders/positions/7")
    assert rig.calls[0][1]["UnitsToDeduct"] is None
    with pytest.raises(ManualOrderError):
        rig.service.preview({**ticket, "position_id": "8"})


def test_sell_can_close_live_position_after_open_is_still_submitted(rig):
    buy_preview = rig.service.preview(rig.ticket)
    with rig.service._db() as db:
        db.execute(
            "UPDATE manual_orders SET state='SUBMITTED', result=? WHERE id=?",
            (
                json.dumps({"status": "SUBMITTED", "broker_order_id": "999"}),
                buy_preview["preview_id"],
            ),
        )
    sell_ticket = {**rig.ticket, "side": "SELL", "position_id": "7"}
    sell_preview = rig.service.preview(sell_ticket)
    result = rig.service.submit({"preview_id": sell_preview["preview_id"], "confirmed": True})
    assert result["status"] == "FILLED"
    assert rig.calls[-1][0].endswith("/demo/market-close-orders/positions/7")


def test_identity_mismatch_blocks(rig):
    rig.identity.stable_user_id = "another"
    with pytest.raises(ManualOrderError, match="conto autenticato"):
        rig.service.preview(rig.ticket)
    assert not rig.calls


def test_http_auth_methods_and_actual_route(rig, monkeypatch):
    import app.web.server as server

    monkeypatch.setattr(server, "manual_orders", rig.service)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), AegisHomeHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_port

    def request(path, body, headers=None, method="POST"):
        conn = HTTPConnection("127.0.0.1", port)
        conn.request(
            method,
            path,
            json.dumps(body),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        res = conn.getresponse()
        payload = json.loads(res.read())
        conn.close()
        return res.status, payload

    try:
        assert request("/api/etoro/demo/order", {})[0] == 401
        origin = {"Origin": f"http://127.0.0.1:{port}", "X-Aegis-Request": "manual"}
        assert (
            request("/api/manual/session", {}, {**origin, "CF-Connecting-IP": "1.2.3.4"})[0] == 401
        )
        assert (
            request("/api/manual/session", {}, {**origin, "Origin": "https://evil.test"})[0] == 401
        )
        status, auth = request("/api/manual/session", {}, origin)
        assert status == 200
        headers = {"Authorization": "Bearer " + auth["token"]}
        for method in ["PUT", "PATCH", "DELETE"]:
            assert request("/api/etoro/demo/order", {}, headers, method)[0] == 405
        status, p = request("/api/etoro/demo/preview", rig.ticket, headers)
        assert status == 200
        status, result = request(
            "/api/etoro/demo/order", {"preview_id": p["preview_id"], "confirmed": True}, headers
        )
        assert status == 200 and result["status"] == "FILLED"
        assert len(rig.calls) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
