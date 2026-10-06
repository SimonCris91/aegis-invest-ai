import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.config.models import ApplicationConfig
from app.data.historical.cache import HistoricalDataCache
from app.data.runtime import (
    build_etoro_crypto_validation_report,
    build_etoro_instrument_catalog_probe_report,
    build_etoro_universe_bootstrap_report,
    load_etoro_dynamic_active_scanner_instruments,
    persist_etoro_instrument_catalog_snapshot,
    read_etoro_dynamic_universe_artifact,
    read_etoro_instrument_catalog_snapshot,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


class CatalogClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def instrument_display_data(self) -> object:
        return self.payload


class BootstrapClient:
    def __init__(self, *, one_hour_count: int = 60, one_day_count: int = 1) -> None:
        self.calls: list[tuple[int, str, int]] = []
        self.one_hour_count = one_hour_count
        self.one_day_count = one_day_count

    def candle_history(
        self, *, instrument_id: int, direction: str, interval: str, candles_count: int
    ) -> object:
        self.calls.append((instrument_id, interval, candles_count))
        count = self.one_hour_count if interval == "OneHour" else self.one_day_count
        step = timedelta(hours=1) if interval == "OneHour" else timedelta(days=1)
        return {
            "candles": [
                {
                    "candles": [
                        {
                            "fromDate": (NOW - step * (count - index))
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "open": "100",
                            "high": "101",
                            "low": "99",
                            "close": "100.5",
                            "volume": "10",
                        }
                        for index in range(count)
                    ]
                }
            ]
        }


class RateLimitedBootstrapClient:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[int, str, int]] = []
        self.headers = headers

    def candle_history(
        self, *, instrument_id: int, direction: str, interval: str, candles_count: int
    ) -> object:
        self.calls.append((instrument_id, interval, candles_count))
        raise EtoroApiError(
            "rate limited",
            endpoint="/api/v1/market-data/instruments/history",
            status=429,
            response_headers=self.headers,
        )


class CryptoValidationClient:
    def candle_history(
        self, *, instrument_id: int, direction: str, interval: str, candles_count: int
    ) -> object:
        if instrument_id == 999:
            raise RuntimeError("temporary provider failure")
        count = 60 if interval == "OneHour" else 30
        return {
            "candles": [
                {
                    "candles": [
                        {
                            "fromDate": (NOW - (timedelta(hours=count - index) if interval == "OneHour" else timedelta(days=count - index)))
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "open": "100",
                            "high": "101",
                            "low": "99",
                            "close": "100.5",
                            "volume": "10",
                        }
                        for index in range(count)
                    ]
                }
            ]
        }


def _payload(count: int = 3) -> dict[str, object]:
    return {
        "instrumentDisplayDatas": [
            {"instrumentID": index, "internalSymbolFull": f"S{index}", "exchangeID": 1}
            for index in range(1, count + 1)
        ],
        "metadata": {"source": "fixture"},
    }


def test_probe_persists_complete_payload_without_truncation(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    payload = _payload(161)
    report = build_etoro_instrument_catalog_probe_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, CatalogClient(payload)),
        persist=True,
        snapshot_path=path,
        clock=lambda: NOW,
    )

    snapshot = read_etoro_instrument_catalog_snapshot(path)
    assert report["snapshot_status"] == "PERSISTED"
    assert report["raw_count"] == 161
    assert report["unique_instrument_id_count"] == 161
    assert snapshot is not None
    records = snapshot["instrument_display_datas"]
    assert isinstance(records, list)
    assert len(records) == 161
    assert snapshot["source_endpoint"] == "/api/v1/market-data/instruments"
    assert snapshot["retrieved_at"] == NOW.isoformat()
    assert snapshot["snapshot_checksum_sha256"] == report["snapshot_checksum_sha256"]


def test_duplicate_ids_are_preserved_and_diagnosed(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    payload: dict[str, object] = {
        "instrumentDisplayDatas": [{"instrumentID": 1}, {"instrumentID": 1}]
    }

    report = build_etoro_instrument_catalog_probe_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, CatalogClient(payload)),
        persist=True,
        snapshot_path=path,
    )

    assert report["duplicate_instrument_id_count"] == 1
    snapshot = read_etoro_instrument_catalog_snapshot(path)
    assert snapshot is not None
    records = snapshot["instrument_display_datas"]
    assert isinstance(records, list)
    assert len(records) == 2


def test_failed_fetch_does_not_replace_existing_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    persist_etoro_instrument_catalog_snapshot(_payload(1), retrieved_at=NOW, path=path)
    before = path.read_bytes()

    report = build_etoro_instrument_catalog_probe_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, CatalogClient({"unexpected": []})),
        persist=True,
        snapshot_path=path,
    )

    assert report["status"] == "BLOCKED"
    assert report["blocker"] == "MALFORMED_CATALOG_RESPONSE"
    assert path.read_bytes() == before


def test_failed_atomic_replace_preserves_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    persist_etoro_instrument_catalog_snapshot(_payload(1), retrieved_at=NOW, path=path)
    before = path.read_bytes()

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("app.data.runtime.os.replace", fail_replace)
    with pytest.raises(OSError):
        persist_etoro_instrument_catalog_snapshot(_payload(2), retrieved_at=NOW, path=path)

    assert path.read_bytes() == before


def test_snapshot_readback_is_network_independent(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    persist_etoro_instrument_catalog_snapshot(_payload(2), retrieved_at=NOW, path=path)

    snapshot = read_etoro_instrument_catalog_snapshot(path)

    assert snapshot is not None
    records = snapshot["instrument_display_datas"]
    assert isinstance(records, list)
    assert [item["internalSymbolFull"] for item in records] == [
        "S1",
        "S2",
    ]
    json.dumps(snapshot)


def test_persistence_does_not_activate_scanner_or_write_broker(tmp_path: Path) -> None:
    report = build_etoro_instrument_catalog_probe_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, CatalogClient(_payload())),
        persist=True,
        snapshot_path=tmp_path / "catalog.json",
    )

    assert report["broker_write_calls"] == 0
    assert report["real_execution_available"] is False
    assert "active_scanner" not in report


def test_native_bootstrap_is_resumable_and_builds_etoro_artifact(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "bootstrap.json"
    artifact_path = tmp_path / "active.json"
    cache_path = tmp_path / "bars.sqlite3"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {
                    "instrumentID": 123,
                    "symbolFull": "TEST",
                    "instrumentDisplayName": "Test Equity",
                    "instrumentTypeID": 5,
                }
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )
    client = BootstrapClient()
    report = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        batch_size=1,
        delay_seconds=0,
    )

    assert report["catalog_count"] == 1
    assert report["bootstrapped"] == 1
    assert report["active_scanner_universe"] == 1
    assert client.calls == [(123, "OneHour", 120), (123, "OneDay", 120)]
    artifact = read_etoro_dynamic_universe_artifact(artifact_path)
    assert artifact is not None
    assert artifact["universe_source"] == "etoro-native-bootstrap"
    assert artifact["active_scanner_universe_count"] == 1
    assert len(cast(list[object], artifact["active_records"])) == 1

    second = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        batch_size=1,
        delay_seconds=0,
    )
    assert second["bootstrapped"] == 1
    assert len(client.calls) == 2


def test_native_bootstrap_does_not_activate_short_one_hour_history(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "bootstrap.json"
    artifact_path = tmp_path / "active.json"
    cache_path = tmp_path / "bars.sqlite3"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {
                    "instrumentID": 123,
                    "symbolFull": "SHORT",
                    "instrumentDisplayName": "Short History Equity",
                    "instrumentTypeID": 5,
                }
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )
    client = BootstrapClient(one_hour_count=59)
    report = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        batch_size=1,
        delay_seconds=0,
    )

    assert report["status"] == "ETORO_UNIVERSE_BOOTSTRAP_INCOMPLETE"
    assert report["bootstrapped"] == 0
    assert report["active_scanner_universe"] == 0
    assert report["retryable"] == 1
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["records"][0]["status"] == "INSUFFICIENT_HISTORY"
    assert progress["records"][0]["reason"] == "INSUFFICIENT_ONE_HOUR_HISTORY:59/60"
    artifact = read_etoro_dynamic_universe_artifact(artifact_path)
    assert artifact is not None
    assert artifact["active_scanner_universe_count"] == 0
    assert artifact["active_records"] == []
    assert report["broker_write_calls"] == 0


def test_native_bootstrap_bounded_passes_advance_persisted_cursor(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "bootstrap.json"
    artifact_path = tmp_path / "active.json"
    cache_path = tmp_path / "bars.sqlite3"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {"instrumentID": 123, "symbolFull": "ONE", "instrumentTypeID": 5},
                {"instrumentID": 124, "symbolFull": "TWO", "instrumentTypeID": 5},
                {"instrumentID": 125, "symbolFull": "THREE", "instrumentTypeID": 5},
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )
    client = BootstrapClient()

    first = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        delay_seconds=0,
        max_instruments_per_run=1,
    )
    second = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        delay_seconds=0,
        max_instruments_per_run=1,
    )

    assert first["processed_this_run"] == 1
    assert first["cursor"] == 1
    assert first["active_scanner_universe"] == 1
    assert second["processed_this_run"] == 1
    assert second["cursor"] == 2
    assert second["active_scanner_universe"] == 2
    assert len(client.calls) == 4
    saved = json.loads(progress_path.read_text(encoding="utf-8"))
    assert saved["cursor"] == 2
    assert len(saved["records"]) == 2
    assert first["broker_write_calls"] == second["broker_write_calls"] == 0


def test_bootstrap_stops_and_persists_cursor_on_rate_limit_then_resumes(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "bootstrap.json"
    artifact_path = tmp_path / "active.json"
    cache_path = tmp_path / "bars.sqlite3"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {"instrumentID": 123, "symbolFull": "ONE", "instrumentTypeID": 5},
                {"instrumentID": 124, "symbolFull": "TWO", "instrumentTypeID": 5},
                {"instrumentID": 125, "symbolFull": "THREE", "instrumentTypeID": 5},
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )

    limited_client = RateLimitedBootstrapClient()
    limited = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, limited_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        batch_size=1,
        delay_seconds=0,
    )

    assert limited["status"] == "ETORO_UNIVERSE_BOOTSTRAP_RATE_LIMITED"
    assert limited["rate_limited"] is True
    assert limited["checked"] == 1
    assert limited["pending"] == 2
    assert limited["retryable"] == 1
    assert limited["remaining"] == 3
    assert limited_client.calls == [(123, "OneHour", 120)]
    saved = json.loads(progress_path.read_text(encoding="utf-8"))
    assert len(saved["records"]) == 1
    assert saved["records"][0]["reason"] == "RATE_LIMITED"
    assert saved["retry_not_before"] == (NOW + timedelta(minutes=15)).isoformat()
    checkpoint_before = progress_path.read_bytes()
    artifact_before = artifact_path.read_bytes()

    repeated_client = RateLimitedBootstrapClient()
    repeated = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, repeated_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        delay_seconds=0,
    )
    assert repeated["rate_limited"] is True
    assert len(repeated_client.calls) == 0
    assert repeated["requests_attempted"] == 0
    assert repeated["cooldown_active"] is True
    assert repeated["remaining"] == 3
    assert progress_path.read_bytes() == checkpoint_before
    assert artifact_path.read_bytes() == artifact_before

    limited_again = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, repeated_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW + timedelta(minutes=15),
        delay_seconds=0,
    )
    assert len(repeated_client.calls) == 1
    assert limited_again["rate_limit_attempts"] == 2
    assert limited_again["retry_not_before"] == (NOW + timedelta(minutes=45)).isoformat()

    resumed_client = BootstrapClient()
    resumed = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, resumed_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW + timedelta(minutes=45),
        batch_size=1,
        delay_seconds=0,
    )

    assert resumed["status"] == "ETORO_UNIVERSE_BOOTSTRAP_COMPLETE"
    assert resumed["rate_limited"] is False
    assert resumed["pending"] == 0
    assert resumed["remaining"] == 0
    assert resumed["retry_not_before"] is None
    assert resumed["rate_limit_attempts"] == 0
    assert resumed["active_scanner_universe"] == 3
    assert len(resumed_client.calls) == 6


def test_bootstrap_obeys_provider_cooldown_across_catalog_refresh(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "bootstrap.json"
    artifact_path = tmp_path / "active.json"
    cache = HistoricalDataCache(tmp_path / "bars.sqlite3")
    catalog = {"instrumentDisplayDatas": [
        {"instrumentID": 123, "symbolFull": "ONE", "instrumentTypeID": 5},
    ]}
    persist_etoro_instrument_catalog_snapshot(catalog, retrieved_at=NOW, path=snapshot_path)
    client = RateLimitedBootstrapClient({"retry-after": "3600"})
    first = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client), cache=cache,
        snapshot_path=snapshot_path, progress_path=progress_path, artifact_path=artifact_path,
        clock=lambda: NOW, delay_seconds=0,
    )
    assert first["retry_not_before"] == (NOW + timedelta(hours=1)).isoformat()
    assert first["cooldown_source"] == "PROVIDER_RETRY_AFTER"
    catalog["instrumentDisplayDatas"].append(
        {"instrumentID": 124, "symbolFull": "TWO", "instrumentTypeID": 5}
    )
    persist_etoro_instrument_catalog_snapshot(
        catalog, retrieved_at=NOW + timedelta(minutes=1), path=snapshot_path,
    )
    resumed = BootstrapClient()
    waiting = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, resumed), cache=cache,
        snapshot_path=snapshot_path, progress_path=progress_path, artifact_path=artifact_path,
        clock=lambda: NOW + timedelta(minutes=20), delay_seconds=0,
    )
    assert resumed.calls == []
    assert waiting["remaining"] == 2
    finished = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, resumed), cache=cache,
        snapshot_path=snapshot_path, progress_path=progress_path, artifact_path=artifact_path,
        clock=lambda: NOW + timedelta(hours=1), delay_seconds=0,
    )
    assert finished["active_scanner_universe"] == 2
    assert finished["remaining"] == 0


def test_bootstrap_catalog_refresh_reuses_stable_rows_and_rechecks_changed_symbols(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "bootstrap.json"
    artifact_path = tmp_path / "active.json"
    cache_path = tmp_path / "bars.sqlite3"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {"instrumentID": 123, "symbolFull": "STABLE", "instrumentDisplayName": "Stable Co", "instrumentTypeID": 5},
                {"instrumentID": 124, "symbolFull": "OLD", "instrumentDisplayName": "Renamed Co", "instrumentTypeID": 5},
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )
    initial_client = BootstrapClient()
    build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, initial_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        batch_size=1,
        delay_seconds=0,
    )
    assert len(initial_client.calls) == 4

    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {"instrumentID": 123, "symbolFull": "STABLE", "instrumentDisplayName": "Stable Holdings", "instrumentTypeID": 5},
                {"instrumentID": 124, "symbolFull": "NEW", "instrumentDisplayName": "Renamed Co", "instrumentTypeID": 5},
                {"instrumentID": 125, "symbolFull": "INTERNAL", "instrumentDisplayName": "Internal template", "instrumentTypeID": 5, "isInternalInstrument": True},
            ]
        },
        retrieved_at=NOW + timedelta(days=1),
        path=snapshot_path,
    )
    refresh_client = BootstrapClient()
    report = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, refresh_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW + timedelta(days=1),
        batch_size=1,
        delay_seconds=0,
    )

    assert report["preserved_previous_snapshot_statuses"] == 1
    assert report["instruments_rechecked_after_catalog_change"] == 1
    assert refresh_client.calls == [(124, "OneHour", 120), (124, "OneDay", 120)]
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["source_snapshot_id"] == read_etoro_instrument_catalog_snapshot(snapshot_path)["snapshot_id"]
    assert {row["instrument_id"]: row["display_name"] for row in progress["records"]}["123"] == "Stable Holdings"
    artifact = read_etoro_dynamic_universe_artifact(artifact_path)
    assert artifact is not None
    assert artifact["source_snapshot_id"] == progress["source_snapshot_id"]
    assert {row["etoro_instrument_id"] for row in artifact["active_records"]} == {"123", "124"}


def test_bootstrap_catalog_refresh_rechecks_instrument_newly_public(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "bootstrap.json"
    artifact_path = tmp_path / "active.json"
    cache_path = tmp_path / "bars.sqlite3"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {
                    "instrumentID": 123,
                    "symbolFull": "PUBLIC_AFTER_REFRESH",
                    "instrumentDisplayName": "Public after refresh",
                    "instrumentTypeID": 5,
                    "isInternalInstrument": True,
                }
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )
    initial_client = BootstrapClient()
    build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, initial_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        batch_size=1,
        delay_seconds=0,
    )
    assert initial_client.calls == []

    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {
                    "instrumentID": 123,
                    "symbolFull": "PUBLIC_AFTER_REFRESH",
                    "instrumentDisplayName": "Public after refresh",
                    "instrumentTypeID": 5,
                    "isInternalInstrument": False,
                }
            ]
        },
        retrieved_at=NOW + timedelta(days=1),
        path=snapshot_path,
    )
    refresh_client = BootstrapClient()
    report = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, refresh_client),
        cache=HistoricalDataCache(cache_path),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW + timedelta(days=1),
        batch_size=1,
        delay_seconds=0,
    )

    assert report["instruments_rechecked_after_catalog_change"] == 1
    assert refresh_client.calls == [(123, "OneHour", 120), (123, "OneDay", 120)]
    assert report["active_scanner_universe"] == 1


def test_native_bootstrap_excludes_authoritative_internal_instrument(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "catalog.json"
    progress_path = tmp_path / "progress.json"
    artifact_path = tmp_path / "active.json"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {
                    "instrumentID": 610,
                    "symbolFull": "ETORIAN610",
                    "instrumentDisplayName": "ETORIAN610",
                    "instrumentTypeID": 5,
                    "isInternalInstrument": True,
                },
                {
                    "instrumentID": 611,
                    "symbolFull": "PUBLIC611",
                    "instrumentDisplayName": "Public 611",
                    "instrumentTypeID": 5,
                },
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )

    client = BootstrapClient()
    report = build_etoro_universe_bootstrap_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(tmp_path / "bars.sqlite3"),
        snapshot_path=snapshot_path,
        progress_path=progress_path,
        artifact_path=artifact_path,
        clock=lambda: NOW,
        batch_size=2,
        delay_seconds=0,
    )

    assert report["catalog_count"] == 2
    assert report["bootstrapped"] == 1
    assert report["status_counts"] == {"BOOTSTRAPPED": 1, "UNSUPPORTED_INTERNAL": 1}
    assert report["active_scanner_universe"] == 1
    artifact = read_etoro_dynamic_universe_artifact(artifact_path)
    assert artifact is not None
    assert [row["etoro_instrument_id"] for row in artifact["active_records"]] == ["611"]


def test_crypto_validation_probe_is_dynamic_resumable_and_non_mutating(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "catalog.json"
    report_path = tmp_path / "crypto-validation.json"
    active_path = tmp_path / "active.json"
    persist_etoro_instrument_catalog_snapshot(
        {
            "instrumentDisplayDatas": [
                {"instrumentID": 123, "symbolFull": "BTC", "instrumentTypeID": 10},
                {"instrumentID": 999, "symbolFull": "BAD", "instrumentTypeID": 10},
                {"instrumentID": 456, "symbolFull": "ETH", "instrumentTypeID": 10},
                {"instrumentID": 777, "symbolFull": "AAPL", "instrumentTypeID": 5},
            ]
        },
        retrieved_at=NOW,
        path=snapshot_path,
    )
    sentinel = {"active_records": [{"symbol": "UNCHANGED"}]}
    active_path.write_text(json.dumps(sentinel), encoding="utf-8")

    report = build_etoro_crypto_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, CryptoValidationClient()),
        cache=HistoricalDataCache(tmp_path / "bars.sqlite3"),
        snapshot_path=snapshot_path,
        report_path=report_path,
        clock=lambda: NOW,
    )

    assert report["total_crypto_discovered"] == 3
    assert report["total_checked"] == 3
    assert report["total_reachable"] == 2
    assert report["active_scanner_ready"] == 2
    assert [row["symbol"] for row in report["newly_eligible"]] == ["BTC", "ETH"]
    assert json.loads(active_path.read_text(encoding="utf-8")) == sentinel
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(persisted["records"]) == 3
    assert report["broker_write_calls"] == 0
    assert report["real_execution_available"] is False


def test_crypto_activation_preserves_existing_equity_and_rejects_snapshot_mismatch(tmp_path):
    from app.data.runtime import activate_validated_etoro_crypto, _write_etoro_native_active_artifact
    snapshot_path, report_path, active_path = (tmp_path / name for name in ("catalog.json", "crypto.json", "active.json"))
    persist_etoro_instrument_catalog_snapshot({"instrumentDisplayDatas": [
        {"instrumentID": 123, "symbolFull": "BTC", "instrumentTypeID": 10},
        {"instrumentID": 777, "symbolFull": "AAPL", "instrumentTypeID": 5},
    ]}, retrieved_at=NOW, path=snapshot_path)
    snapshot = read_etoro_instrument_catalog_snapshot(snapshot_path)
    _write_etoro_native_active_artifact(snapshot=snapshot, progress_records=[{
        "instrument_id": "777", "symbol": "AAPL", "display_name": "Apple", "asset_class": "EQUITY", "status": "BOOTSTRAPPED",
    }], artifact_path=active_path, created_at=NOW)
    build_etoro_crypto_validation_report(ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, CryptoValidationClient()), cache=HistoricalDataCache(tmp_path / "cache.sqlite3"),
        snapshot_path=snapshot_path, report_path=report_path, clock=lambda: NOW)
    result = activate_validated_etoro_crypto(report_path=report_path, snapshot_path=snapshot_path, artifact_path=active_path)
    assert result["runtime_universe_count"] == 2
    assert result["activated_crypto"] == ["BTC"]
    assert {r["symbol"] for r in read_etoro_dynamic_universe_artifact(active_path)["active_records"]} == {"BTC", "AAPL"}
    before = active_path.read_bytes()
    report = json.loads(report_path.read_text())
    report["source_snapshot_id"] = "wrong"
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="snapshot mismatch"):
        activate_validated_etoro_crypto(report_path=report_path, snapshot_path=snapshot_path, artifact_path=active_path)
    assert active_path.read_bytes() == before


def test_local_dynamic_artifact_retains_catalog_and_only_ready_records() -> None:
    snapshot = read_etoro_instrument_catalog_snapshot()
    artifact = read_etoro_dynamic_universe_artifact()

    assert snapshot is not None
    assert artifact is not None
    assert artifact["source_snapshot_id"] == snapshot["snapshot_id"]
    assert artifact["catalog_instrument_count"] == snapshot["unique_instrument_id_count"]
    records = artifact["records"]
    active_records = artifact["active_records"]
    assert isinstance(records, list)
    assert isinstance(active_records, list)
    assert len(records) == len({row["instrument_id"] for row in records})
    assert len(active_records) == artifact["market_data_ready_count"]
    assert all(item["universe_state"] == "ACTIVE_SCANNER_READY" for item in active_records)


def test_dynamic_loader_does_not_fallback_to_the_34_asset_baseline() -> None:
    instruments = load_etoro_dynamic_active_scanner_instruments()

    artifact = read_etoro_dynamic_universe_artifact()
    assert len(instruments) == artifact["market_data_ready_count"] > 34
    assert {item.broker_instrument_id for item in instruments} == {
        str(row["provider_instrument_id"]) for row in artifact["active_records"]
    }
    assert all("etoro-native-bootstrap" in instrument.tags for instrument in instruments)
