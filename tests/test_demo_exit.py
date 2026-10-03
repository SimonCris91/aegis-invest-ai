from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroApiError
from app.brokers.etoro.demo_exit import (
    DEMO_CLOSE_URL_PREFIX,
    evaluate_demo_exit,
    manage_demo_exits,
)
from app.brokers.etoro.http import HttpResponse
from app.brokers.models import BrokerIdentity, ExecutionState
from app.storage.sqlite import SqliteRecordStore


class FakeHttp:
    def __init__(self, response: HttpResponse | list[HttpResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post_once(self, url, headers, payload):
        self.calls.append((url, payload))
        return self.responses.pop(0)


class FakeClient:
    def __init__(
        self,
        pnl: Decimal,
        *,
        position_rows: list[dict[str, object]] | None = None,
        aggregate_pnl: Decimal | None = None,
        lookup_state: ExecutionState = ExecutionState.UNKNOWN,
    ) -> None:
        self.lookup_state = lookup_state
        self.lookup_calls: list[tuple[str, str | None]] = []
        self.raw = {
            "clientPortfolio": {
                "positions": position_rows
                or [
                    {
                        "positionID": 77,
                        "instrumentID": 100,
                        "isBuy": True,
                        "mirrorID": 0,
                        "amount": "100",
                        "unrealizedPnL": {
                            "pnL": str(pnl),
                            "marginInAccountCurrency": "100",
                        },
                    }
                ]
            }
        }
        self.portfolio_raw = self.raw
        self.snapshot = SimpleNamespace(
            positions=(
                SimpleNamespace(
                    instrument_id=100,
                    current_exposure=Decimal("100"),
                    initial_exposure=Decimal("100"),
                    unrealized_pnl_account_currency=(
                        aggregate_pnl if aggregate_pnl is not None else pnl
                    ),
                ),
            )
        )

    def demo_pnl_payload(self):
        return self.raw

    def demo_portfolio_payload(self):
        return self.portfolio_raw

    def demo_account(self, identity):
        return self.snapshot

    def demo_order_lookup(self, order_id, *, reference_id=None):
        self.lookup_calls.append((order_id, reference_id))
        return self.lookup_state

    def demo_close_order_position_affected(self, order_id, position_id):
        self.lookup_calls.append((order_id, None))
        return self.lookup_state is ExecutionState.FILLED


def _store(tmp_path: Path) -> SqliteRecordStore:
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    assert store.reserve_demo_submission(
        "etoro-demo-pilot:test:BTC:OPEN",
        {
            "instrument_id": 100,
            "symbol": "BTC",
            "amount_eur": "100",
            "action": "OPEN",
            "account_currency": "EUR",
            "broker_position_id": "77",
        },
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
    assert http.calls[0][1] == {"InstrumentId": 100, "UnitsToDeduct": None}
    saved = store.demo_submission("etoro-demo-pilot:test:BTC:OPEN")["payload"]
    assert saved["exit_status"] == "SUBMITTED"
    assert saved["last_exit_pnl_pct"] == "-0.16"
    assert saved["last_exit_peak_pnl_pct"] == "-0.16"
    assert saved["exit_trigger_pnl_pct"] == "-0.16"
    assert saved["exit_trigger_position_pnl"] == "-16"
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


def test_demo_exit_reads_nested_broker_close_order_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    http = FakeHttp(HttpResponse(
        200, {}, b'{"orderForClose":{"positionID":77,"orderID":12349}}'
    ))
    result = manage_demo_exits(
        client=FakeClient(Decimal("-16")),
        identity=BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )
    saved = store.demo_submission("etoro-demo-pilot:test:BTC:OPEN")["payload"]
    assert result["close_write_calls"] == 1
    assert saved["exit_status"] == "SUBMITTED"
    assert saved["exit_order_id"] == "12349"
    assert http.calls == [
        (f"{DEMO_CLOSE_URL_PREFIX}77", {"InstrumentId": 100, "UnitsToDeduct": None})
    ]
    store.close()


def test_only_legacy_http_400_close_gets_one_corrected_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key,
        "FILLED",
        {
            "exit_status": "REJECTED",
            "exit_http_status": 400,
            "exit_reason": "EXITPOLICY_V2_CAPITAL_PROTECTION_CLOSE",
        },
    )
    http = FakeHttp(HttpResponse(200, {}, b'{"orderId":12345}'))

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

    saved = store.demo_submission(key)["payload"]
    assert result["close_retry_calls"] == 1
    assert result["close_write_calls"] == 1
    assert http.calls == [(f"{DEMO_CLOSE_URL_PREFIX}77", {"InstrumentId": 100, "UnitsToDeduct": None})]
    assert saved["exit_status"] == "SUBMITTED"
    assert saved["exit_retry_count"] == 1
    assert saved["exit_order_id"] == "12345"
    assert saved["exit_request_payload_mode"] == "INSTRUMENT_ID_UNITS_NULL"
    assert saved["exit_instrument_id_retry_attempted"] is True

    # Once the corrected request has been accepted, reconciliation is read-
    # only and a later poll must never send another close for the same position.
    client = FakeClient(Decimal("-16"), lookup_state=ExecutionState.FILLED)
    second = manage_demo_exits(
        client=client,
        identity=BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    saved = store.demo_submission(key)["payload"]
    assert second["close_write_calls"] == 0
    assert len(http.calls) == 1
    assert client.lookup_calls == [("12345", None)]
    assert saved["exit_order_state"] == "FILLED"
    assert saved["exit_status"] == "SUBMITTED"
    store.close()


def test_previous_omitted_units_close_gets_one_null_body_correction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key,
        "FILLED",
        {
            "exit_status": "REJECTED",
            "exit_http_status": 400,
            "exit_request_payload_mode": "OMIT_UNITS_TO_DEDUCT",
            "exit_retry_count": 1,
        },
    )
    http = FakeHttp(HttpResponse(200, {}, b'{"orderId":12346}'))

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

    saved = store.demo_submission(key)["payload"]
    assert result["close_retry_calls"] == 1
    assert http.calls == [(f"{DEMO_CLOSE_URL_PREFIX}77", {"InstrumentId": 100, "UnitsToDeduct": None})]
    assert saved["exit_retry_count"] == 2
    assert saved["exit_instrument_id_retry_attempted"] is True
    assert saved["exit_request_payload_mode"] == "INSTRUMENT_ID_UNITS_NULL"
    store.close()


def test_new_close_body_http_400_is_terminal_with_diagnostics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    http = FakeHttp(HttpResponse(400, {}, b'{"code":"BadRequest","message":"invalid close field"}'))
    kwargs = {
        "client": FakeClient(Decimal("-16")),
        "identity": BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        "credentials": _credentials(),
        "http": http,
        "registry": store,
        "observed_at": datetime.now(UTC),
    }

    first = manage_demo_exits(**kwargs)
    second = manage_demo_exits(
        **{**kwargs, "observed_at": datetime.now(UTC) + timedelta(minutes=2)}
    )
    third = manage_demo_exits(
        **{**kwargs, "observed_at": datetime.now(UTC) + timedelta(minutes=4)}
    )

    saved = store.demo_submission("etoro-demo-pilot:test:BTC:OPEN")["payload"]
    assert first["close_write_calls"] == 1
    assert first["blocked"] == 1
    assert first["pending_confirmation"] == 0
    assert first["response_diagnostics"] == [
        {
            "symbol": "BTC",
            "http_status": 400,
            "exit_broker_error_code": "BadRequest",
            "exit_broker_error_message": "invalid close field",
        }
    ]
    assert second["close_retry_calls"] == 0
    assert second["blocked"] == 1
    assert http.calls == [
        (f"{DEMO_CLOSE_URL_PREFIX}77", {"InstrumentId": 100, "UnitsToDeduct": None}),
    ]
    assert third["close_write_calls"] == 0
    assert saved["exit_status"] == "REJECTED"
    assert saved["exit_retry_count"] == 0
    assert saved["exit_request_payload_mode"] == "INSTRUMENT_ID_UNITS_NULL"
    store.close()


def test_historical_400_gets_only_one_instrument_id_correction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key,
        "FILLED",
        {
            "exit_status": "REJECTED",
            "exit_http_status": 400,
            "exit_request_payload_mode": "UNITS_TO_DEDUCT_OMITTED",
            "exit_retry_count": 3,
        },
    )
    rejection = HttpResponse(400, {}, b'{"code":"BadRequest"}')
    http = FakeHttp(rejection)
    kwargs = {
        "client": FakeClient(Decimal("-16")),
        "identity": BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        "credentials": _credentials(),
        "http": http,
        "registry": store,
        "observed_at": datetime.now(UTC),
    }

    first = manage_demo_exits(**kwargs)
    second = manage_demo_exits(
        **{**kwargs, "observed_at": datetime.now(UTC) + timedelta(minutes=2)}
    )
    third = manage_demo_exits(
        **{**kwargs, "observed_at": datetime.now(UTC) + timedelta(minutes=4)}
    )

    saved = store.demo_submission(key)["payload"]
    assert first["close_retry_calls"] == 1
    assert second["blocked"] == 1
    assert third["close_write_calls"] == 0
    assert len(http.calls) == 1
    assert http.calls[0] == (
        f"{DEMO_CLOSE_URL_PREFIX}77", {"InstrumentId": 100, "UnitsToDeduct": None}
    )
    assert saved["exit_status"] == "REJECTED"
    assert saved["exit_instrument_id_retry_attempted"] is True
    assert saved["exit_retry_count"] == 4
    store.close()


def test_terminal_close_rejection_still_refreshes_live_hold_decision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key,
        "FILLED",
        {
            "exit_status": "REJECTED",
            "exit_http_status": 400,
            "exit_request_payload_mode": "INSTRUMENT_ID_UNITS_NULL",
            "exit_instrument_id_retry_attempted": True,
            "exit_omitted_units_retry_attempted": True,
            "exit_retry_count": 3,
            "peak_pnl_pct": "0.17",
            "last_exit_decision": "CLOSE",
        },
    )
    http = FakeHttp([])
    result = manage_demo_exits(
        client=FakeClient(Decimal("8")),
        identity=BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )
    saved = store.demo_submission(key)["payload"]
    assert result["held"] == 1
    assert result["blocked"] == 0
    assert result["close_write_calls"] == 0
    assert http.calls == []
    assert saved["last_exit_decision"] == "HOLD"
    assert saved["exit_status"] == "REJECTED"
    store.close()


def test_three_old_rejections_may_use_one_instrument_id_correction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key,
        "FILLED",
        {
            "exit_status": "REJECTED",
            "exit_http_status": 400,
            "exit_request_payload_mode": "OMIT_UNITS_TO_DEDUCT",
            "exit_retry_count": 3,
            "exit_reason": "EXITPOLICY_V2_CAPITAL_PROTECTION_CLOSE",
        },
    )
    accepted = HttpResponse(200, {}, b'{"orderId":12348}')
    http = FakeHttp(accepted)
    kwargs = {
        "client": FakeClient(Decimal("-16")),
        "identity": BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        "credentials": _credentials(),
        "http": http,
        "registry": store,
        "observed_at": datetime.now(UTC),
    }

    first = manage_demo_exits(**kwargs)
    second = manage_demo_exits(
        **{**kwargs, "observed_at": datetime.now(UTC) + timedelta(minutes=2)}
    )
    third = manage_demo_exits(
        **{**kwargs, "observed_at": datetime.now(UTC) + timedelta(minutes=4)}
    )

    saved = store.demo_submission(key)["payload"]
    assert first["close_retry_calls"] == 1
    assert second["close_retry_calls"] == 0
    assert second["pending_confirmation"] == 1
    assert third["close_write_calls"] == 0
    assert http.calls == [
        (f"{DEMO_CLOSE_URL_PREFIX}77", {"InstrumentId": 100, "UnitsToDeduct": None}),
    ]
    assert saved["exit_status"] == "SUBMITTED"
    assert saved["exit_retry_count"] == 4
    assert saved["exit_instrument_id_retry_attempted"] is True
    assert saved["exit_request_payload_mode"] == "INSTRUMENT_ID_UNITS_NULL"
    store.close()


def test_ambiguous_server_error_is_read_reconciled_never_reposted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    http = FakeHttp(HttpResponse(503, {}, b""))
    identity = BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2)
    first = manage_demo_exits(
        client=FakeClient(Decimal("-16")),
        identity=identity,
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )
    client = FakeClient(Decimal("-16"), lookup_state=ExecutionState.PENDING)
    second = manage_demo_exits(
        client=client,
        identity=identity,
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    saved = store.demo_submission("etoro-demo-pilot:test:BTC:OPEN")["payload"]
    assert first["pending_confirmation"] == 1
    assert saved["exit_status"] == "UNKNOWN"
    assert second["close_write_calls"] == 0
    assert second["pending_confirmation"] == 1
    assert client.lookup_calls == [("", saved["exit_request_id"])]
    assert len(http.calls) == 1
    store.close()


def test_ambiguous_close_is_marked_rejected_only_after_broker_lookup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    http = FakeHttp(HttpResponse(504, {}, b""))
    identity = BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2)
    manage_demo_exits(
        client=FakeClient(Decimal("-16")),
        identity=identity,
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )
    client = FakeClient(Decimal("-16"), lookup_state=ExecutionState.REJECTED)
    result = manage_demo_exits(
        client=client,
        identity=identity,
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    saved = store.demo_submission("etoro-demo-pilot:test:BTC:OPEN")["payload"]
    assert saved["exit_status"] == "REJECTED"
    assert saved["exit_broker_terminal_status"] == "REJECTED"
    assert result["blocked"] == 1
    assert result["pending_confirmation"] == 0
    assert result["close_write_calls"] == 0
    assert len(http.calls) == 1
    store.close()


def test_demo_exit_uses_per_position_pnl_for_duplicate_instruments(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rows = [
        {
            "positionID": 77,
            "instrumentID": 100,
            "isBuy": True,
            "mirrorID": 0,
            "amount": "100",
            "unrealizedPnL": {"pnL": "-5", "marginInAccountCurrency": "100"},
        },
        {
            "positionID": 78,
            "instrumentID": 100,
            "isBuy": True,
            "mirrorID": 0,
            "amount": "100",
            "unrealizedPnL": {"pnL": "-40", "marginInAccountCurrency": "100"},
        },
    ]
    http = FakeHttp(HttpResponse(200, {}, b"{}"))

    result = manage_demo_exits(
        client=FakeClient(Decimal("-45"), position_rows=rows, aggregate_pnl=Decimal("-45")),
        identity=BrokerIdentity(
            stable_user_id="user", demo_account_id=1, real_account_id=2
        ),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )

    assert result["held"] == 1
    assert result["blocked"] == 0
    assert result["close_write_calls"] == 0
    assert http.calls == []
    saved = store.demo_submission("etoro-demo-pilot:test:BTC:OPEN")["payload"]
    assert saved["last_exit_decision"] == "HOLD"
    assert saved["last_exit_reason"] == "EXITPOLICY_V2_NO_EXIT_TRIGGER_HOLD"
    store.close()


def test_absent_position_is_counted_once_without_claiming_order_confirmation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key, "FILLED", {"exit_status": "SUBMITTED", "exit_order_id": "12345"}
    )
    client = FakeClient(Decimal("0"))
    client.raw["clientPortfolio"]["positions"] = []
    client.snapshot.positions = ()
    http = FakeHttp([])
    identity = BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2)
    first_at = datetime.now(UTC)

    first = manage_demo_exits(
        client=client, identity=identity, credentials=_credentials(),
        http=http, registry=store, observed_at=first_at,
    )
    initial_payload = store.demo_submission(key)["payload"]
    second = manage_demo_exits(
        client=client, identity=identity, credentials=_credentials(),
        http=http, registry=store, observed_at=first_at + timedelta(minutes=2),
    )
    saved = store.demo_submission(key)["payload"]

    assert first["evaluated"] == 1
    assert first["closed_confirmed"] == 1
    assert first["closed_total"] == 1
    assert second["evaluated"] == 0
    assert second["closed_confirmed"] == 0
    assert second["closed_total"] == 1
    assert first["close_write_calls"] == second["close_write_calls"] == 0
    assert http.calls == []
    assert saved["exit_status"] == "CLOSED"
    assert saved["exit_confirmation_basis"] == "BROKER_PORTFOLIO_AND_DETAIL_ABSENT"
    assert saved["exit_order_confirmed"] is False
    assert saved["exit_confirmed_at"] == initial_payload["exit_confirmed_at"]
    store.close()


def test_close_lookup_404_stays_pending_without_reposting_until_position_absent(
    tmp_path: Path,
) -> None:
    class Lookup404Client(FakeClient):
        def demo_close_order_position_affected(self, order_id, position_id):
            self.lookup_calls.append((order_id, None))
            raise EtoroApiError("not found", endpoint="/demo/orders:lookup", status=404)

    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key, "FILLED", {"exit_status": "SUBMITTED", "exit_order_id": "12345"}
    )
    client = Lookup404Client(Decimal("-16"))
    http = FakeHttp([])
    identity = BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2)
    first_at = datetime.now(UTC)

    first = manage_demo_exits(
        client=client, identity=identity, credentials=_credentials(),
        http=http, registry=store, observed_at=first_at,
    )
    second = manage_demo_exits(
        client=client, identity=identity, credentials=_credentials(),
        http=http, registry=store, observed_at=first_at + timedelta(seconds=30),
    )
    pending = store.demo_submission(key)["payload"]
    assert first["pending_confirmation"] == second["pending_confirmation"] == 1
    assert first["close_write_calls"] == second["close_write_calls"] == 0
    assert pending["exit_status"] == "SUBMITTED"
    assert pending["exit_order_lookup_error"] == "HTTP_404"
    assert client.lookup_calls == [("12345", None)]
    assert http.calls == []

    client.raw["clientPortfolio"]["positions"] = []
    client.snapshot.positions = ()
    confirmed = manage_demo_exits(
        client=client, identity=identity, credentials=_credentials(),
        http=http, registry=store, observed_at=first_at + timedelta(minutes=2),
    )
    saved = store.demo_submission(key)["payload"]
    assert confirmed["closed_confirmed"] == 1
    assert confirmed["pending_confirmation"] == 0
    assert confirmed["close_write_calls"] == 0
    assert saved["exit_status"] == "CLOSED"
    assert saved["exit_order_confirmed"] is False
    assert saved["exit_order_lookup_error"] == "HTTP_404"
    store.close()


def test_absent_position_records_matching_close_order_and_realized_history(
    tmp_path: Path,
) -> None:
    class HistoryClient(FakeClient):
        def demo_closed_trade_by_position(self, position_id, *, min_date, instrument_id):
            assert position_id == "77"
            assert instrument_id == 100
            return {
                "position_id": "77",
                "realized_pnl_account_currency": "12.50",
                "closed_at": "2026-09-25T05:17:25+00:00",
            }

    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key, "FILLED", {"exit_status": "SUBMITTED", "exit_order_id": "12345"}
    )
    client = HistoryClient(Decimal("0"), lookup_state=ExecutionState.FILLED)
    client.raw["clientPortfolio"]["positions"] = []
    client.snapshot.positions = ()
    http = FakeHttp([])

    result = manage_demo_exits(
        client=client,
        identity=BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )
    saved = store.demo_submission(key)["payload"]
    assert result["closed_confirmed"] == result["closed_total"] == 1
    assert result["close_write_calls"] == 0
    assert saved["exit_confirmation_basis"] == "DEMO_TRADE_HISTORY_POSITION_ID"
    assert saved["exit_order_confirmed"] is True
    assert saved["exit_trade_history_confirmed"] is True
    assert saved["exit_realized_pnl_account_currency"] == "12.50"
    assert saved["exit_broker_closed_at"] == "2026-09-25T05:17:25+00:00"
    assert http.calls == []
    store.close()


def test_closed_position_reconciles_when_same_instrument_has_other_open_positions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key,
        "FILLED",
        {
            "exit_status": "SUBMITTED",
            "exit_order_id": "12345",
        },
    )
    other_position = {
        "positionID": 88,
        "instrumentID": 100,
        "isBuy": True,
        "mirrorID": 0,
        "amount": "75",
        "unrealizedPnL": {
            "pnL": "3",
            "marginInAccountCurrency": "75",
        },
    }
    client = FakeClient(
        Decimal("3"),
        position_rows=[other_position],
        lookup_state=ExecutionState.FILLED,
    )
    http = FakeHttp([])

    result = manage_demo_exits(
        client=client,
        identity=BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )

    saved = store.demo_submission(key)["payload"]
    assert result["closed_confirmed"] == 1
    assert result["close_write_calls"] == 0
    assert result["pending_confirmation"] == 0
    assert client.lookup_calls == [("12345", None)]
    assert saved["exit_status"] == "CLOSED"
    assert saved["exit_order_confirmed"] is True
    assert saved["remaining_exposure_account_currency"] == "0"
    assert http.calls == []
    store.close()


def test_missing_pnl_detail_does_not_close_exact_position_still_in_portfolio(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    key = "etoro-demo-pilot:test:BTC:OPEN"
    store.update_demo_submission(
        key,
        "FILLED",
        {
            "exit_status": "SUBMITTED",
            "exit_order_id": "12345",
        },
    )
    client = FakeClient(Decimal("0"))
    client.raw["clientPortfolio"]["positions"] = []
    client.portfolio_raw = {
        "clientPortfolio": {
            "positions": [
                {
                    "positionID": 77,
                    "instrumentID": 100,
                    "isBuy": True,
                    "mirrorID": 0,
                    "amount": "100",
                }
            ]
        }
    }
    http = FakeHttp([])

    result = manage_demo_exits(
        client=client,
        identity=BrokerIdentity(stable_user_id="user", demo_account_id=1, real_account_id=2),
        credentials=_credentials(),
        http=http,
        registry=store,
        observed_at=datetime.now(UTC),
    )

    saved = store.demo_submission(key)["payload"]
    assert result["closed_confirmed"] == 0
    assert result["pending_confirmation"] == 0
    assert result["blocked"] == 1
    assert result["close_write_calls"] == 0
    assert saved["exit_status"] == "SUBMITTED"
    assert "remaining_exposure_account_currency" not in saved
    assert http.calls == []
    store.close()
