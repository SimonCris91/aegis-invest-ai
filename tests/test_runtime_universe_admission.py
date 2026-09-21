from datetime import UTC, datetime
from decimal import Decimal

from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.universe import UniversalInstrument
from app.orchestration.session_state import partition_runtime_instruments
from app.orchestration.market_acquisition import build_coherent_one_hour_snapshot

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def instrument(identifier, *, internal=False, closed=False):
    return UniversalInstrument(
        broker="etoro", broker_instrument_id=str(identifier), symbol=f"T{identifier}",
        asset_class=AssetClass.EQUITY, currency=Currency.USD,
        market_status=MarketStatus.CLOSED if closed else MarketStatus.UNKNOWN,
        metadata_timestamp=NOW, tags=("unsupported-internal",) if internal else (),
    )


def test_partition_retains_unknown_and_closed_and_excludes_only_proven_internal():
    items = (instrument(1), instrument(2, closed=True), instrument(610, internal=True))
    included, excluded = partition_runtime_instruments(items)
    assert included == items[:2]
    assert excluded == items[2:]
    assert len(included) + len(excluded) == len(items)


def test_all_closed_universe_never_authorizes_trading():
    included, _ = partition_runtime_instruments((instrument(1, closed=True), instrument(610, internal=True)))
    snapshot = build_coherent_one_hour_snapshot(
        instruments=included, bars_by_symbol={}, as_of=NOW, minimum_coverage_ratio=Decimal("1"),
    )
    assert snapshot.total_universe == snapshot.session_not_expected == 1
    assert snapshot.coverage_denominator == 0
    assert not snapshot.coverage_sufficient


def test_unresolved_asset_still_blocks_full_coverage():
    included, _ = partition_runtime_instruments((instrument(1), instrument(2, closed=True)))
    snapshot = build_coherent_one_hour_snapshot(
        instruments=included, bars_by_symbol={}, as_of=NOW, minimum_coverage_ratio=Decimal("1"),
    )
    assert snapshot.coverage_denominator == 1
    assert not snapshot.coverage_sufficient
