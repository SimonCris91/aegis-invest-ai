"""Deterministic tests for the local read-only Home contract."""

from datetime import UTC, datetime
from decimal import Decimal
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

from app.web.home import home_snapshot_from_scan_cycle
from app.web.server import AegisHomeHandler


def _report(*, top: int = 0, watchlist: tuple[dict[str, object], ...] = ()) -> dict[str, object]:
    return {
        "status": "READ_ONLY_ACTIVE_SCAN_CYCLE_READY",
        "scan_cycle_timestamp": datetime(2026, 8, 30, tzinfo=UTC).isoformat(),
        "assets_requested": 34,
        "assets_comparable": 34,
        "top_opportunities": tuple({"symbol": "AAA"} for _ in range(top)),
        "watchlist": watchlist,
        "no_trade": tuple({"symbol": "AAA"} for _ in range(26)),
        "positions_to_manage": (),
        "scanner_output": {"simulated_capital": "200"},
    }


def test_home_snapshot_maps_successful_zero_opportunity_state() -> None:
    snapshot = home_snapshot_from_scan_cycle(
        _report(
            watchlist=(
                {
                    "symbol": "BTC",
                    "full_asset_name": "Bitcoin",
                    "opportunity_score": "68.95",
                    "rank": 1,
                },
            )
        )
    )

    assert snapshot.capital.amount == Decimal("200")
    assert snapshot.scanner.top_opportunities == 0
    assert snapshot.scanner.assets_scanned == 34
    assert snapshot.scanner.assets_comparable == 34
    assert snapshot.scanner.watchlist_count == 1
    assert snapshot.positions.open_count == 0
    assert snapshot.safety.execution_mode == "READ_ONLY"
    assert snapshot.safety.broker_write_calls == 0
    assert snapshot.data_health.one_hour_status == "READY"


def test_home_snapshot_preserves_empty_watchlist_and_positions() -> None:
    snapshot = home_snapshot_from_scan_cycle(_report())

    assert snapshot.watchlist == ()
    assert snapshot.positions.items == ()
    assert snapshot.positions.open_count == 0


def test_home_snapshot_does_not_turn_missing_scanner_fields_into_zero() -> None:
    report = _report()
    report.pop("assets_comparable")
    report["status"] = "BLOCKED"

    snapshot = home_snapshot_from_scan_cycle(report)

    assert snapshot.scanner.assets_comparable is None
    assert snapshot.data_health.one_hour_status == "DEGRADED"
    assert snapshot.data_health.backend_status == "DEGRADED"


def test_local_server_serves_home_and_rejects_mutations() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), AegisHomeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/")
        get_response = connection.getresponse()
        get_body = get_response.read()
        assert get_response.status == 200
        assert b"Aegis Invest AI" in get_body

        connection.request("POST", "/api/home")
        post_response = connection.getresponse()
        post_body = post_response.read()
        assert post_response.status == 405
        assert b"READ_ONLY_METHOD_NOT_ALLOWED" in post_body
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
