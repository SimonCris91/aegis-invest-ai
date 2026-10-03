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
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.runtime import runtime_credentials
from app.benchmarks.etoro_profiles import read_etoro_benchmarks
from app.config.loader import load_config, load_runtime_values
from app.data.runtime import (
    DEFAULT_ETORO_ACTIVE_UNIVERSE_PATH,
    DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH,
    _catalog_asset_class,
    _catalog_value,
    read_etoro_dynamic_universe_artifact,
    read_etoro_instrument_catalog_snapshot,
)
from app.domain.enums import Currency
from app.performance.demo_review import demo_performance_review
from app.news.relay import relay_key, relay_store_path, store_envelope
from app.orchestration.active_intelligence import DEFAULT_ACTIVE_INTELLIGENCE_STORE_PATH
from app.orchestration.active_runtime import (
    MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY,
    MAX_AEGIS_MANAGED_EXPOSURE_EUR,
    read_etoro_demo_runtime_status,
)
from app.storage.sqlite import SqliteRecordStore
from app.web.home import home_snapshot_from_scan_cycle, live_system_status_from_runtime
from app.web.manual_demo import ManualOrderError, ManualReadClient, manual_orders

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
LOCAL_SESSION_TOKEN = secrets.token_urlsafe(32)

TOP_OPPORTUNITY_FIELDS = (
    "symbol",
    "instrument_id",
    "full_asset_name",
    "full_name",
    "display_name",
    "description",
    "asset_class",
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

_CATALOG_LABEL_CACHE: tuple[int, dict[str, dict[str, object]]] | None = None


def _read_managed_demo_exposure_read_only(
    path: Path = Path("work") / "etoro-demo-runtime.sqlite3",
    currency: Currency | None = None,
) -> Decimal | None:
    """Calculate Aegis-owned net exposure from its ledger without opening writes."""
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT state, payload FROM demo_submissions "
                "WHERE state IN ('FILLED','PARTIALLY_FILLED','RESERVED','SUBMITTED','PENDING','UNKNOWN')"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None

    open_by_instrument: dict[str, Decimal] = {}
    reserved = Decimal("0")
    selected_currency = currency
    try:
        for raw_state, raw_payload in rows:
            payload = json.loads(raw_payload)
            payload_currency = Currency(str(payload.get("account_currency", "")))
            if selected_currency is None:
                selected_currency = payload_currency
            if payload_currency is not selected_currency:
                return None
            raw_amount = (
                payload.get("amount_account_currency")
                if selected_currency is Currency.USD
                else payload.get("amount_account_currency", payload.get("amount_eur"))
            )
            if raw_amount is None:
                return None
            amount = Decimal(str(raw_amount))
            if amount < 0:
                return None
            state = str(raw_state).upper()
            if state in {"RESERVED", "SUBMITTED", "PENDING", "UNKNOWN"}:
                reserved += amount
                continue
            if payload.get("exit_status") == "CLOSED":
                continue
            instrument = str(payload["instrument_id"])
            action = str(payload.get("action", "OPEN")).upper()
            if action in {"OPEN", "INCREASE"}:
                if selected_currency is Currency.USD:
                    raw_remaining = payload.get(
                        "remaining_exposure_account_currency",
                        payload.get("executed_exposure_account_currency", amount),
                    )
                else:
                    raw_remaining = payload.get("remaining_exposure_eur", amount)
                amount = Decimal(str(raw_remaining))
                open_by_instrument[instrument] = open_by_instrument.get(
                    instrument, Decimal("0")
                ) + amount
            elif action in {"REDUCE", "CLOSE"}:
                open_by_instrument[instrument] = open_by_instrument.get(
                    instrument, Decimal("0")
                ) - amount
            else:
                return None
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return None
    if any(value < 0 for value in open_by_instrument.values()):
        return None
    return sum(open_by_instrument.values(), Decimal("0")) + reserved


def _read_unverified_demo_ledger_summary(
    path: Path = Path("work") / "etoro-demo-runtime.sqlite3",
) -> dict[str, object]:
    """Summarize legacy order notionals without guessing their currency or exposure."""
    result: dict[str, object] = {
        "count": None,
        "nominal_total": None,
        "currency": "UNKNOWN",
    }
    if not path.exists():
        return result
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT payload FROM demo_submissions "
                "WHERE state IN ('FILLED','PARTIALLY_FILLED')"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return result
    total = Decimal("0")
    count = 0
    currencies: set[str] = set()
    try:
        for (raw_payload,) in rows:
            payload = json.loads(raw_payload)
            payload_currency = str(payload.get("account_currency", "UNKNOWN"))
            currencies.add(payload_currency)
            if (
                payload_currency in {Currency.EUR.value, Currency.USD.value}
                and str(payload.get("broker_order_status", "")).upper() == "FILLED"
                and payload.get("broker_position_id")
                and payload.get("broker_reconciliation_source")
                and payload.get("executed_exposure_account_currency") is not None
            ):
                continue
            raw_amount = payload.get("amount_account_currency")
            if raw_amount is None:
                raw_amount = payload.get("amount_eur")
            if raw_amount is None:
                return result
            amount = Decimal(str(raw_amount))
            if amount < 0:
                return result
            total += amount
            count += 1
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return result
    currency_name = next(iter(currencies)) if len(currencies) == 1 else "UNKNOWN"
    result.update(
        {"count": count, "nominal_total": str(total), "currency": currency_name}
    )
    return result


def _catalog_labels() -> dict[str, dict[str, object]]:
    """Read human-readable instrument labels from the verified local catalog."""
    global _CATALOG_LABEL_CACHE
    path = DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    if _CATALOG_LABEL_CACHE is not None and _CATALOG_LABEL_CACHE[0] == modified_ns:
        return _CATALOG_LABEL_CACHE[1]

    try:
        snapshot = read_etoro_instrument_catalog_snapshot(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    raw_rows = snapshot.get("instrument_display_datas", []) if snapshot else []
    labels: dict[str, dict[str, object]] = {}
    if isinstance(raw_rows, list):
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbolFull")
            display_name = row.get("instrumentDisplayName")
            if not isinstance(symbol, str) or not symbol.strip():
                continue
            label = str(display_name).strip() if display_name is not None else ""
            if not label:
                label = symbol.strip()
            labels[symbol.casefold()] = {
                "symbol": symbol.strip(),
                "full_asset_name": label,
                "full_name": label,
                "display_name": label,
                "description": label,
                "instrument_id": row.get("instrumentID"),
                "asset_class": _catalog_asset_class(row),
            }
    _CATALOG_LABEL_CACHE = (modified_ns, labels)
    return labels


def _catalog_labels_by_id() -> dict[str, dict[str, object]]:
    return {
        str(metadata["instrument_id"]): metadata
        for metadata in _catalog_labels().values()
        if metadata.get("instrument_id") is not None
    }


def _search_etoro_catalog(
    query: str, *, limit: int = 30, offset: int = 0
) -> dict[str, object]:
    """Search the full local eToro display catalog without contacting the broker."""
    snapshot = read_etoro_instrument_catalog_snapshot(DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH)
    if snapshot is None:
        raise FileNotFoundError("ETORO_CATALOG_SNAPSHOT_MISSING")
    records = snapshot.get("instrument_display_datas")
    if not isinstance(records, list):
        raise ValueError("ETORO_CATALOG_SNAPSHOT_INVALID")

    normalized_query = query.strip().casefold()
    active_artifact = read_etoro_dynamic_universe_artifact(
        DEFAULT_ETORO_ACTIVE_UNIVERSE_PATH
    )
    artifact_matches_snapshot = bool(
        active_artifact
        and active_artifact.get("source_snapshot_id") == snapshot.get("snapshot_id")
    )
    bootstrap_by_id: dict[str, dict[str, object]] = {}
    if artifact_matches_snapshot:
        artifact_records = active_artifact.get("records")
        if isinstance(artifact_records, list):
            bootstrap_by_id = {
                str(row.get("instrument_id")): row
                for row in artifact_records
                if isinstance(row, dict) and row.get("instrument_id") is not None
            }

    matches: list[tuple[int, str, str, dict[str, object]]] = []
    unique_ids: set[str] = set()
    for row in records:
        if not isinstance(row, dict):
            continue
        instrument_id = _catalog_value(row, "instrumentID", "instrumentId")
        if instrument_id:
            unique_ids.add(instrument_id)
        symbol = _catalog_value(row, "symbolFull", "internalSymbolFull").strip()
        name = _catalog_value(
            row, "instrumentDisplayName", "displayname", "displayName"
        ).strip() or symbol
        isin = _catalog_value(row, "isin", "ISIN")
        fields = (symbol, name, instrument_id, isin)
        if not normalized_query or not any(
            normalized_query in field.casefold() for field in fields if field
        ):
            continue

        folded_symbol = symbol.casefold()
        folded_name = name.casefold()
        relevance = (
            0 if folded_symbol == normalized_query or folded_name == normalized_query else
            1 if folded_symbol.startswith(normalized_query) else
            2 if folded_name.startswith(normalized_query) else
            3 if normalized_query in folded_symbol else
            4 if normalized_query in folded_name else 5
        )
        bootstrap = bootstrap_by_id.get(instrument_id, {})
        item = {
            "instrument_id": instrument_id or None,
            "symbol": symbol,
            "name": name or symbol,
            "asset_class": _catalog_asset_class(row),
            "bootstrap_status": bootstrap.get("status", "NOT_VERIFIED"),
            "bootstrap_reason": bootstrap.get("reason"),
        }
        matches.append((relevance, folded_symbol, instrument_id, item))

    matches.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    selected = [entry[3] for entry in matches[offset : offset + limit]]
    return {
        "status": "OK",
        "query": query.strip(),
        "catalog_count": snapshot.get("unique_instrument_id_count", len(unique_ids)),
        "catalog_as_of": snapshot.get("retrieved_at"),
        "readiness_as_of": (
            active_artifact.get("created_at")
            if artifact_matches_snapshot and active_artifact
            else None
        ),
        "matched_count": len(matches),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(selected) < len(matches),
        "results": selected,
    }


def _overlay_local_etoro_universe_metrics(
    runtime: dict[str, object] | None,
) -> dict[str, object] | None:
    """Prefer current catalog/bootstrap artifacts over the runner's last-cycle copy."""
    if runtime is None:
        return None
    snapshot = read_etoro_instrument_catalog_snapshot(DEFAULT_ETORO_CATALOG_SNAPSHOT_PATH)
    if snapshot is None:
        return dict(runtime)
    updated = dict(runtime)
    updated["catalog_instrument_count"] = snapshot.get("unique_instrument_id_count")
    updated["catalog_snapshot_retrieved_at"] = snapshot.get("retrieved_at")

    artifact = read_etoro_dynamic_universe_artifact(DEFAULT_ETORO_ACTIVE_UNIVERSE_PATH)
    if not artifact or artifact.get("source_snapshot_id") != snapshot.get("snapshot_id"):
        for key in (
            "catalog_verified_mapping_count",
            "catalog_market_data_ready_count",
            "catalog_blocked_count",
            "catalog_pending_count",
            "active_scanner_universe_count",
        ):
            updated.pop(key, None)
        updated["bootstrap_snapshot_created_at"] = None
        return updated

    updated.update(
        {
            "catalog_verified_mapping_count": artifact.get("verified_mapping_count"),
            "catalog_market_data_ready_count": artifact.get("market_data_ready_count"),
            "catalog_blocked_count": artifact.get("catalog_not_ready_count", artifact.get("blocked_count")),
            "catalog_pending_count": artifact.get("catalog_pending_count"),
            "active_scanner_universe_count": artifact.get("active_scanner_universe_count"),
            "bootstrap_snapshot_created_at": artifact.get("created_at"),
        }
    )
    return updated


def _enrich_asset_rows(rows: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    """Fill missing human-readable names without changing strategy decisions."""
    labels = _catalog_labels()
    enriched: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        symbol = item.get("symbol")
        metadata = labels.get(str(symbol).casefold()) if symbol else None
        if metadata:
            for key in ("full_asset_name", "full_name", "display_name", "description"):
                if not item.get(key) or item.get(key) == symbol:
                    item[key] = metadata[key]
            if not item.get("instrument_id"):
                item["instrument_id"] = metadata["instrument_id"]
        enriched.append(item)
    return tuple(enriched)


def _enrich_performance_review_asset_types(
    review: dict[str, object],
) -> dict[str, object]:
    """Resolve legacy history rows without changing their P/L attribution."""
    labels = _catalog_labels()
    labels_by_id = _catalog_labels_by_id()

    def enrich_rows(raw: object) -> list[dict[str, object]]:
        if not isinstance(raw, list):
            return []
        enriched: list[dict[str, object]] = []
        for raw_row in raw:
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            direct = row.get("asset_class")
            if direct:
                row["asset_class_source"] = "LEDGER"
            else:
                metadata = labels_by_id.get(str(row.get("instrument_id")))
                if metadata is None and row.get("symbol"):
                    metadata = labels.get(str(row["symbol"]).casefold())
                resolved = metadata.get("asset_class") if metadata else None
                if resolved:
                    row["asset_class"] = resolved
                    row["asset_class_source"] = "CATALOG_SNAPSHOT"
                else:
                    row["asset_class"] = "UNKNOWN"
                    row["asset_class_source"] = "NOT_VERIFIED"
            enriched.append(row)
        return enriched

    updated = dict(review)
    updated["realized_history"] = enrich_rows(review.get("realized_history"))
    updated["contested_history"] = enrich_rows(review.get("contested_history"))
    updated["order_history"] = enrich_rows(review.get("order_history"))
    return updated


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

    unverified_ledger = _read_unverified_demo_ledger_summary(path)
    try:
        configured = load_config()
        configured_currency = configured.authorized_capital_currency
        configured_capital = configured.authorized_capital
    except (ValueError, TypeError):
        configured_currency = Currency.EUR
        configured_capital = None
    try:
        recorded_currency = (
            latest.get("demo_account_currency")
            or latest.get("authorized_capital_currency")
        )
        if (
            recorded_currency is None
            and unverified_ledger.get("currency") in {Currency.EUR.value, Currency.USD.value}
        ):
            recorded_currency = unverified_ledger["currency"]
        if recorded_currency is None and latest.get("authorized_capital_eur") is not None:
            recorded_currency = Currency.EUR.value
        account_currency = Currency(
            str(
                recorded_currency or configured_currency.value
            )
        )
    except ValueError:
        account_currency = configured_currency
    exposure_limit = MAX_AEGIS_MANAGED_EXPOSURE_BY_CURRENCY.get(account_currency)
    managed_exposure = _read_managed_demo_exposure_read_only(path, account_currency)
    managed_exposure_text = str(managed_exposure) if managed_exposure is not None else None
    remaining_exposure = (
        max(Decimal("0"), exposure_limit - managed_exposure)
        if managed_exposure is not None and exposure_limit is not None
        else None
    )
    authorized_capital = latest.get("authorized_capital")
    if authorized_capital is None:
        authorized_capital = latest.get("authorized_capital_eur")
    if authorized_capital is None and configured_capital is not None:
        authorized_capital = str(configured_capital)
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
        "authorized_capital": authorized_capital,
        "authorized_capital_currency": account_currency.value,
        "authorized_capital_eur": (
            latest.get("authorized_capital_eur")
            if account_currency is Currency.EUR
            else None
        ),
        "managed_exposure": managed_exposure_text,
        "managed_exposure_currency": account_currency.value,
        "managed_exposure_eur": (
            managed_exposure_text if account_currency is Currency.EUR else None
        ),
        "managed_exposure_limit": (
            None if exposure_limit is None else str(exposure_limit)
        ),
        "managed_exposure_limit_eur": (
            str(MAX_AEGIS_MANAGED_EXPOSURE_EUR)
            if account_currency is Currency.EUR
            else None
        ),
        "remaining_capital": (
            str(remaining_exposure) if remaining_exposure is not None else None
        ),
        "remaining_capital_currency": account_currency.value,
        "remaining_capital_eur": (
            str(remaining_exposure)
            if remaining_exposure is not None and account_currency is Currency.EUR
            else None
        ),
        "exposure_cap_status": (
            "EXCEEDED"
            if managed_exposure is not None
            and exposure_limit is not None
            and managed_exposure > exposure_limit
            else "WITHIN_LIMIT"
            if managed_exposure is not None and exposure_limit is not None
            else "UNAVAILABLE"
        ),
        "unverified_legacy_demo_order_count": unverified_ledger["count"],
        "unverified_legacy_demo_order_nominal_total": unverified_ledger["nominal_total"],
        "unverified_legacy_demo_order_currency": unverified_ledger["currency"],
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


def _read_aegis_demo_orders(
    path: Path = Path("work") / "etoro-demo-runtime.sqlite3",
) -> tuple[dict[str, object], ...]:
    """Expose Aegis submissions in the same read-only view as manual orders.

    The runtime registry is deliberately separate from the user-directed
    manual-order ledger. Unknown broker outcomes remain explicitly
    unconfirmed; this projection never promotes an order to FILLED or
    REJECTED.
    """
    if not path.exists():
        return ()
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT idempotency_key, state, payload FROM demo_submissions "
                "ORDER BY rowid DESC"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return ()

    try:
        checked_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except OSError:
        checked_at = None

    orders: list[dict[str, object]] = []
    for idempotency_key, raw_state, raw_payload in rows:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        state = str(raw_state or "UNKNOWN").upper()
        unresolved = state in {"RESERVED", "SUBMITTED", "PENDING", "UNKNOWN"}
        status = "UNCONFIRMED" if unresolved else state
        broker_order_id = payload.get("broker_order_id")
        account_currency = payload.get("account_currency") or "UNKNOWN"
        amount = payload.get("amount_account_currency")
        if amount is None:
            amount = payload.get("amount_eur")
        message = (
            f"Ordine Aegis {broker_order_id or 'senza ID'}: eToro non ha restituito "
            "un esito esplicito. Non ripetere l'ordine; capitale mantenuto bloccato."
            if unresolved
            else (
                f"Stato nel registro Aegis: {status}; valuta ordine "
                f"{account_currency}."
            )
        )
        orders.append(
            {
                "preview_id": f"aegis:{idempotency_key}",
                "source": "AEGIS_RUNTIME",
                "side": "BUY" if str(payload.get("action", "OPEN")).upper() in {"OPEN", "INCREASE"} else "SELL",
                "symbol": payload.get("symbol"),
                "instrument_id": payload.get("instrument_id"),
                "amount": amount,
                "currency": account_currency,
                "units": payload.get("units"),
                "status": status,
                "broker_state": state,
                "broker_order_id": broker_order_id,
                "message": message,
                "created_at": (
                    payload.get("submitted_at")
                    or payload.get("created_at")
                    or payload.get("reserved_at")
                ),
                "timestamp_source": (
                    "BROKER_SUBMITTED_AT"
                    if payload.get("submitted_at")
                    else "AEGIS_RECORD_CREATED_AT"
                    if payload.get("created_at") or payload.get("reserved_at")
                    else "NOT_RECORDED_LEGACY"
                ),
                "last_checked_at": checked_at,
            }
        )
    return tuple(orders)


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
    candidate_rows = _enrich_asset_rows(
        tuple(row for row in candidates if isinstance(row, dict))
    )
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
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
    # The legacy portfolio payload is still needed for position IDs used by
    # the manual close flow.  Account totals must come from the aggregate
    # endpoint: the legacy payload's ``credit`` is cash, not account equity.
    identity = client.identity()
    aggregate = client.demo_account(identity)
    raw = client.demo_portfolio_payload()
    pnl_by_position_id: dict[str, dict[str, object]] = {}
    try:
        pnl_payload = client.demo_pnl_payload()
        pnl_data = pnl_payload.get("clientPortfolio") if isinstance(pnl_payload, dict) else None
        pnl_rows = pnl_data.get("positions") if isinstance(pnl_data, dict) else None
        if isinstance(pnl_rows, list):
            pnl_by_position_id = {
                str(row.get("positionID", row.get("positionId"))): row
                for row in pnl_rows
                if isinstance(row, dict)
                and row.get("positionID", row.get("positionId")) is not None
            }
    except EtoroApiError:
        # The portfolio remains useful if eToro's supplementary P/L view is down.
        pass
    if not isinstance(raw, dict) or not isinstance(raw.get("clientPortfolio"), dict):
        raise ValueError("invalid Demo portfolio payload")
    data = raw["clientPortfolio"]
    labels_by_id = _catalog_labels_by_id()
    positions = [
        _live_etoro_position_row(
            item,
            labels_by_id,
            pnl_item=pnl_by_position_id.get(
                str(item.get("positionID", item.get("positionId", "")))
            ),
        )
        for item in data.get("positions", [])
        if isinstance(item, dict)
    ]
    cash = str(aggregate.cash)
    snapshot = {
        "status": "LIVE_READ_ONLY",
        "source": "ETORO_API",
        "as_of": aggregate.as_of.isoformat(),
        "currency": aggregate.currency.value,
        "total_value": str(aggregate.total_value),
        "cash": cash,
        "current_pnl": str(aggregate.current_pnl),
        "account_balance": str(aggregate.account_balance),
        "positions": positions,
        "open_positions_count": len(positions),
        "broker_write_calls": 0,
    }
    position_pnl_values = []
    for position in positions:
        try:
            value = Decimal(str(position.get("unrealized_pnl")))
        except (InvalidOperation, ValueError):
            value = None
        if value is None or not value.is_finite():
            position_pnl_values = []
            break
        position_pnl_values.append(value)
    if position_pnl_values:
        open_positions_pnl = sum(position_pnl_values, Decimal("0"))
        account_pnl = aggregate.current_pnl
        snapshot["pnl_reconciliation"] = {
            "status": "MATCHED" if abs(open_positions_pnl - account_pnl) <= Decimal("0.01") else "DIFFERENT_SCOPES",
            "account_current_pnl": str(account_pnl),
            "open_positions_pnl": str(open_positions_pnl),
            "difference": str(account_pnl - open_positions_pnl),
            "explanation": "Account aggregate may include realized results, fees or adjustments; open-position total is unrealized only.",
        }
    else:
        snapshot["pnl_reconciliation"] = {"status": "UNAVAILABLE"}
    snapshot["performance_review"] = _enrich_performance_review_asset_types(
        demo_performance_review(
            snapshot,
            excluded_position_ids=manual_orders.aegis_statistics_excluded_position_ids(),
        )
    )
    return snapshot


def _live_etoro_benchmark_snapshot() -> dict[str, object]:
    values = load_runtime_values()
    credentials = runtime_credentials(values)
    config = load_config(values)
    if credentials is None or not config.etoro_api_enabled:
        return read_etoro_benchmarks(None)
    client = EtoroReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
    return read_etoro_benchmarks(client)


def _live_etoro_position_row(
    item: dict[str, object],
    labels_by_id: dict[str, dict[str, object]],
    *,
    pnl_item: dict[str, object] | None = None,
) -> dict[str, object]:
    instrument_id = item.get("instrumentID", item.get("instrumentId"))
    metadata = labels_by_id.get(str(instrument_id), {})
    symbol = str(
        item.get("symbol")
        or item.get("internalSymbolFull")
        or metadata.get("symbol")
        or f"ID {instrument_id}"
    )
    description = str(metadata.get("description") or symbol)
    return {
        "position_id": str(item.get("positionID", item.get("positionId", ""))),
        "instrument_id": instrument_id,
        "symbol": symbol,
        "name": description,
        "description": description,
        "asset_class": metadata.get("asset_class") or "UNKNOWN",
        "units": _live_etoro_metric_text(item, ("units", "netUnits", "quantity")),
        "average_entry_price": _live_etoro_metric_text(
            item, ("openRate", "avgOpenRate", "averageOpenPrice")
        ) or _live_etoro_metric_text(
            pnl_item or {}, ("openRate", "avgOpenRate", "averageOpenPrice")
        ),
        "current_price": _live_etoro_metric_text(
            item, ("currentRate", "marketPrice")
        ) or _live_etoro_metric_text(pnl_item or {}, ("currentRate", "marketPrice")),
        "current_value": _live_etoro_metric_text(
            item,
            ("currentValue", "currentExposure", "netCurrentExposureAccountCurrency"),
        ) or _live_etoro_metric_text(
            pnl_item or {},
            ("currentValue", "currentExposure", "netCurrentExposureAccountCurrency"),
        ),
        "unrealized_pnl": _live_etoro_metric_text(
            item,
            ("unrealizedPnL", "unrealizedPnl", "accountCurrencyReturn", "pnlAccountCurrency"),
            nested_keys=("pnL", "pnl"),
        ) or _live_etoro_metric_text(
            pnl_item or {},
            ("unrealizedPnL", "unrealizedPnl", "accountCurrencyReturn", "pnlAccountCurrency"),
            nested_keys=("pnL", "pnl"),
        ),
        "invested_amount": _live_etoro_metric_text(
            pnl_item or item, ("amount", "initialAmountInDollars", "initialAmount")
        ),
        "direction": "LONG" if item.get("isBuy") is True else "SHORT",
    }


def _live_etoro_metric_text(
    item: dict[str, object],
    keys: tuple[str, ...],
    *,
    nested_keys: tuple[str, ...] = (),
) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            value = next((value[name] for name in nested_keys if value.get(name) is not None), None)
        if value is None or isinstance(value, (bool, list, tuple, set)):
            continue
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if decimal_value.is_finite():
            return str(decimal_value)
    return None


def _live_etoro_instruments(*, offset: int = 0) -> dict[str, object]:
    """Return a bounded, sanitized list of currently open Demo instruments."""
    from app.brokers.etoro.live_candidates import current_catalog_candidates
    values = load_runtime_values()
    credentials = runtime_credentials(values)
    config = load_config(values)
    if credentials is None or not config.etoro_api_enabled:
        return {"status": "NOT_CONFIGURED", "source": "ETORO_LIVE_READ_ONLY", "instruments": []}
    client = ManualReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
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

    def _dashboard_read_authorized(self) -> bool:
        """Keep proxied dashboard reads behind a server-held bearer token."""
        if self._direct_local():
            return True
        configured = load_runtime_values().get("AEGIS_DASHBOARD_ACCESS_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if len(configured) < 32 or not supplied.startswith("Bearer "):
            return False
        return hmac.compare_digest(supplied[7:].strip(), configured)

    def _same_origin(self) -> bool:
        """Require browser same-origin semantics for the temporary session token."""
        origin = self.headers.get("Origin", "").rstrip("/")
        host = self.headers.get("Host", "")
        return bool(host) and origin in {f"http://{host}", f"https://{host}"}

    def _session_origin_allowed(self) -> bool:
        """Allow local or Cloudflare-forwarded same-origin browser sessions only."""
        return self._direct_local() or (self._forwarded_remote() and self._same_origin())

    def _forwarded_remote(self) -> bool:
        proxy_headers = any(
            self.headers.get(h)
            for h in ("CF-Connecting-IP", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto")
        )
        # Cloudflared connects to this server over loopback. Proxy headers must
        # still classify that request as remote; non-local peers are remote even
        # if an intermediary omitted forwarding headers.
        return proxy_headers or not self._direct_local()

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
        if path.startswith("/api/") and not self._dashboard_read_authorized():
            self._send_json(401, {"status": "DASHBOARD_READ_UNAUTHORIZED"})
            return
        if path == "/api/etoro/demo":
            try:
                self._send_json(200, _live_etoro_demo_snapshot())
            except EtoroApiError as exc:
                self._send_json(
                    503,
                    {
                        "status": "LIVE_READ_UNAVAILABLE",
                        "source": "ETORO_API",
                        "error": exc.category.value,
                        "diagnostics": exc.safe_metadata(),
                    },
                )
            except Exception:
                self._send_json(
                    503,
                    {
                        "status": "LIVE_READ_UNAVAILABLE",
                        "source": "ETORO_API",
                        "error": "UNEXPECTED_READ_ERROR",
                    },
                )
            return
        if path == "/api/etoro/demo/orders":
            try:
                manual_payload = manual_orders.recent()
                aegis_orders = _read_aegis_demo_orders()
                manual_rows = manual_payload.get("orders", [])
                if not isinstance(manual_rows, list):
                    manual_rows = []
                unresolved = [
                    order for order in aegis_orders if order.get("status") == "UNCONFIRMED"
                ]
                unresolved_capital = Decimal("0")
                for order in unresolved:
                    try:
                        unresolved_capital += Decimal(str(order.get("amount") or "0"))
                    except (InvalidOperation, TypeError, ValueError):
                        unresolved_capital = Decimal("0")
                        break
                self._send_json(
                    200,
                    {
                        **manual_payload,
                        "status": "LOCAL_AND_AEGIS_ORDER_LEDGER",
                        "orders": [*aegis_orders, *manual_rows],
                        "aegis_unresolved_count": len(unresolved),
                        "aegis_unresolved_capital_eur": f"{unresolved_capital:.2f}",
                    },
                )
            except Exception:
                self._send_json(503, {"status": "LOCAL_ORDER_LEDGER_UNAVAILABLE", "orders": []})
            return
        if path == "/api/benchmarks/etoro":
            try:
                self._send_json(200, _live_etoro_benchmark_snapshot())
            except Exception:
                self._send_json(
                    503,
                    {
                        "status": "BENCHMARK_READ_UNAVAILABLE",
                        "source": "ETORO_PUBLIC_API_READ_ONLY",
                        "profiles": [],
                    },
                )
            return
        if path == "/api/etoro/catalog/search":
            params = parse_qs(parsed_url.query, keep_blank_values=True)
            query = params.get("q", [""])[0].strip()
            if len(query) < 2 or len(query) > 80:
                self._send_json(
                    400,
                    {
                        "status": "INVALID_QUERY",
                        "message": "La ricerca richiede da 2 a 80 caratteri.",
                        "results": [],
                    },
                )
                return
            try:
                limit = int(params.get("limit", ["30"])[0])
                offset = int(params.get("offset", ["0"])[0])
            except (TypeError, ValueError):
                self._send_json(
                    400,
                    {
                        "status": "INVALID_PAGINATION",
                        "message": "Parametri di pagina non validi.",
                        "results": [],
                    },
                )
                return
            if not 1 <= limit <= 50 or offset < 0:
                self._send_json(
                    400,
                    {
                        "status": "INVALID_PAGINATION",
                        "message": "Parametri di pagina non validi.",
                        "results": [],
                    },
                )
                return
            try:
                payload = _search_etoro_catalog(query, limit=limit, offset=offset)
                self._send_json(200, payload)
            except Exception:
                self._send_json(
                    503,
                    {
                        "status": "CATALOG_SEARCH_UNAVAILABLE",
                        "message": "Il catalogo locale non è disponibile.",
                        "results": [],
                    },
                )
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
        if path == "/api/secondary/news":
            values = load_runtime_values()
            key = relay_key(values)
            if key is None:
                self._send_json(503, {"status": "SECONDARY_NEWS_RELAY_NOT_CONFIGURED"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 262_144 or self.headers.get_content_type() != "application/json":
                    raise ValueError("invalid relay body")
                envelope = json.loads(self.rfile.read(size))
                signature = self.headers.get("X-Aegis-News-Relay-Signature", "")
                if not isinstance(envelope, dict) or not hmac.compare_digest(
                    signature, str(envelope.get("signature", ""))
                ):
                    raise ValueError("invalid relay signature header")
                body = store_envelope(
                    envelope,
                    key=key,
                    path=relay_store_path(values),
                )
                self._send_json(
                    200,
                    {
                        "status": "SECONDARY_NEWS_ACCEPTED",
                        "shadow_only": True,
                        "source_id": body["source_id"],
                        "contexts": len(body["contexts"]),
                    },
                )
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                self._send_json(400, {"status": "SECONDARY_NEWS_REJECTED"})
            return
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
            runtime_status = _overlay_local_etoro_universe_metrics(
                read_etoro_demo_runtime_status()
            )
            snapshot = home_snapshot_from_scan_cycle(
                report,
                live_system_status=live_system_status_from_runtime(
                    runtime_status,
                    overnight_activity=_read_runtime_activity(),
                ),
            )
            # The external benchmark is intentionally fetched through its own
            # read-only endpoint: a slow/denied public API must never stall the
            # operational dashboard or its refresh loop.
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
