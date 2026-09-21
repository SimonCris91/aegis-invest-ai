from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.data.historical.cache import HistoricalDataCache
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.universe import UniversalInstrument
from app.intelligence.models import TimeFrame
from app.orchestration.active_runtime import _refresh_active_one_hour_bars

AS_OF = datetime(2026, 8, 31, 20, tzinfo=UTC)


class FakeEtoroClient:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[int, str, int]] = []

    def candle_history(
        self, *, instrument_id: int, direction: str, interval: str, candles_count: int
    ) -> object:
        self.calls.append((instrument_id, interval, candles_count))
        if self.unavailable:
            raise RuntimeError("provider unavailable")
        first = AS_OF - timedelta(hours=60)
        candles = []
        for index in range(61):
            timestamp = first + timedelta(hours=index)
            candles.append(
                {
                    "fromDate": timestamp.isoformat().replace("+00:00", "Z"),
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": "1000",
                }
            )
        return {"candles": [{"candles": candles}]}


def _instrument() -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id="1001",
        symbol="TEST",
        display_name="Test Equity",
        asset_class=AssetClass.EQUITY,
        currency=Currency.USD,
        exchange="TEST",
        market_status=MarketStatus.OPEN,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=AS_OF,
    )


def test_refresh_persists_only_causal_completed_bars(tmp_path: Path) -> None:
    client = FakeEtoroClient()
    cache = HistoricalDataCache(tmp_path / "bars.sqlite3")

    bars, telemetry = _refresh_active_one_hour_bars(
        client=client,
        cache=cache,
        instruments=(_instrument(),),
        as_of=AS_OF,
    )

    assert client.calls == [(1001, "OneHour", 61)]
    assert telemetry["acquisition_status"] == "SUCCESS"
    assert telemetry["acquisition_instruments_updated"] == 1
    assert len(bars["TEST"]) == 60
    assert max(bar.timestamp for bar in bars["TEST"]) == AS_OF - timedelta(hours=1)
    assert (
        cache.coverage_summary(
            provider="etoro",
            broker="etoro",
            broker_instrument_id="1001",
            timeframe=TimeFrame.ONE_HOUR,
            start=AS_OF - timedelta(hours=60),
            end=AS_OF,
        )["count"]
        == 60
    )
    assert cache.duplicate_timestamp_report() == ()


def test_refresh_provider_failure_fails_closed_without_cycle_data(tmp_path: Path) -> None:
    client = FakeEtoroClient(unavailable=True)
    cache = HistoricalDataCache(tmp_path / "bars.sqlite3")

    bars, telemetry = _refresh_active_one_hour_bars(
        client=client,
        cache=cache,
        instruments=(_instrument(),),
        as_of=AS_OF,
    )

    assert telemetry["acquisition_status"] == "PROVIDER_UNAVAILABLE"
    assert telemetry["acquisition_missing_count"] == 0
    assert telemetry["acquisition_error"] == ("TEST:OTHER_ERROR",)
    assert bars["TEST"] == ()
    assert cache.inventory_summary() == ()


@pytest.mark.parametrize(
    "timestamp",
    (AS_OF + timedelta(minutes=1), AS_OF + timedelta(hours=1)),
)
def test_refresh_never_persists_future_bars(tmp_path: Path, timestamp: datetime) -> None:
    client = FakeEtoroClient()
    cache = HistoricalDataCache(tmp_path / "bars.sqlite3")

    bars, _ = _refresh_active_one_hour_bars(
        client=client,
        cache=cache,
        instruments=(_instrument(),),
        as_of=timestamp,
    )

    assert all(bar.timestamp <= timestamp for bar in bars["TEST"])
