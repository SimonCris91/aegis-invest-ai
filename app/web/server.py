"""Dependency-free local HTTP server for the read-only Aegis Home."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.brokers.etoro.client import EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.runtime import runtime_credentials
from app.config.loader import load_config, load_runtime_values
from app.domain.enums import EtoroTransportMode
from app.orchestration.active_intelligence import DEFAULT_ACTIVE_INTELLIGENCE_STORE_PATH
from app.orchestration.active_runtime import read_etoro_demo_runtime_status
from app.storage.sqlite import SqliteRecordStore
from app.web.home import home_snapshot_from_scan_cycle, live_system_status_from_runtime
from app.web.manual_demo import ManualOrderError, ManualReadClient, manual_orders

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
LOCAL_SESSION_TOKEN = secrets.token_urlsafe(32)

TOP_OPPORTUNITY_FIELDS = (
    "symbol",
    "instrument_id",
    "rank",
    "score",
    "opportunity_score",
    "confidence",
    "action",
    "classification",
    "timeframe",
    "observed_at",
    "timestamp",
    "rationale",
)


def _allowlisted_top_opportunity(
    row: dict[str, object], *, rank: int, observed_at: object
) -> dict[str, object]:
    """Serialize only the stable, sanitized fields needed by the dashboard."""
    projected = {
        key: row[key] for key in TOP_OPPORTUNITY_FIELDS if key in row and row[key] is not None
    }
    projected.setdefault("rank", rank)
    projected.setdefault("observed_at", observed_at)
    return projected


def _read_runtime_activity(
    path: Path = Path("work") / "etoro-demo-runtime.sqlite3",
) -> dict[str, object] | None:
    """Read committed runtime events without creating or mutating the store."""
    if not path.exists():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT payload FROM operational_records "
            "WHERE kind='etoro-demo-runtime-status' ORDER BY id DESC LIMIT 64"
        ).fetchall()
        exit_row = connection.execute(
            "SELECT payload FROM operational_records "
            "WHERE kind='etoro-demo-exit-management' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    if not rows:
        return None
    history = [json.loads(str(row[0])) for row in rows]
    latest = history[0]
    if not isinstance(latest, dict):
        return None

    latest_exit_management: dict[str, object] | None = None
    if exit_row:
        try:
            parsed_exit = json.loads(str(exit_row[0]))
        except (TypeError, json.JSONDecodeError):
            parsed_exit = None
        if isinstance(parsed_exit, dict):
            latest_exit_management = parsed_exit

    # Each poll can persist a pre- and post-cycle status. Collapse those into
    # one observable row per poll without manufacturing execution outcomes.
    per_poll: dict[int, dict[str, object]] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        poll_count = item.get("poll_count")
        cycle_state = item.get("cycle_state")
        # Poll numbers reset on restart. Do not mix the previous run's larger
        # counters into this run's activity history.
        if isinstance(poll_count, int) and isinstance(latest.get("poll_count"), int):
            if poll_count > latest["poll_count"]:
                break
        if isinstance(poll_count, int) and cycle_state is not None:
            per_poll.setdefault(poll_count, item)
            if len(per_poll) == 12:
                break
    cycle_rows = [
        {
            "poll_count": poll_count,
            "observed_at": item.get("observed_at"),
            "cycle_state": item.get("cycle_state"),
            "top_opportunity_count": item.get("top_opportunity_count"),
        }
        for poll_count, item in sorted(per_poll.items())
    ]

    def persisted_number(name: str) -> object | None:
        return latest.get(name) if name in latest else None

    one_shot = _read_latest_one_shot_report()
    return {
        "runner_state": latest.get("runner_state"),
        "last_completed_cycle_time": latest.get("last_successful_scan_at"),
        "cycles_completed_since_startup": persisted_number("accepted_cycle_count"),
        "top_opportunities_per_cycle": cycle_rows[-12:],
        "demo_submissions_attempted": persisted_number("demo_submissions_attempted"),
        "demo_submissions_filled": persisted_number("demo_submissions_filled"),
        "demo_submissions_rejected": persisted_number("demo_submissions_rejected"),
        "demo_submissions_blocked": persisted_number("demo_submissions_blocked"),
        "latest_risk_decision": persisted_number("latest_risk_decision"),
        "latest_risk_reason": persisted_number("latest_risk_reason"),
        "authorized_capital_eur": latest.get("authorized_capital_eur"),
        "managed_exposure_eur": latest.get("managed_exposure_eur"),
        "remaining_capital_eur": latest.get("remaining_authorized_capital_eur"),
        "last_activity_at": latest.get("observed_at"),
        "news_provider": latest.get("news_provider"),
        "news_provider_status": latest.get("news_provider_status"),
        "news_rate_limit_triggered": latest.get("news_rate_limit_triggered"),
        "news_provider_request_count": latest.get("news_provider_request_count"),
        "news_requests_suppressed_after_rate_limit": latest.get(
            "news_requests_suppressed_after_rate_limit"
        ),
        "news_cache_hits": latest.get("news_cache_hits"),
        "news_cache_misses": latest.get("news_cache_misses"),
        "news_provider_diagnostics": latest.get("news_provider_diagnostics", {}),
        "news_event_digest": latest.get("news_event_digest", ()),
        "news_asset_contexts": latest.get("news_asset_contexts", {}),
        "global_risk_context": latest.get("global_risk_context", {}),
        "acquisition_status": latest.get("acquisition_status"),
        "acquisition_instruments_requested": latest.get(
            "acquisition_instruments_requested"
        ),
        "acquisition_instruments_attempted": latest.get(
            "acquisition_instruments_attempted"
        ),
        "acquisition_instruments_not_attempted": latest.get(
            "acquisition_instruments_not_attempted"
        ),
        "acquisition_instruments_in_backoff": latest.get(
            "acquisition_instruments_in_backoff"
        ),
        "acquisition_instruments_updated": latest.get("acquisition_instruments_updated"),
        "acquisition_newest_completed_bar": latest.get(
            "acquisition_newest_completed_bar"
        ),
        "acquisition_outcome_counts": latest.get("acquisition_outcome_counts", {}),
        "coherent_eligible_count": latest.get("coherent_eligible_count"),
        "coherent_coverage_ratio": latest.get("coherent_coverage_ratio"),
        "coherent_coverage_minimum": latest.get("coherent_coverage_minimum"),
        "demo_writes": latest.get("demo_writes"),
        "real_writes": latest.get("real_writes"),
        "activity_code": latest.get("activity_code"),
        "blockers": latest.get("blockers", ()),
        "last_error": latest.get("last_error"),
        "latest_exit_management": latest_exit_management,
        "one_shot": one_shot,
        "data_available": {
            "submission_counters": all(
                name in latest
                for name in (
                    "demo_submissions_attempted",
                    "demo_submissions_filled",
                    "demo_submissions_rejected",
                    "demo_submissions_blocked",
                )
            ),
            "risk_decision": "latest_risk_decision" in latest,
            "cycles_since_startup": "accepted_cycle_count" in latest,
        },
    }


def _read_latest_one_shot_report(
    path: Path = Path("work") / "FIRST-DEMO-ORDER.txt",
) -> dict[str, object] | None:
    """Read the latest sanitized one-shot result without executing anything."""
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
        lines = [line.strip() for line in raw.decode(encoding).splitlines()]
        payload = json.loads(next(line for line in reversed(lines) if line))
    except (OSError, StopIteration, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    live_selection = payload.get("live_selection")
    selection = live_selection if isinstance(live_selection, dict) else {}
    return {
        "observed_at": payload.get("observed_at"),
        "candidates_checked": payload.get("candidates_checked"),
        "open_tradable_checked": payload.get("open_tradable_checked"),
        "demo_submission_attempts": payload.get("demo_submission_attempts"),
        "demo_writes": payload.get("demo_write_performed"),
        "real_writes": payload.get("real_write_performed"),
        "status": payload.get("status"),
        "news_provider": payload.get("news_provider"),
        "news_provider_status": payload.get("news_provider_status"),
        "news_rate_limit_triggered": payload.get("news_rate_limit_triggered"),
        "news_provider_request_count": payload.get("news_provider_request_count"),
        "news_requests_suppressed_after_rate_limit": payload.get(
            "news_requests_suppressed_after_rate_limit"
        ),
        "news_cache_hits": payload.get("news_cache_hits"),
        "news_cache_misses": payload.get("news_cache_misses"),
        "news_provider_diagnostics": payload.get("news_provider_diagnostics", {}),
        "catalog_progress": selection.get("local_prefilter_count"),
    }


def _persisted_home_report() -> dict[str, object]:
    """Read the last accepted cycle; this function never evaluates a scan."""
    cycle = SqliteRecordStore.read_latest_read_only(
        DEFAULT_ACTIVE_INTELLIGENCE_STORE_PATH,
        "active-intelligence-cycle",
    )
    if cycle is None:
        raise RuntimeError("no accepted active-intelligence cycle is persisted")
    scanner = cycle.get("scanner_result")
    scanner_payload = scanner if isinstance(scanner, dict) else {}
    candidates = scanner_payload.get("candidates", ())
    candidate_rows = tuple(row for row in candidates if isinstance(row, dict))
    grouped = {
        bucket: tuple(row for row in candidate_rows if row.get("bucket") == bucket)
        for bucket in ("TOP_OPPORTUNITIES", "WATCHLIST", "NO_TRADE", "REJECTED")
    }
    top_rows = sorted(
        grouped["TOP_OPPORTUNITIES"],
        key=lambda row: (
            -float(row.get("opportunity_score", 0)),
            str(row.get("symbol", "")),
        ),
    )
    top_items = tuple(
        _allowlisted_top_opportunity(
            row,
            rank=index,
            observed_at=cycle.get("scan_cycle_timestamp"),
        )
        for index, row in enumerate(top_rows, start=1)
    )
    symbols_evaluated = cycle.get("symbols_evaluated")
    symbol_count = len(symbols_evaluated) if isinstance(symbols_evaluated, (list, tuple)) else 0
    data_health = str(cycle.get("data_health_state", "UNKNOWN"))
    return {
        "status": "READ_ONLY_ACTIVE_SCAN_CYCLE_READY",
        "execution_mode": "READ_ONLY",
        "capital_currency": "EUR",
        "capital_mode": "SIMULATED",
        "data_readiness": "READY" if data_health == "HEALTHY" else "DEGRADED",
        "scanner_cycle_status": "COMPLETE",
        "scan_cycle_timestamp": cycle.get("scan_cycle_timestamp"),
        "assets_requested": symbol_count,
        "assets_comparable": len(candidate_rows),
        "top_opportunities": grouped["TOP_OPPORTUNITIES"],
        "top_opportunity_items": top_items,
        "last_scan_top_opportunities": top_items,
        "watchlist": grouped["WATCHLIST"],
        "no_trade": grouped["NO_TRADE"],
        "rejected": grouped["REJECTED"],
        "scanner_output": {
            "simulated_capital": str(cycle.get("shadow_capital", "")),
        },
        "news_provider": cycle.get("news_provider"),
        "news_provider_status": cycle.get("news_provider_status"),
        "news_provider_diagnostics": cycle.get("news_provider_diagnostics", {}),
        "news_event_digest": cycle.get("news_event_digest", ()),
        "news_asset_contexts": cycle.get("news_asset_contexts", {}),
        "global_risk_context": cycle.get("global_risk_context", {}),
        "broker_write_calls": cycle.get("broker_write_calls"),
        "real_execution_available": cycle.get("real_execution_available", False),
        "demo_execution_enabled": cycle.get("demo_execution_enabled", False),
    }


def _live_etoro_demo_snapshot() -> dict[str, object]:
    """Read a sanitized Demo portfolio directly from eToro; never writes."""
    values = load_runtime_values()
    credentials = runtime_credentials(values)
    config = load_config(values)
    if credentials is None or not config.etoro_api_enabled:
        return {"status": "NOT_CONFIGURED", "source": "ETORO_LIVE_READ_ONLY"}
    client = EtoroReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(EtoroTransportMode.DIRECT)),
    )
    # The legacy portfolio payload is still needed for position IDs used by
    # the manual close flow.  Account totals must come from the aggregate
    # endpoint: the legacy payload's ``credit`` is cash, not account equity.
    identity = client.identity()
    aggregate = client.demo_account(identity)
    raw = client.demo_portfolio_payload()
    if not isinstance(raw, dict) or not isinstance(raw.get("clientPortfolio"), dict):
        raise ValueError("invalid Demo portfolio payload")
    data = raw["clientPortfolio"]
    positions = []
    for item in data.get("positions", []):
        if not isinstance(item, dict):
            continue
        positions.append(
            {
                "position_id": str(item.get("positionID", "")),
                "instrument_id": item.get("instrumentID"),
                "units": str(item.get("units", "")),
                "average_entry_price": str(item.get("openRate", "")),
                "direction": "LONG" if item.get("isBuy") else "SHORT",
            }
        )
    cash = str(aggregate.cash)
    return {
        "status": "LIVE_READ_ONLY",
        "source": "ETORO_API",
        "as_of": aggregate.as_of.isoformat(),
        "currency": aggregate.currency.value,
        "total_value": str(aggregate.total_value),
        "cash": cash,
        "current_pnl": str(aggregate.current_pnl),
        "account_balance": str(aggregate.account_balance),
        "positions": positions,
        "broker_write_calls": 0,
    }


def _live_etoro_instruments(*, offset: int = 0) -> dict[str, object]:
    """Return a bounded, sanitized list of currently open Demo instruments."""
    from app.brokers.etoro.live_candidates import current_catalog_candidates
    values = load_runtime_values()
    credentials = runtime_credentials(values)
    config = load_config(values)
    if credentials is None or not config.etoro_api_enabled:
        return {"status": "NOT_CONFIGURED", "source": "ETORO_LIVE_READ_ONLY", "instruments": []}
    client = ManualReadClient(credentials, DisciplinedHttpClient(UrllibTransport(EtoroTransportMode.DIRECT)))
    diagnostics: dict[str, object] = {}
    candidates = current_catalog_candidates(client, selection_diagnostics=diagnostics, catalog_offset=offset, live_get_cap=10, pacing_delay_seconds=1.0)
    instrument_rows = []
    for item in candidates:
        quote_data = {"bid": None, "ask": None, "last_price": None, "price_as_of": None}
        try:
            quote, _, _ = client.quote_with_diagnostics(int(item.broker_instrument_id), item.symbol)
            quote_data = {"bid": str(quote.bid) if quote.bid is not None else None, "ask": str(quote.ask) if quote.ask is not None else None, "last_price": str(quote.price), "price_as_of": quote.as_of.isoformat()}
        except Exception:
            pass
        instrument_rows.append({"instrument_id": item.broker_instrument_id, "symbol": item.symbol, "name": item.display_name or item.symbol, "asset_class": item.asset_class.value, "buy_allowed": item.buy_allowed, "sell_allowed": item.sell_allowed, **quote_data})
    return {
        "status": "LIVE_READ_ONLY",
        "source": "ETORO_API",
        "observed_at": datetime.now(UTC).isoformat(),
        "instruments": instrument_rows,
        "checked": diagnostics.get("live_get_count", 0),
        "offset": offset,
        "has_more": bool(diagnostics.get("unchecked_due_to_cap_count", 0)),
        "broker_write_calls": 0,
    }


class AegisHomeHandler(BaseHTTPRequestHandler):
    """Only GET is implemented; all mutation methods are rejected."""

    server_version = "AegisReadOnly/1.0"

    def _dashboard_write_authorized(self) -> bool:
        # The remote tunnel is deliberately read-only.  A bearer token must
        # never turn a public tunnel into an order gateway.
        if self._forwarded_remote():
            return False
        configured = load_runtime_values().get("AEGIS_DASHBOARD_ACCESS_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not supplied.startswith("Bearer "):
            return False
        token = supplied[7:].strip()
        return ((len(configured) >= 32 and hmac.compare_digest(token, configured))
                or (self._session_origin_allowed() and hmac.compare_digest(token, LOCAL_SESSION_TOKEN)))

    def _same_origin(self) -> bool:
        """Require browser same-origin semantics for the temporary session token."""
        origin = self.headers.get("Origin", "").rstrip("/")
        host = self.headers.get("Host", "")
        return bool(host) and origin in {f"http://{host}", f"https://{host}"}

    def _session_origin_allowed(self) -> bool:
        """Allow local or Cloudflare-forwarded same-origin browser sessions only."""
        return self._direct_local() or (self._forwarded_remote() and self._same_origin())

    def _forwarded_remote(self) -> bool:
        forwarded = any(
            self.headers.get(h)
            for h in ("CF-Connecting-IP", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto")
        )
        return forwarded and self.client_address[0] not in {"127.0.0.1", "::1"}

    def _direct_local(self) -> bool:
        port = self.server.server_port
        peer = self.client_address[0]
        try:
            peer_is_local_or_private = ipaddress.ip_address(peer).is_private
        except ValueError:
            peer_is_local_or_private = False
        bound_host = str(self.server.server_address[0])
        host_header = self.headers.get("Host", "")
        accepted_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"{bound_host}:{port}"}
        lan_host = host_header.rsplit(":", 1)[-1] == str(port)
        return (peer_is_local_or_private
                and (host_header in accepted_hosts or (bound_host == "0.0.0.0" and lan_host))
                and not any(self.headers.get(h) for h in ("CF-Connecting-IP", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto")))

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/api/etoro/demo":
            try:
                self._send_json(200, _live_etoro_demo_snapshot())
            except Exception:
                self._send_json(503, {"status": "LIVE_READ_UNAVAILABLE", "source": "ETORO_API"})
            return
        if path == "/api/etoro/demo/orders":
            try:
                self._send_json(200, manual_orders.recent())
            except Exception:
                self._send_json(503, {"status": "LOCAL_ORDER_LEDGER_UNAVAILABLE", "orders": []})
            return
        if path == "/api/etoro/instruments":
            try:
                raw_offset = parse_qs(parsed_url.query).get("offset", ["0"])[0]
                offset = max(0, int(raw_offset))
                self._send_json(200, _live_etoro_instruments(offset=offset))
            except Exception:
                self._send_json(503, {"status": "LIVE_READ_UNAVAILABLE", "source": "ETORO_API", "instruments": []})
            return
        if path == "/api/home":
            self._send_home()
            return
        static_files = {
            "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/index.html": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
            "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
        }
        if path in static_files:
            file_path, content_type = static_files[path]
            self._send_file(file_path, content_type)
            return
        self._send_json(404, {"status": "ERROR", "error": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/manual/session":
            if (self._direct_local()
                    and self._same_origin()
                    and self.headers.get("X-Aegis-Request") == "manual"):
                self._send_json(200, {"token": LOCAL_SESSION_TOKEN})
            else:
                self._send_json(401, {"message": "Apri l'app dalla rete locale del PC per autorizzare gli ordini Demo."})
            return
        if path in {"/api/etoro/demo/order", "/api/etoro/demo/preview", "/api/etoro/demo/order-status"}:
            if not self._dashboard_write_authorized():
                self._send_json(401, {"status": "DASHBOARD_AUTH_REQUIRED", "message": "Accesso ordini richiesto. Apri l'app dalla rete locale del PC.", "broker_write_calls": 0})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096 or self.headers.get_content_type() != "application/json":
                    raise ManualOrderError("Richiesta JSON non valida.")
                data = json.loads(self.rfile.read(size))
                if path.endswith("/preview"):
                    result = manual_orders.preview(data)
                elif path.endswith("/order-status"):
                    result = manual_orders.status(data)
                else:
                    result = manual_orders.submit(data)
                self._send_json(200, result)
            except (ManualOrderError, ValueError) as exc:
                self._send_json(400, {"status": "BLOCKED", "message": str(exc) if isinstance(exc, ManualOrderError) else "Richiesta non valida."})
            except Exception:
                self._send_json(503, {"status": "UNAVAILABLE", "message": "Verifica eToro non completata. Se hai confermato, controlla l'esito prima di ripetere."})
            return
        self._send_json(405, {"status": "ERROR", "error": "READ_ONLY_METHOD_NOT_ALLOWED"})

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(405, {"status": "METHOD_NOT_ALLOWED"})

    def do_PATCH(self) -> None:  # noqa: N802
        self._send_json(405, {"status": "METHOD_NOT_ALLOWED"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_json(405, {"status": "METHOD_NOT_ALLOWED"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_home(self) -> None:
        try:
            report = _persisted_home_report()
            runtime_status = read_etoro_demo_runtime_status()
            snapshot = home_snapshot_from_scan_cycle(
                report,
                live_system_status=live_system_status_from_runtime(
                    runtime_status,
                    overnight_activity=_read_runtime_activity(),
                ),
            )
            self._send_json(200, snapshot.model_dump(mode="json"))
        except Exception:
            self._send_json(
                503,
                {
                    "status": "ERROR",
                    "error": "HOME_SNAPSHOT_UNAVAILABLE",
                    "message": "Read-only scanner state is temporarily unavailable.",
                },
            )

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"status": "ERROR", "error": "STATIC_ASSET_NOT_FOUND"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(*, host: str = "0.0.0.0", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), AegisHomeHandler)
    print(f"Aegis Home: http://{host}:{port}")
    print("Manual Demo: authenticated user confirmation required; Real unavailable")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local Aegis dashboard server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
