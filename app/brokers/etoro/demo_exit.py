"""Guarded automatic exits for eToro Demo positions.

This module is deliberately Demo-only.  It reuses the frozen balanced
EXITPOLICY_V2 parameters for the two broker-observable triggers we can
evaluate without fabricating a fresh strategy analysis:

* capital protection at a 16% loss from the recorded initial exposure;
* trailing protection after a 12% favorable excursion and a 10% retracement.

The full broker-neutral policy still owns the richer thesis/regime signals.
This adapter only turns the observable lifecycle state into the documented
eToro Demo market-close request and records the result idempotently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import BASE, EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UnknownWriteOutcome
from app.brokers.models import BrokerIdentity, ExecutionState
from app.storage.sqlite import SqliteRecordStore

DEMO_CLOSE_PATH_PREFIX = (
    "/api/v1/trading/execution/demo/market-close-orders/positions/"
)
DEMO_CLOSE_URL_PREFIX = BASE + DEMO_CLOSE_PATH_PREFIX

# These are the frozen balanced guarded candidate values declared in
# app.agent.exit_policy.  They are not tuned from the current BTC result.
CAPITAL_CLOSE_DRAWDOWN = Decimal("0.16")
TRAILING_MIN_FAVORABLE_EXCURSION = Decimal("0.12")
TRAILING_CLOSE_RETRACE = Decimal("0.10")


class DemoExitError(RuntimeError):
    """A safe, non-secret Demo exit error."""


@dataclass(frozen=True, slots=True)
class DemoExitDecision:
    action: str
    reason: str
    pnl_pct: Decimal | None = None
    peak_pnl_pct: Decimal | None = None


def assert_demo_close_route(url: str) -> None:
    """Reject every write URL except an eToro Demo market-close route."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "public-api.etoro.com"
        or not parsed.path.startswith(DEMO_CLOSE_PATH_PREFIX)
        or not parsed.path.rsplit("/", 1)[-1].isdigit()
    ):
        raise DemoExitError("only the verified eToro Demo close route is permitted")


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DemoExitError(f"invalid Demo exit field: {field}") from exc
    if not result.is_finite():
        raise DemoExitError(f"invalid Demo exit field: {field}")
    return result


def evaluate_demo_exit(
    *,
    pnl_pct: Decimal,
    peak_pnl_pct: Decimal,
) -> DemoExitDecision:
    """Apply only the frozen, broker-observable exit guards."""
    if pnl_pct <= -CAPITAL_CLOSE_DRAWDOWN:
        return DemoExitDecision(
            action="CLOSE",
            reason="EXITPOLICY_V2_CAPITAL_PROTECTION_CLOSE",
            pnl_pct=pnl_pct,
            peak_pnl_pct=peak_pnl_pct,
        )
    retrace = peak_pnl_pct - pnl_pct
    if (
        peak_pnl_pct >= TRAILING_MIN_FAVORABLE_EXCURSION
        and retrace >= TRAILING_CLOSE_RETRACE
    ):
        return DemoExitDecision(
            action="CLOSE",
            reason="EXITPOLICY_V2_TRAILING_PROFIT_CLOSE",
            pnl_pct=pnl_pct,
            peak_pnl_pct=peak_pnl_pct,
        )
    return DemoExitDecision(
        action="HOLD",
        reason="EXITPOLICY_V2_NO_EXIT_TRIGGER_HOLD",
        pnl_pct=pnl_pct,
        peak_pnl_pct=peak_pnl_pct,
    )


def _position_rows(raw: object) -> tuple[dict[str, object], ...]:
    if not isinstance(raw, dict):
        raise DemoExitError("Demo portfolio payload is not an object")
    data = raw.get("clientPortfolio")
    if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
        raise DemoExitError("Demo portfolio positions are unavailable")
    return tuple(row for row in data["positions"] if isinstance(row, dict))


def _matches(rows: tuple[dict[str, object], ...], instrument_id: int) -> tuple[dict[str, object], ...]:
    return tuple(
        row
        for row in rows
        if str(row.get("instrumentID")) == str(instrument_id)
        and row.get("isBuy") is True
        and int(row.get("mirrorID", 0) or 0) == 0
    )


def _persist(
    registry: SqliteRecordStore,
    key: str,
    *,
    state: str,
    values: dict[str, object],
) -> None:
    registry.update_demo_submission(key, state, values)


def manage_demo_exits(
    *,
    client: EtoroReadClient,
    identity: BrokerIdentity,
    credentials: EtoroCredentials,
    http: DisciplinedHttpClient,
    registry: SqliteRecordStore,
    observed_at: datetime,
) -> dict[str, object]:
    """Evaluate and, when necessary, close Aegis-owned Demo positions.

    A successful close request is never replayed automatically.  Until the
    read-back confirms the position is gone, the original OPEN record remains
    FILLED and therefore continues to reserve the capital envelope.
    """
    report: dict[str, object] = {
        "enabled": True,
        "evaluated": 0,
        "held": 0,
        "close_triggered": 0,
        "close_write_calls": 0,
        "closed_confirmed": 0,
        "pending_confirmation": 0,
        "blocked": 0,
        "errors": (),
        "policy": {
            "capital_close_drawdown_pct": str(CAPITAL_CLOSE_DRAWDOWN),
            "trailing_min_favorable_excursion_pct": str(
                TRAILING_MIN_FAVORABLE_EXCURSION
            ),
            "trailing_close_retrace_pct": str(TRAILING_CLOSE_RETRACE),
        },
    }
    errors: list[str] = []
    try:
        submissions = registry.filled_demo_submissions()
        if not submissions:
            return report
        raw_portfolio = client.demo_portfolio_payload()
        rows = _position_rows(raw_portfolio)
        snapshot = client.demo_account(identity)
        by_instrument = {position.instrument_id: position for position in snapshot.positions}
        for submission in submissions:
            key = str(submission["idempotency_key"])
            state = str(submission["state"])
            payload = submission["payload"]
            if not isinstance(payload, dict):
                continue
            if str(payload.get("action", "OPEN")).upper() not in {"OPEN", "INCREASE"}:
                continue
            instrument_id = int(payload["instrument_id"])
            report["evaluated"] = int(report["evaluated"]) + 1
            position = by_instrument.get(instrument_id)
            matches = _matches(rows, instrument_id)
            if position is None or position.current_exposure <= 0:
                # The position disappeared outside this manager or was already
                # closed. Mark the lifecycle record closed without writing.
                _persist(
                    registry,
                    key,
                    state=state,
                    values={
                        "exit_status": "CLOSED",
                        "exit_reason": "POSITION_ABSENT_ON_READBACK",
                        "exit_confirmed_at": observed_at.isoformat(),
                        "remaining_exposure_eur": "0",
                    },
                )
                report["closed_confirmed"] = int(report["closed_confirmed"]) + 1
                continue
            if not matches:
                # Aggregate portfolio and position detail disagree.  Keep the
                # position open and wait for a coherent read-back; never infer
                # a close from a missing detail row.
                errors.append(f"{payload.get('symbol', instrument_id)}:POSITION_DETAIL_MISSING")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            if len(matches) != 1 or str(matches[0].get("positionID", "")).isdigit() is False:
                errors.append(f"{payload.get('symbol', instrument_id)}:POSITION_ID_AMBIGUOUS")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            exit_status = str(payload.get("exit_status", ""))
            if exit_status in {"SUBMITTED", "UNKNOWN", "REJECTED"}:
                report["pending_confirmation"] = int(report["pending_confirmation"]) + 1
                continue
            initial = position.initial_exposure
            if initial <= 0:
                errors.append(f"{payload.get('symbol', instrument_id)}:INITIAL_EXPOSURE_UNAVAILABLE")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            pnl_pct = position.unrealized_pnl_account_currency / initial
            stored_peak = _decimal(payload.get("peak_pnl_pct", pnl_pct), field="peak_pnl_pct")
            peak = max(stored_peak, pnl_pct)
            decision = evaluate_demo_exit(pnl_pct=pnl_pct, peak_pnl_pct=peak)
            _persist(
                registry,
                key,
                state=state,
                values={
                    "peak_pnl_pct": str(peak),
                    "last_exit_evaluation_at": observed_at.isoformat(),
                    "last_exit_decision": decision.action,
                    "last_exit_reason": decision.reason,
                },
            )
            if decision.action != "CLOSE":
                report["held"] = int(report["held"]) + 1
                continue
            report["close_triggered"] = int(report["close_triggered"]) + 1
            position_id = str(matches[0]["positionID"])
            endpoint = f"{DEMO_CLOSE_URL_PREFIX}{position_id}"
            assert_demo_close_route(endpoint)
            headers = credentials.headers()
            try:
                response = http.post_once(
                    endpoint,
                    headers,
                    # Full close is explicit; no partial reduction is sent by
                    # this first guarded lifecycle implementation.
                    {"UnitsToDeduct": None},
                )
            except UnknownWriteOutcome:
                _persist(
                    registry,
                    key,
                    state=state,
                    values={
                        "exit_status": "UNKNOWN",
                        "exit_reason": decision.reason,
                        "exit_request_id": headers["x-request-id"],
                    },
                )
                errors.append(f"{payload.get('symbol', instrument_id)}:CLOSE_OUTCOME_UNKNOWN")
                continue
            report["close_write_calls"] = int(report["close_write_calls"]) + 1
            if not 200 <= response.status < 300:
                _persist(
                    registry,
                    key,
                    state=state,
                    values={
                        "exit_status": "REJECTED",
                        "exit_http_status": response.status,
                        "exit_reason": decision.reason,
                    },
                )
                errors.append(f"{payload.get('symbol', instrument_id)}:CLOSE_HTTP_{response.status}")
                continue
            _persist(
                registry,
                key,
                state=state,
                values={
                    "exit_status": "SUBMITTED",
                    "exit_reason": decision.reason,
                    "exit_request_id": headers["x-request-id"],
                    "exit_submitted_at": observed_at.isoformat(),
                },
            )
            report["pending_confirmation"] = int(report["pending_confirmation"]) + 1
        report["errors"] = tuple(errors)
        return report
    except (EtoroApiError, DemoExitError, KeyError, TypeError, ValueError) as exc:
        report["errors"] = (type(exc).__name__,)
        report["blocked"] = int(report["blocked"]) + 1
        return report
