"""Focused causal active-intelligence gate tests."""

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from app.data import runtime as data_runtime
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.orchestration import active_runtime
from app.orchestration.active_intelligence import (
    ActiveIntelligenceAuditStore,
    AegisActiveIntelligenceOrchestrator,
)
from app.storage.sqlite import SqliteRecordStore

BASE = datetime(2026, 8, 28, 10, tzinfo=UTC)


def test_production_active_scan_callers_use_causal_gate_only() -> None:
    active_runtime_source = inspect.getsource(
        active_runtime.build_active_intelligence_orchestrator_report
    )
    readonly_source = inspect.getsource(data_runtime.build_readonly_active_scan_cycle_report)

    assert "run_if_new_bar_cycle" in active_runtime_source
    assert ".run_cycle(" not in active_runtime_source
    assert "run_if_new_bar_cycle" in readonly_source
    assert ".scan(" not in readonly_source


class CountingScanner:
    def __init__(self) -> None:
        self.calls = 0

    def scan(self, **kwargs: object) -> object:
        self.calls += 1
        from app.scanner.active import ActiveMarketScanner

        return ActiveMarketScanner(minimum_bars=60).scan(**cast(dict[str, Any], kwargs))


class FailingScanner:
    calls = 0

    def scan(self, **kwargs: object) -> object:
        self.calls += 1
        raise RuntimeError("synthetic scanner infrastructure failure")


def test_bootstrap_then_same_bar_is_no_cycle(tmp_path: Path) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    scanner = CountingScanner()
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, scanner), audit_store=store
    )
    bars = {"BTC": _bars(instrument, BASE, 60)}

    first = orchestrator.run_if_new_bar_cycle(
        scheduled_at=BASE,
        instruments=(instrument,),
        bars_by_symbol=bars,
        portfolio=_portfolio(BASE),
        timeframe=TimeFrame.ONE_HOUR,
    )
    second = orchestrator.run_if_new_bar_cycle(
        scheduled_at=BASE + timedelta(minutes=10),
        instruments=(instrument,),
        bars_by_symbol=bars,
        portfolio=_portfolio(BASE + timedelta(minutes=10)),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert first is not None
    assert second is None
    assert scanner.calls == 1
    assert store.accepted_active_cycles() == {first.cycle_id: BASE}
    assert store.watermarks() == {("BTC", "1H"): BASE}


def test_universe_reconciliation_never_reads_entire_history(tmp_path: Path, monkeypatch) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    scanner = CountingScanner()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, scanner), audit_store=store
    )

    def reject_bulk_history() -> None:
        raise AssertionError("unbounded history read")

    monkeypatch.setattr(store, "cycles", reject_bulk_history)
    arguments = dict(
        scheduled_at=BASE,
        instruments=(instrument,),
        bars_by_symbol={"BTC": _bars(instrument, BASE, 60)},
        portfolio=_portfolio(BASE),
        timeframe=TimeFrame.ONE_HOUR,
        force_universe_reconciliation=True,
    )
    assert orchestrator.run_if_new_bar_cycle(**arguments) is not None
    assert orchestrator.run_if_new_bar_cycle(**arguments) is None
    assert scanner.calls == 1


def test_future_or_regressed_bar_does_not_trigger(tmp_path: Path) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    scanner = CountingScanner()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, scanner), audit_store=store
    )

    assert (
        orchestrator.run_if_new_bar_cycle(
            scheduled_at=BASE,
            instruments=(instrument,),
            bars_by_symbol={"BTC": (_bar(instrument, BASE + timedelta(hours=1)),)},
            portfolio=_portfolio(BASE),
            timeframe=TimeFrame.ONE_HOUR,
        )
        is None
    )
    claim = store._store.acquire_active_claim(
        claim_name="active-intelligence:1h",
        owner_token="seed",
        acquired_at=BASE,
        expires_at=BASE + timedelta(hours=1),
    )
    assert claim is not None
    store.release_claim(claim=claim, released_at=BASE)
    assert scanner.calls == 0


def test_newer_completed_bar_triggers_one_more_cycle(tmp_path: Path) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    scanner = CountingScanner()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, scanner), audit_store=store
    )
    initial = orchestrator.run_if_new_bar_cycle(
        scheduled_at=BASE,
        instruments=(instrument,),
        bars_by_symbol={"BTC": _bars(instrument, BASE, 60)},
        portfolio=_portfolio(BASE),
        timeframe=TimeFrame.ONE_HOUR,
    )
    later = BASE + timedelta(hours=1)
    updated = orchestrator.run_if_new_bar_cycle(
        scheduled_at=later,
        instruments=(instrument,),
        bars_by_symbol={"BTC": _bars(instrument, later, 61)},
        portfolio=_portfolio(later),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert initial is not None
    assert later == updated.scan_cycle_timestamp if updated is not None else False
    assert scanner.calls == 2
    assert store.watermarks()[("BTC", "1H")] == later


def test_equity_incomplete_current_bar_does_not_trigger(tmp_path: Path) -> None:
    instrument = _instrument("AAPL", AssetClass.EQUITY)
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    scanner = CountingScanner()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, scanner), audit_store=store
    )
    as_of = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)
    result = orchestrator.run_if_new_bar_cycle(
        scheduled_at=as_of,
        instruments=(instrument,),
        bars_by_symbol={"AAPL": (_bar(instrument, datetime(2026, 8, 28, 14, tzinfo=UTC)),)},
        portfolio=_portfolio(as_of),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result is None
    assert scanner.calls == 0


class WatermarkChangesAfterPrecheck(ActiveIntelligenceAuditStore):
    def __init__(self, store: SqliteRecordStore) -> None:
        super().__init__(store)
        self.reads = 0

    def watermarks(self) -> dict[tuple[str, str], datetime]:
        self.reads += 1
        if self.reads == 2:
            return {("BTC", "1H"): BASE}
        return super().watermarks()


def test_post_claim_revalidation_skips_when_watermark_changed(tmp_path: Path) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    store = WatermarkChangesAfterPrecheck(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    scanner = CountingScanner()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, scanner), audit_store=store
    )

    result = orchestrator.run_if_new_bar_cycle(
        scheduled_at=BASE,
        instruments=(instrument,),
        bars_by_symbol={"BTC": _bars(instrument, BASE, 60)},
        portfolio=_portfolio(BASE),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result is None
    assert scanner.calls == 0
    assert store._store.active_claim("active-intelligence:1h") is None


def test_claim_held_and_post_claim_revalidation_skip_scanner(tmp_path: Path) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    scanner = CountingScanner()
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, scanner), audit_store=store
    )
    held = store._store.acquire_active_claim(
        claim_name="active-intelligence:1h",
        owner_token="other",
        acquired_at=BASE,
        expires_at=BASE + timedelta(hours=1),
    )
    assert held is not None

    result = orchestrator.run_if_new_bar_cycle(
        scheduled_at=BASE,
        instruments=(instrument,),
        bars_by_symbol={"BTC": _bars(instrument, BASE, 60)},
        portfolio=_portfolio(BASE),
        timeframe=TimeFrame.ONE_HOUR,
    )
    assert result is None
    assert scanner.calls == 0


def test_scanner_failure_releases_claim_without_state(tmp_path: Path) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    orchestrator = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, FailingScanner()), audit_store=store
    )

    with pytest.raises(RuntimeError, match="infrastructure"):
        orchestrator.run_if_new_bar_cycle(
            scheduled_at=BASE,
            instruments=(instrument,),
            bars_by_symbol={"BTC": _bars(instrument, BASE, 60)},
            portfolio=_portfolio(BASE),
            timeframe=TimeFrame.ONE_HOUR,
        )

    assert store.cycles() == ()
    assert store.accepted_active_cycles() == {}
    assert store.watermarks() == {}
    assert store._store.active_claim("active-intelligence:1h") is None


def test_two_orchestrators_same_store_accept_only_once(tmp_path: Path) -> None:
    instrument = _instrument("BTC", AssetClass.CRYPTO)
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    first_scanner = CountingScanner()
    second_scanner = CountingScanner()
    first = AegisActiveIntelligenceOrchestrator(scanner=cast(Any, first_scanner), audit_store=store)
    second = AegisActiveIntelligenceOrchestrator(
        scanner=cast(Any, second_scanner), audit_store=store
    )
    bars = {"BTC": _bars(instrument, BASE, 60)}

    accepted = first.run_if_new_bar_cycle(
        scheduled_at=BASE,
        instruments=(instrument,),
        bars_by_symbol=bars,
        portfolio=_portfolio(BASE),
        timeframe=TimeFrame.ONE_HOUR,
    )
    skipped = second.run_if_new_bar_cycle(
        scheduled_at=BASE,
        instruments=(instrument,),
        bars_by_symbol=bars,
        portfolio=_portfolio(BASE),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert accepted is not None
    assert skipped is None
    assert first_scanner.calls == 1
    assert second_scanner.calls == 0
    assert len(store.cycles()) == 1


def _instrument(symbol: str, asset_class: AssetClass) -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id=symbol,
        symbol=symbol,
        display_name=symbol,
        asset_class=asset_class,
        currency=Currency.USD,
        exchange="TEST",
        market_status=(
            MarketStatus.CONTINUOUS_24_7 if asset_class is AssetClass.CRYPTO else MarketStatus.OPEN
        ),
        short_allowed=False,
        leverage_available=False,
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=BASE,
    )


def _bars(instrument: UniversalInstrument, end: datetime, count: int) -> tuple[MarketBar, ...]:
    return tuple(
        _bar(instrument, end - timedelta(hours=count - index - 1)) for index in range(count)
    )


def _bar(instrument: UniversalInstrument, timestamp: datetime) -> MarketBar:
    price = Decimal("100") + Decimal(str((timestamp - BASE).total_seconds() / 3600))
    return MarketBar(
        instrument=instrument,
        timestamp=timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("100"),
        currency=Currency.USD,
        source="fixture",
        data_quality=FeatureQuality.GOOD,
    )


def _portfolio(as_of: datetime) -> PortfolioSnapshot:
    return PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("200"))
