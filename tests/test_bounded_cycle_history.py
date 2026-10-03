"""Runtime comparisons must not decode the entire multi-GB audit history."""

import inspect
from pathlib import Path

import pytest

from app.data.runtime import build_readonly_active_scan_cycle_report
from app.orchestration.active_intelligence import (
    ActiveIntelligenceAuditStore,
    AegisActiveIntelligenceOrchestrator,
    _universe_expanded_since_last_cycle,
)
from app.orchestration.active_runtime import build_active_intelligence_orchestrator_report
from app.storage.sqlite import SqliteRecordStore


def test_latest_cycle_is_bounded_and_keeps_all_audit_rows(tmp_path: Path, monkeypatch) -> None:
    store = SqliteRecordStore(tmp_path / "cycles.sqlite3")
    audit = ActiveIntelligenceAuditStore(store)
    assert audit.latest_cycle() is None
    for index in range(50):
        store.append("active-intelligence-cycle", {"symbols_evaluated": [str(index)]})
    store.append("unrelated-record", {"symbols_evaluated": ["WRONG"]})

    def no_bulk_read(*args, **kwargs):
        raise AssertionError("runtime must not read all historical cycles")

    monkeypatch.setattr(store, "list", no_bulk_read)
    assert audit.latest_cycle() == {"symbols_evaluated": ["49"]}
    assert store._connection.execute(
        "SELECT COUNT(*) FROM operational_records"
    ).fetchone()[0] == 51
    plan = store._connection.execute(
        "EXPLAIN QUERY PLAN SELECT payload FROM operational_records "
        "WHERE kind=? ORDER BY id DESC LIMIT 1", ("active-intelligence-cycle",)
    ).fetchall()
    assert "idx_operational_records_kind_id" in str(plan)


@pytest.mark.parametrize("last,current,expanded", [
    (None, {"BTC"}, False),
    ({"symbols_evaluated": ["BTC"]}, {"BTC", "ETH"}, True),
    ({"symbols_evaluated": ["BTC", "ETH"]}, {"BTC", "ETH"}, False),
    ({"symbols_evaluated": ["BTC", "ETH"]}, {"BTC"}, False),
    ({"symbols_evaluated": ["BTC"]}, {"ETH"}, False),
    ({"symbols_evaluated": []}, {"BTC"}, False),
    ({"symbols_evaluated": None}, {"BTC"}, False),
])
def test_last_cycle_preserves_strict_expansion_semantics(last, current, expanded) -> None:
    assert _universe_expanded_since_last_cycle(last, current_symbols=current) is expanded


def test_runtime_and_readonly_reports_do_not_load_all_cycles() -> None:
    for function in (
        AegisActiveIntelligenceOrchestrator.run_if_new_bar_cycle,
        build_readonly_active_scan_cycle_report,
        build_active_intelligence_orchestrator_report,
    ):
        source = inspect.getsource(function)
        assert ".cycles()" not in source
        assert ".latest_cycle()" in source
