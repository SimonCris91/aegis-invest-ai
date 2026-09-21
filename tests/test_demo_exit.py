from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.demo_exit import (
    DEMO_CLOSE_URL_PREFIX,
    manage_demo_exits,
    evaluate_demo_exit,
)
from app.brokers.etoro.http import HttpResponse
from app.brokers.models import BrokerIdentity
from app.storage.sqlite import SqliteRecordStore
from pydantic import SecretStr


class FakeHttp:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post_once(self, url, headers, payload):
        self.calls.append((url, payload))
        return self.response


class FakeClient:
    def __init__(self, pnl: Decimal) -> None:
        self.raw = {
            "clientPortfolio": {
                "positions": [
                    {"positionID": 77, "instrumentID": 100, "isBuy": True, "mirrorID": 0}
                ]
            }
        }
        self.snapshot = SimpleNamespace(
            positions=(
                SimpleNamespace(
                    instrument_id=100,
                    current_exposure=Decimal("100"),
                    initial_exposure=Decimal("100"),
                    unrealized_pnl_account_currency=pnl,
                ),
            )
        )

    def demo_portfolio_payload(self):
        return self.raw

    def demo_account(self, identity):
        return self.snapshot


def _store(tmp_path: Path) -> SqliteRecordStore:
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    assert store.reserve_demo_submission(
        "etoro-demo-pilot:test:BTC:OPEN",
        {"instrument_id": 100, "symbol": "BTC", "amount_eur": "100", "action": "OPEN"},
    )
    store.update_demo_submission(
        "etoro-demo-pilot:test:BTC:OPEN", "FILLED", {"broker_order_id": "1"}
    )
    return store


def _credentials() -> EtoroCredentials:
    return EtoroCredentials(api_key=SecretStr("api"), user_key=SecretStr("user"))


def test_exit_policy_holds_current_small_gain_and_closes_protection_levels() -> None:
    assert evaluate_demo_exit(
        pnl_pct=Decimal("0.0048"), peak_pnl_pct=Decimal("0.0048")
    ).action == "HOLD"
    assert evaluate_demo_exit(
        pnl_pct=Decimal("-0.16"), peak_pnl_pct=Decimal("0")
    ).reason == "EXITPOLICY_V2_CAPITAL_PROTECTION_CLOSE"
    assert evaluate_demo_exit(
        pnl_pct=Decimal("0.01"), peak_pnl_pct=Decimal("0.12")
    ).reason == "EXITPOLICY_V2_TRAILING_PROFIT_CLOSE"


def test_demo_exit_posts_verified_close_once_and_keeps_lifecycle_reserved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    http = FakeHttp(HttpResponse(200, {}, b"{}"))
    result = manage_demo_exits(
        client=FakeClient(Decimal("-16")),
        identity=BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )
    assert result["close_write_calls"] == 1
    assert http.calls[0][0] == f"{DEMO_CLOSE_URL_PREFIX}77"
    assert http.calls[0][1] == {"UnitsToDeduct": None}
    assert store.demo_submission("etoro-demo-pilot:test:BTC:OPEN")["payload"]["exit_status"] == (
        "SUBMITTED"
    )
    # A second poll must wait for read-back instead of replaying the close.
    second = manage_demo_exits(
        client=FakeClient(Decimal("-16")),
        identity=BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )
    assert second["close_write_calls"] == 0
    assert len(http.calls) == 1
    store.close()

