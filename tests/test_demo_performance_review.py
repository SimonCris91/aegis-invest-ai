import json
import sqlite3

import pytest

from app.performance.demo_review import demo_performance_review
from app.storage.sqlite import SqliteRecordStore


def _ledger(tmp_path, records):
    path = tmp_path / "ledger.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE demo_submissions(state TEXT, payload TEXT)")
    connection.executemany("INSERT INTO demo_submissions VALUES (?, ?)", [("FILLED", json.dumps(r)) for r in records])
    connection.commit()
    connection.close()
    return path


def _closed(**changes):
    return {"broker_position_id": "1", "instrument_id": 10, "account_currency": "USD",
            "exit_status": "CLOSED", "exit_trade_history_confirmed": True,
            "exit_confirmation_basis": "DEMO_TRADE_HISTORY_POSITION_ID",
            "exit_realized_pnl_account_currency": "5.89", **changes}


def _snapshot(positions=None):
    return {"status": "LIVE_READ_ONLY", "currency": "USD", "current_pnl": "9999",
            "positions": positions or [], "authorized_capital": "98000"}


def test_closures_use_sale_date_not_insertion_or_confirmation_order(tmp_path):
    records = [
        _closed(broker_position_id="1", exit_broker_closed_at="2026-09-27T12:00:00Z", exit_confirmed_at="2026-10-01T12:00:00Z"),
        _closed(broker_position_id="2", exit_broker_closed_at="2026-09-28T12:00:00+02:00", submitted_at="2026-09-23T08:00:00Z", executed_exposure_account_currency="100"),
        _closed(broker_position_id="3", exit_broker_closed_at="2026-09-28T11:00:00Z"),
        _closed(broker_position_id="4", exit_confirmed_at="invalid"),
    ]
    path = _ledger(tmp_path, records)
    before = path.read_bytes()
    history = demo_performance_review(_snapshot(), path)["realized_history"]
    assert [row["position_id"] for row in history] == ["3", "2", "1", "4"]
    assert history[1]["opened_at"] == "2026-09-23T08:00:00Z"
    assert history[1]["opened_at_basis"] == "SUBMISSION"
    assert history[1]["purchase_amount"] == "100"
    assert history[2]["closed_at"] == "2026-09-27T12:00:00Z"
    assert history[2]["opened_at"] is None
    assert path.read_bytes() == before


def test_history_limit_keeps_latest_closures_even_if_inserted_first(tmp_path):
    from datetime import datetime, timedelta, timezone

    records = [_closed(broker_position_id=str(i), exit_broker_closed_at=(datetime(2026, 9, 30, tzinfo=timezone.utc) - timedelta(days=i)).isoformat()) for i in range(55)]
    history = demo_performance_review(_snapshot(), _ledger(tmp_path, records))["realized_history"]
    assert len(history) == 50
    assert history[0]["position_id"] == "0"
    assert history[-1]["position_id"] == "49"


def test_pnl_is_attributed_by_position_and_currency_not_account_total(tmp_path):
    path = _ledger(tmp_path, [_closed(), {"broker_position_id": "2", "instrument_id": 20, "account_currency": "USD"}])
    snapshot = _snapshot([
        {"position_id": "2", "instrument_id": 20, "unrealized_pnl": "-2.00"},
        {"position_id": "3", "instrument_id": 30, "unrealized_pnl": "100.00"},
    ])
    before = path.read_bytes()
    result = demo_performance_review(snapshot, path)
    assert result["status"] == "AVAILABLE"
    assert result["realized_pnl"] == "5.89"
    assert result["unrealized_pnl"] == "-2.00"
    assert result["combined_pnl"] == "3.89"
    assert result["unattributed_broker_positions"] == 1
    assert result["total_return_pct"] is None
    assert result["maximum_drawdown_pct"] is None
    assert result["broker_write_calls"] == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"account_currency": "EUR"}, {"exit_trade_history_confirmed": False},
    {"exit_confirmation_basis": "BROKER_PORTFOLIO_AND_DETAIL_ABSENT"},
    {"exit_realized_pnl_account_currency": None},
    {"exit_realized_pnl_account_currency": "NaN"},
    {"exit_realized_pnl_account_currency": "Infinity"},
    {"broker_position_id": ""},
])
def test_unverified_closed_values_are_not_zero_or_complete(tmp_path, changes):
    result = demo_performance_review(_snapshot(), _ledger(tmp_path, [_closed(**changes)]))
    assert result["status"] == "PARTIAL"
    assert result["realized_pnl"] is None
    assert result["combined_pnl"] is None


def test_duplicate_position_does_not_double_count_profit(tmp_path):
    result = demo_performance_review(_snapshot(), _ledger(tmp_path, [_closed(), _closed()]))
    assert result["realized_pnl"] is None
    assert result["verified_closed_count"] == 0


def test_closed_position_still_present_is_not_confirmed(tmp_path):
    result = demo_performance_review(_snapshot([{"position_id": "1"}]), _ledger(tmp_path, [_closed()]))
    assert result["realized_pnl"] is None


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    assert demo_performance_review(_snapshot(), path)["status"] == "UNAVAILABLE"
    assert not path.exists()


def test_open_position_requires_exact_instrument_and_finite_pnl(tmp_path):
    path = _ledger(tmp_path, [{"broker_position_id": "2", "instrument_id": 20, "account_currency": "USD"}])
    for instrument, pnl in [(21, "3"), (20, None), (20, "NaN")]:
        result = demo_performance_review(_snapshot([{"position_id": "2", "instrument_id": instrument, "unrealized_pnl": pnl}]), path)
        assert result["unrealized_pnl"] is None
        assert result["combined_pnl"] is None


def test_manually_controlled_position_is_excluded_from_aegis_statistics(tmp_path):
    path = _ledger(tmp_path, [{"broker_position_id": "2", "instrument_id": 20, "account_currency": "USD"}])

    result = demo_performance_review(
        _snapshot([{"position_id": "2", "instrument_id": 20, "unrealized_pnl": "15.00"}]),
        path,
        excluded_position_ids={"2"},
    )

    assert result["status"] == "AVAILABLE"
    assert result["manual_excluded_position_count"] == 1
    assert result["open_ledger_count"] == 0
    assert result["unattributed_broker_positions"] == 0
    assert result["unrealized_pnl"] == "0.00"


def test_operational_history_uses_index_without_losing_records(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = SqliteRecordStore(path)
    store.append("other", {"sequence": 1})
    store.append("status", {"sequence": 2})
    store.append("status", {"sequence": 3})
    connection = sqlite3.connect(path)
    plan = connection.execute("EXPLAIN QUERY PLAN SELECT payload FROM operational_records WHERE kind=? ORDER BY id DESC LIMIT 1", ("status",)).fetchall()
    assert "idx_operational_records_kind_id" in str(plan)
    assert connection.execute("SELECT count(*) FROM operational_records").fetchone()[0] == 3
    connection.close()
    assert SqliteRecordStore.read_latest_read_only(path, "status") == {"sequence": 3}


def test_verified_closures_report_preliminary_strategy_metrics(tmp_path):
    records = [
        _closed(broker_position_id="1", exit_realized_pnl_account_currency="10.00"),
        _closed(broker_position_id="2", exit_realized_pnl_account_currency="-4.00"),
        _closed(broker_position_id="3", exit_realized_pnl_account_currency="0.00"),
    ]
    result = demo_performance_review(_snapshot(), _ledger(tmp_path, records))
    assert result["realized_trade_count"] == 3
    assert result["realized_win_count"] == 1
    assert result["realized_loss_count"] == 1
    assert result["realized_flat_count"] == 1
    assert result["realized_win_rate"] == "33.33"
    assert result["realized_profit_factor"] == "2.50"
    assert result["evaluation"] == "POSITIVE_BUT_PRELIMINARY"


def test_history_preserves_asset_class_and_instrument_id(tmp_path):
    record = _closed(symbol="ZEN", instrument_id=100521, asset_class="CRYPTO")
    result = demo_performance_review(_snapshot(), _ledger(tmp_path, [record]))
    assert result["realized_history"][0]["symbol"] == "ZEN"
    assert result["realized_history"][0]["instrument_id"] == 100521
    assert result["realized_history"][0]["asset_class"] == "CRYPTO"


def test_closure_audit_separates_certified_and_contested_pnl(tmp_path):
    records = [
        _closed(broker_position_id="1", exit_realized_pnl_account_currency="10.00"),
        _closed(
            broker_position_id="2",
            exit_realized_pnl_account_currency="-4.00",
            exit_trade_history_confirmed=False,
        ),
    ]
    result = demo_performance_review(_snapshot(), _ledger(tmp_path, records))
    assert result["certified_closed_count"] == 1
    assert result["contested_closed_count"] == 1
    assert result["certified_realized_pnl"] == "10.00"
    assert result["adjusted_realized_pnl"] == "10.00"
    assert result["contested_pnl_known"] == "-4.00"
    assert result["closure_audit_status"] == "PARTIAL_CERTIFICATION"
    assert result["contested_history"][0]["audit_status"] == "CONTESTED"
