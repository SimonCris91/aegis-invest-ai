import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from app.brokers.etoro.client import EtoroReadClient
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
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, int]] = []

    def candle_history(
        self, *, instrument_id: int, direction: str, interval: str, candles_count: int
    ) -> object:
        self.calls.append((instrument_id, interval, candles_count))
        return {
            "candles": [
                {
                    "candles": [
                        {
                            "fromDate": "2026-08-30T10:00:00Z",
                            "open": "100",
                            "high": "101",
                            "low": "99",
                            "close": "100.5",
                            "volume": "10",
                        }
                    ]
                }
            ]
        }


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
    assert snapshot["retrieved_at"]
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
    assert artifact["catalog_instrument_count"] == 16152
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
