from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock

from app.brokers.etoro.client import EtoroApiError
from app.brokers.etoro.http import EtoroHttpFailureKind
from app.config.loader import load_config
from app.data.historical.cache import HistoricalDataCache
from app.domain.enums import (
    AssetClass,
    BrokerExecutionMode,
    Currency,
    MarketStatus,
    SettlementType,
)
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.orchestration.market_acquisition import (
    EtoroOneHourAcquisitionCoordinator,
    build_coherent_one_hour_snapshot,
)
from app.storage.sqlite import SqliteRecordStore

AS_OF = datetime(2026, 9, 1, 20, 30, tzinfo=UTC)


class CatalogClient:
    def __init__(self, failures: dict[int, BaseException] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[int] = []
        self._lock = Lock()

    def candle_history(
        self, *, instrument_id: int, direction: str, interval: str, candles_count: int
    ) -> object:
        with self._lock:
            self.calls.append(instrument_id)
        failure = self.failures.get(instrument_id)
        if failure is not None:
            raise failure
        first = AS_OF.replace(minute=0) - timedelta(hours=60)
        return {
            "candles": [
                {
                    "candles": [
                        {
                            "fromDate": (first + timedelta(hours=index))
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "open": "100",
                            "high": "101",
                            "low": "99",
                            "close": "100",
                            "volume": "1000",
                        }
                        for index in range(61)
                    ]
                }
            ]
        }


class FakeCryptoFallback:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]:
        self.calls.append(instrument.symbol)
        return _market_bars(instrument, AS_OF - timedelta(hours=1))


def test_633_requested_reconcile_with_partial_failures(tmp_path: Path) -> None:
    instruments = tuple(_instrument(index) for index in range(1, 634))
    client = CatalogClient(
        {
            2: EtoroApiError("limited", endpoint="fixture", status=429),
            3: TimeoutError("timeout"),
            4: RuntimeError("isolated"),
        }
    )
    coordinator = _coordinator(tmp_path, client=client, batch_size=633, concurrency=4)

    bars, telemetry = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    counts = telemetry["acquisition_outcome_counts"]
    assert telemetry["acquisition_instruments_requested"] == 633
    assert sum(counts.values()) == 633
    assert counts["UPDATED"] == 630
    assert counts["RATE_LIMITED"] == 1
    assert counts["TIMEOUT"] == 1
    assert counts["OTHER_ERROR"] == 1
    assert telemetry["acquisition_status"] == "PARTIAL"
    assert len(bars["SYM1"]) == 60
    assert bars["SYM2"] == ()


def test_acquisition_progress_is_resumable_and_does_not_restart_at_first_item(
    tmp_path: Path,
) -> None:
    instruments = tuple(_instrument(index) for index in range(1, 6))
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=2, concurrency=2)

    _, first = coordinator.refresh(instruments=instruments, as_of=AS_OF)
    _, second = coordinator.refresh(instruments=instruments, as_of=AS_OF)
    _, third = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    assert client.calls[:2] == [1, 2]
    assert set(client.calls[2:4]) == {3, 4}
    assert client.calls[4:] == [5]
    assert first["acquisition_status"] == "IN_PROGRESS"
    assert second["acquisition_status"] == "IN_PROGRESS"
    assert third["acquisition_outcome_counts"]["ALREADY_CURRENT"] == 4
    states = SqliteRecordStore(tmp_path / "runtime.sqlite3").market_acquisition_states("1H")
    assert len(states) == 5
    assert all(
        state["classification"] in {"UPDATED", "ALREADY_CURRENT"} for state in states.values()
    )


def test_acquisition_progress_moves_past_repeated_failures(tmp_path: Path) -> None:
    instruments = tuple(_instrument(index) for index in range(1, 6))
    client = CatalogClient(
        {
            1: EtoroApiError("limited", endpoint="fixture", status=429),
            2: EtoroApiError("limited", endpoint="fixture", status=429),
        }
    )
    coordinator = _coordinator(tmp_path, client=client, batch_size=2, concurrency=2)

    _, first = coordinator.refresh(instruments=instruments, as_of=AS_OF)
    _, second = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    assert first["acquisition_outcome_counts"]["NOT_ATTEMPTED"] == 3
    assert second["acquisition_outcome_counts"]["NOT_ATTEMPTED"] == 1
    assert set(client.calls) >= {1, 2, 3, 4}


def test_coherent_snapshot_blocks_low_coverage_and_preserves_valid_subset(
    tmp_path: Path,
) -> None:
    instruments = tuple(_instrument(index) for index in range(1, 5))
    coordinator = _coordinator(tmp_path, client=CatalogClient(), batch_size=2, concurrency=2)
    bars, telemetry = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    blocked = build_coherent_one_hour_snapshot(
        instruments=instruments,
        bars_by_symbol=bars,
        as_of=AS_OF,
        minimum_coverage_ratio=Decimal("0.75"),
    )
    allowed = build_coherent_one_hour_snapshot(
        instruments=instruments,
        bars_by_symbol=bars,
        as_of=AS_OF,
        minimum_coverage_ratio=Decimal("0.50"),
    )

    assert sum(telemetry["acquisition_outcome_counts"].values()) == 4
    assert blocked.eligible_for_target_bar == 2
    assert blocked.coverage_ratio == Decimal("0.5")
    assert blocked.coverage_sufficient is False
    assert allowed.coverage_sufficient is True
    assert {item.symbol for item in allowed.instruments} == {"SYM1", "SYM2"}
    assert set(allowed.bars_by_symbol) == {"SYM1", "SYM2"}


def test_rate_limited_instrument_honors_persisted_backoff(tmp_path: Path) -> None:
    client = CatalogClient({1: EtoroApiError("limited", endpoint="fixture", status=429)})
    coordinator = _coordinator(tmp_path, client=client, batch_size=1, concurrency=1)
    instrument = _instrument(1)

    _, first = coordinator.refresh(instruments=(instrument,), as_of=AS_OF)
    _, second = coordinator.refresh(instruments=(instrument,), as_of=AS_OF + timedelta(minutes=1))

    assert client.calls == [1]
    assert first["acquisition_outcome_counts"]["RATE_LIMITED"] == 1
    assert second["acquisition_outcome_counts"]["RETRY_BACKOFF"] == 1
    state = SqliteRecordStore(tmp_path / "runtime.sqlite3").market_acquisition_states("1H")["1"]
    assert state["consecutive_failures"] == 1
    assert state["retry_after"] is not None


def test_repeated_failure_is_retried_within_bounded_interval(tmp_path: Path) -> None:
    client = CatalogClient({1: EtoroApiError("limited", endpoint="fixture", status=429)})
    coordinator = _coordinator(tmp_path, client=client, batch_size=1, concurrency=1)
    instrument = _instrument(1)
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    attempt_at = AS_OF

    for expected_failures in range(1, 7):
        _, telemetry = coordinator.refresh(instruments=(instrument,), as_of=attempt_at)
        state = store.market_acquisition_states("1H")["1"]
        retry_after = datetime.fromisoformat(str(state["retry_after"]))
        assert retry_after - attempt_at <= timedelta(hours=1)
        assert state["consecutive_failures"] == expected_failures
        row = telemetry["acquisition_results"][0]
        assert row["last_attempt_at"] == attempt_at.isoformat()
        assert row["retry_after"] == retry_after.isoformat()
        assert row["staleness_age_seconds"] is None
        attempt_at = retry_after

    assert len(client.calls) == 6


def test_scheduler_prioritizes_oldest_success_not_only_last_attempt(tmp_path: Path) -> None:
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=1, concurrency=1)
    store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    older, newer = _instrument(1), _instrument(2)
    store.upsert_market_acquisition_state(
        timeframe="1H",
        state={
            "instrument_id": "1",
            "symbol": older.symbol,
            "last_attempt_at": (AS_OF - timedelta(minutes=1)).isoformat(),
            "last_success_at": (AS_OF - timedelta(days=2)).isoformat(),
            "latest_valid_completed_bar": None,
            "classification": "NO_DATA",
            "consecutive_failures": 0,
            "retry_after": None,
            "detail_code": None,
        },
    )
    store.upsert_market_acquisition_state(
        timeframe="1H",
        state={
            "instrument_id": "2",
            "symbol": newer.symbol,
            "last_attempt_at": (AS_OF - timedelta(days=1)).isoformat(),
            "last_success_at": (AS_OF - timedelta(hours=1)).isoformat(),
            "latest_valid_completed_bar": None,
            "classification": "NO_DATA",
            "consecutive_failures": 0,
            "retry_after": None,
            "detail_code": None,
        },
    )

    coordinator.refresh(instruments=(older, newer), as_of=AS_OF)

    assert client.calls == [1]


def test_closed_market_is_distinct_and_session_groups_are_explicit(tmp_path: Path) -> None:
    open_crypto = _instrument(1)
    closed_equity = _instrument(2).model_copy(
        update={"asset_class": AssetClass.EQUITY, "market_status": MarketStatus.CLOSED}
    )
    coordinator = _coordinator(tmp_path, client=CatalogClient(), batch_size=2, concurrency=2)
    coordinator.refresh(
        instruments=(closed_equity.model_copy(update={"market_status": MarketStatus.OPEN}),),
        as_of=AS_OF,
    )
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=2, concurrency=2)

    bars, telemetry = coordinator.refresh(instruments=(open_crypto, closed_equity), as_of=AS_OF)
    snapshot = build_coherent_one_hour_snapshot(
        instruments=(open_crypto, closed_equity),
        bars_by_symbol=bars,
        as_of=AS_OF,
        minimum_coverage_ratio=Decimal("0.5"),
    )

    assert telemetry["acquisition_outcome_counts"]["MARKET_CLOSED_NO_NEW_BAR"] == 1
    assert client.calls == [1]
    assert snapshot.coverage_denominator == 1
    assert snapshot.coverage_ratio == Decimal("1")
    assert snapshot.session_not_expected == 1
    assert snapshot.eligible_by_session_group == {"CONTINUOUS_24_7": 1}
    assert snapshot.excluded_by_session_group == {"ETORO::CLOSED": 1}


def test_closed_market_without_cache_is_session_not_expected(tmp_path: Path) -> None:
    closed = _instrument(1).model_copy(
        update={"asset_class": AssetClass.EQUITY, "market_status": MarketStatus.CLOSED}
    )
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=1, concurrency=1)

    _, telemetry = coordinator.refresh(instruments=(closed,), as_of=AS_OF)

    assert client.calls == []
    assert telemetry["acquisition_outcome_counts"]["SESSION_NOT_EXPECTED"] == 1
    assert sum(telemetry["acquisition_outcome_counts"].values()) == 1


def test_deferred_batch_members_are_explicitly_not_attempted(tmp_path: Path) -> None:
    instruments = tuple(_instrument(index) for index in range(1, 5))
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=1, concurrency=1)

    _, telemetry = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    assert telemetry["acquisition_instruments_attempted"] == 1
    assert telemetry["acquisition_instruments_not_attempted"] == 3
    assert telemetry["acquisition_outcome_counts"]["NOT_ATTEMPTED"] == 3
    assert sum(telemetry["acquisition_outcome_counts"].values()) == 4
    states = SqliteRecordStore(tmp_path / "runtime.sqlite3").market_acquisition_states("1H")
    assert sum(state["classification"] == "NOT_ATTEMPTED" for state in states.values()) == 3


def test_acquisition_batch_fairly_represents_open_asset_classes(tmp_path: Path) -> None:
    instruments = (
        _instrument(1).model_copy(update={"asset_class": AssetClass.EQUITY}),
        _instrument(2).model_copy(update={"asset_class": AssetClass.EQUITY}),
        _instrument(3).model_copy(update={"asset_class": AssetClass.ETF}),
        _instrument(4).model_copy(update={"asset_class": AssetClass.ETF}),
        _instrument(5),
        _instrument(6),
    )
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=3, concurrency=1)

    _, telemetry = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    attempted_ids = set(client.calls)
    assert telemetry["acquisition_instruments_attempted"] == 3
    assert attempted_ids == {1, 3, 5}


def test_acquisition_prioritizes_verified_open_sessions_without_starving_discovery(
    tmp_path: Path,
) -> None:
    instruments = (
        _instrument(1).model_copy(update={"asset_class": AssetClass.EQUITY}),
        _instrument(2).model_copy(update={
            "asset_class": AssetClass.EQUITY,
            "tags": ("session-state:OPEN_TRADABLE",),
        }),
        _instrument(3).model_copy(update={"asset_class": AssetClass.ETF}),
        _instrument(4).model_copy(update={
            "asset_class": AssetClass.ETF,
            "tags": ("session-state:OPEN_TRADABLE",),
        }),
        _instrument(5),
        _instrument(6).model_copy(update={"tags": ("session-state:OPEN_TRADABLE",)}),
    )
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=3, concurrency=1)

    _, first = coordinator.refresh(instruments=instruments, as_of=AS_OF)
    _, second = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    assert first["acquisition_instruments_attempted"] == 3
    assert set(client.calls[:3]) == {2, 4, 6}
    assert second["acquisition_instruments_attempted"] == 3
    assert set(client.calls[3:]) == {1, 3, 5}


def test_bounded_633_batch_has_no_unclassified_remainder(tmp_path: Path) -> None:
    instruments = tuple(_instrument(index) for index in range(1, 634))
    client = CatalogClient()
    coordinator = _coordinator(tmp_path, client=client, batch_size=64, concurrency=4)

    _, telemetry = coordinator.refresh(instruments=instruments, as_of=AS_OF)

    counts = telemetry["acquisition_outcome_counts"]
    assert telemetry["acquisition_instruments_requested"] == 633
    assert telemetry["acquisition_instruments_attempted"] == 64
    assert counts["UPDATED"] == 64
    assert counts["NOT_ATTEMPTED"] == 569
    assert sum(counts.values()) == 633
    assert telemetry["acquisition_status"] == "IN_PROGRESS"


def test_provider_transport_failure_is_not_collapsed_into_other_error(tmp_path: Path) -> None:
    client = CatalogClient(
        {
            1: EtoroApiError(
                "network unavailable",
                endpoint="fixture",
                category=EtoroHttpFailureKind.NETWORK_TRANSPORT_ERROR,
            )
        }
    )
    coordinator = _coordinator(tmp_path, client=client, batch_size=1, concurrency=1)

    _, telemetry = coordinator.refresh(instruments=(_instrument(1),), as_of=AS_OF)

    assert telemetry["acquisition_outcome_counts"]["PROVIDER_UNAVAILABLE"] == 1
    assert telemetry["acquisition_outcome_counts"]["OTHER_ERROR"] == 0
    assert telemetry["acquisition_status"] == "PROVIDER_UNAVAILABLE"


def test_crypto_transport_failure_uses_causal_alpaca_fallback(tmp_path: Path) -> None:
    client = CatalogClient(
        {
            1: EtoroApiError(
                "network unavailable",
                endpoint="fixture",
                category=EtoroHttpFailureKind.NETWORK_TRANSPORT_ERROR,
            )
        }
    )
    fallback = FakeCryptoFallback()
    coordinator = _coordinator(
        tmp_path,
        client=client,
        batch_size=1,
        concurrency=1,
        crypto_fallback_provider=fallback,
    )

    bars, telemetry = coordinator.refresh(instruments=(_instrument(1),), as_of=AS_OF)

    assert fallback.calls == ["SYM1"]
    assert telemetry["acquisition_outcome_counts"]["UPDATED"] == 1
    assert telemetry["acquisition_status"] == "COMPLETE"
    assert len(bars["SYM1"]) == 60


def test_incompatible_exchange_sessions_use_distinct_causal_targets() -> None:
    first = _instrument(1).model_copy(update={"asset_class": AssetClass.EQUITY, "exchange": "4"})
    second = _instrument(2).model_copy(update={"asset_class": AssetClass.ETF, "exchange": "20"})
    first_end = AS_OF.replace(minute=0) - timedelta(hours=1)
    second_end = first_end - timedelta(hours=5)

    snapshot = build_coherent_one_hour_snapshot(
        instruments=(first, second),
        bars_by_symbol={
            first.symbol: _market_bars(first, first_end),
            second.symbol: _market_bars(second, second_end),
        },
        as_of=AS_OF,
        minimum_coverage_ratio=Decimal("1"),
    )

    assert snapshot.coverage_sufficient
    assert snapshot.eligible_by_session_group == {"4::OPEN": 1, "20::OPEN": 1}
    assert snapshot.target_completed_bars_by_session_group == {
        "4::OPEN": first_end,
        "20::OPEN": second_end,
    }


def test_execution_mode_is_explicit_and_real_remains_unavailable() -> None:
    readonly = load_config({})
    demo = load_config(
        {
            "AEGIS_OPERATING_MODE": "ETORO_DEMO",
            "ETORO_API_ENABLED": "true",
            "ETORO_DEMO_EXECUTION_ENABLED": "true",
            "AEGIS_BROKER_EXECUTION_MODE": "DEMO_EXECUTION",
        }
    )

    assert readonly.broker_execution_mode is BrokerExecutionMode.READ_ONLY
    assert demo.broker_execution_mode is BrokerExecutionMode.DEMO_EXECUTION
    try:
        load_config({"AEGIS_BROKER_EXECUTION_MODE": "REAL_EXECUTION"})
    except ValueError as exc:
        assert "REAL_EXECUTION is unavailable" in str(exc)
    else:
        raise AssertionError("REAL_EXECUTION must fail closed")


def _coordinator(
    tmp_path: Path,
    *,
    client: CatalogClient,
    batch_size: int,
    concurrency: int,
    crypto_fallback_provider: object | None = None,
) -> EtoroOneHourAcquisitionCoordinator:
    return EtoroOneHourAcquisitionCoordinator(
        client=client,  # type: ignore[arg-type]
        cache=HistoricalDataCache(tmp_path / "bars.sqlite3"),
        store=SqliteRecordStore(tmp_path / "runtime.sqlite3"),
        batch_size=batch_size,
        concurrency=concurrency,
        crypto_fallback_provider=crypto_fallback_provider,  # type: ignore[arg-type]
    )


def _instrument(instrument_id: int) -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id=str(instrument_id),
        symbol=f"SYM{instrument_id}",
        display_name=f"Instrument {instrument_id}",
        asset_class=AssetClass.CRYPTO,
        currency=Currency.USD,
        exchange="ETORO",
        market_status=MarketStatus.OPEN,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=AS_OF,
    )


def _market_bars(instrument: UniversalInstrument, ending_at: datetime) -> tuple[MarketBar, ...]:
    return tuple(
        MarketBar(
            instrument=instrument,
            timestamp=ending_at - timedelta(hours=59 - index),
            timeframe=TimeFrame.ONE_HOUR,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
            currency=Currency.USD,
            source="fixture",
            data_quality=FeatureQuality.GOOD,
        )
        for index in range(60)
    )
