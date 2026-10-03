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

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import BASE, EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UnknownWriteOutcome
from app.brokers.models import BrokerIdentity
from app.storage.sqlite import SqliteRecordStore

DEMO_CLOSE_PATH_PREFIX = (
    "/api/v1/trading/execution/demo/market-close-orders/positions/"
)
DEMO_CLOSE_URL_PREFIX = BASE + DEMO_CLOSE_PATH_PREFIX
DEMO_CLOSE_LOOKUP_BACKOFF = timedelta(minutes=1)
DEMO_CLOSE_PAYLOAD_MODE = "UNITS_TO_DEDUCT_NULL"
DEMO_CLOSE_OMITTED_PAYLOAD_MODE = "UNITS_TO_DEDUCT_OMITTED"
DEMO_CLOSE_INSTRUMENT_PAYLOAD_MODE = "INSTRUMENT_ID_UNITS_NULL"

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


def _matches(
    rows: tuple[dict[str, object], ...], instrument_id: int
) -> tuple[dict[str, object], ...]:
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
        "close_retry_calls": 0,
        "closed_confirmed": 0,
        "closed_total": 0,
        "pending_confirmation": 0,
        "blocked": 0,
        "errors": (),
        "response_diagnostics": [],
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
        raw_pnl = client.demo_pnl_payload()
        rows = _position_rows(raw_pnl)
        raw_portfolio = client.demo_portfolio_payload()
        portfolio_rows = _position_rows(raw_portfolio)
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
            # A FILLED opening remains in the submission ledger after its
            # position has closed.  Count that closure in the cumulative
            # total, but do not report it as newly confirmed every poll or
            # overwrite the original confirmation timestamp.
            if str(payload.get("exit_status", "")).upper() == "CLOSED":
                report["closed_total"] = int(report["closed_total"]) + 1
                continue
            instrument_id = int(payload["instrument_id"])
            report["evaluated"] = int(report["evaluated"]) + 1
            position = by_instrument.get(instrument_id)
            instrument_matches = _matches(rows, instrument_id)
            recorded_position_id = str(
                payload.get("broker_position_id") or payload.get("position_id") or ""
            )
            matches = (
                tuple(
                    row
                    for row in instrument_matches
                    if str(row.get("positionID", row.get("positionId", "")))
                    == recorded_position_id
                )
                if recorded_position_id
                else instrument_matches
            )
            portfolio_instrument_matches = _matches(portfolio_rows, instrument_id)
            portfolio_position_matches = (
                tuple(
                    row
                    for row in portfolio_instrument_matches
                    if str(row.get("positionID", row.get("positionId", "")))
                    == recorded_position_id
                )
                if recorded_position_id
                else portfolio_instrument_matches
            )
            aggregate_position_absent = (
                position is None or position.current_exposure <= 0
            )
            exact_position_absent = (
                bool(recorded_position_id)
                and not matches
                and not portfolio_position_matches
            )
            if aggregate_position_absent or exact_position_absent:
                if matches or portfolio_position_matches:
                    errors.append(
                        f"{payload.get('symbol', instrument_id)}:PORTFOLIO_AGGREGATE_MISMATCH"
                    )
                    report["blocked"] = int(report["blocked"]) + 1
                    continue
                # Both live broker views show the position absent.  The
                # dedicated close-order endpoint and exact trade history add
                # provenance when available; their failure never fabricates
                # execution or causes another close POST.
                order_confirmed = payload.get("exit_order_state") == "FILLED"
                close_order_id = str(payload.get("exit_order_id") or "")
                if close_order_id.isdigit() and recorded_position_id.isdigit():
                    lookup_close = getattr(client, "demo_close_order_position_affected", None)
                    if callable(lookup_close):
                        try:
                            order_confirmed = order_confirmed or bool(
                                lookup_close(close_order_id, recorded_position_id)
                            )
                        except (EtoroApiError, DemoExitError, ValueError, TypeError) as exc:
                            errors.append(
                                f"{payload.get('symbol', instrument_id)}:CLOSE_ORDER_LOOKUP_"
                                f"{getattr(exc, 'status', None) or type(exc).__name__}"
                            )
                closed_trade = None
                if recorded_position_id.isdigit():
                    lookup_history = getattr(client, "demo_closed_trade_by_position", None)
                    if callable(lookup_history):
                        try:
                            closed_trade = lookup_history(
                                recorded_position_id,
                                min_date=(observed_at - timedelta(days=30)).date(),
                                instrument_id=instrument_id,
                            )
                        except (EtoroApiError, DemoExitError, ValueError, TypeError) as exc:
                            errors.append(
                                f"{payload.get('symbol', instrument_id)}:CLOSE_HISTORY_"
                                f"{getattr(exc, 'status', None) or type(exc).__name__}"
                            )
                confirmed_history = isinstance(closed_trade, dict)
                confirmation_values: dict[str, object] = {
                    "exit_status": "CLOSED",
                    "exit_reason": "POSITION_ABSENT_ON_READBACK",
                    "exit_confirmation_basis": (
                        "DEMO_TRADE_HISTORY_POSITION_ID"
                        if confirmed_history else "BROKER_PORTFOLIO_AND_DETAIL_ABSENT"
                    ),
                    "exit_order_confirmed": order_confirmed,
                    "exit_trade_history_confirmed": confirmed_history,
                    "exit_confirmed_at": observed_at.isoformat(),
                    "remaining_exposure_account_currency": "0",
                    "remaining_exposure_eur": "0",
                }
                if confirmed_history:
                    assert isinstance(closed_trade, dict)
                    if "realized_pnl_account_currency" in closed_trade:
                        realized = closed_trade["realized_pnl_account_currency"]
                        confirmation_values["exit_realized_pnl_account_currency"] = realized
                        try:
                            confirmation_values["exit_realized_return_pct"] = str(
                                _decimal(realized, field="exit_realized_pnl")
                                / _decimal(
                                    payload.get("amount_account_currency"),
                                    field="amount_account_currency",
                                )
                            )
                            confirmation_values["exit_realized_return_basis"] = (
                                "CONFIRMED_TRADE_HISTORY_OVER_EXECUTED_EXPOSURE"
                            )
                        except DemoExitError:
                            errors.append(
                                f"{payload.get('symbol', instrument_id)}:"
                                "EXIT_REALIZED_RETURN_UNAVAILABLE"
                            )
                    if "closed_at" in closed_trade:
                        confirmation_values["exit_broker_closed_at"] = closed_trade["closed_at"]
                _persist(
                    registry,
                    key,
                    state=state,
                    values=confirmation_values,
                )
                report["closed_confirmed"] = int(report["closed_confirmed"]) + 1
                report["closed_total"] = int(report["closed_total"]) + 1
                continue
            if not matches:
                # Aggregate portfolio and position detail disagree.  Keep the
                # position open and wait for a coherent read-back; never infer
                # a close from a missing detail row.
                errors.append(f"{payload.get('symbol', instrument_id)}:POSITION_DETAIL_MISSING")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            matched_position_id = (
                ""
                if len(matches) != 1
                else str(matches[0].get("positionID", matches[0].get("positionId", "")))
            )
            if len(matches) != 1 or not matched_position_id.isdigit():
                errors.append(f"{payload.get('symbol', instrument_id)}:POSITION_ID_AMBIGUOUS")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            exit_status = str(payload.get("exit_status", ""))
            if exit_status in {"SUBMITTED", "UNKNOWN"}:
                broker_exit_state = _reconcile_close_order(
                    client=client,
                    registry=registry,
                    key=key,
                    state=state,
                    payload=payload,
                    observed_at=observed_at,
                    errors=errors,
                )
                if broker_exit_state in {"REJECTED", "CANCELLED"}:
                    report["blocked"] = int(report["blocked"]) + 1
                else:
                    report["pending_confirmation"] = int(report["pending_confirmation"]) + 1
                continue
            corrected_close_retry = _instrument_close_retry_allowed(payload)
            raw_position_pnl = matches[0].get("unrealizedPnL")
            if not isinstance(raw_position_pnl, dict):
                raw_position_pnl = matches[0].get("unrealizedPnl")
            if not isinstance(raw_position_pnl, dict):
                errors.append(f"{payload.get('symbol', instrument_id)}:POSITION_PNL_UNAVAILABLE")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            raw_pnl_amount = raw_position_pnl.get("pnL", raw_position_pnl.get("pnl"))
            raw_initial = (
                raw_position_pnl.get("marginInAccountCurrency")
                or matches[0].get("amount")
                or matches[0].get("initialAmountInDollars")
            )
            if raw_pnl_amount is None or raw_initial is None:
                errors.append(f"{payload.get('symbol', instrument_id)}:POSITION_PNL_UNAVAILABLE")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            try:
                position_pnl = _decimal(raw_pnl_amount, field="position_pnl")
                initial = _decimal(raw_initial, field="position_initial_exposure")
            except DemoExitError:
                errors.append(f"{payload.get('symbol', instrument_id)}:POSITION_PNL_UNAVAILABLE")
                report["blocked"] = int(report["blocked"]) + 1
                continue
            if initial <= 0:
                errors.append(
                    f"{payload.get('symbol', instrument_id)}:INITIAL_EXPOSURE_UNAVAILABLE"
                )
                report["blocked"] = int(report["blocked"]) + 1
                continue
            pnl_pct = position_pnl / initial
            stored_peak = _decimal(payload.get("peak_pnl_pct", pnl_pct), field="peak_pnl_pct")
            peak = max(stored_peak, pnl_pct)
            decision = evaluate_demo_exit(pnl_pct=pnl_pct, peak_pnl_pct=peak)
            _persist(
                registry,
                key,
                state=state,
                values={
                    "peak_pnl_pct": str(peak),
                    # The peak is historical state, not the live P/L that
                    # caused this evaluation. Persist both explicitly.
                    "last_exit_pnl_pct": str(pnl_pct),
                    "last_exit_peak_pnl_pct": str(peak),
                    "last_exit_position_pnl": str(position_pnl),
                    "last_exit_initial_exposure": str(initial),
                    "last_exit_evaluation_at": observed_at.isoformat(),
                    "last_exit_decision": decision.action,
                    "last_exit_reason": decision.reason,
                },
            )
            if decision.action != "CLOSE":
                if corrected_close_retry:
                    errors.append(
                        f"{payload.get('symbol', instrument_id)}:"
                        "CLOSE_RETRY_SKIPPED_POLICY_NO_LONGER_CLOSE"
                    )
                    report["blocked"] = int(report["blocked"]) + 1
                else:
                    report["held"] = int(report["held"]) + 1
                continue
            if exit_status == "REJECTED" and not corrected_close_retry:
                # A past terminal rejection must not freeze the *current* exit
                # decision. Keep evaluating the live position, but never
                # replay a close that the broker has definitively rejected.
                status_detail = payload.get("exit_broker_terminal_status") or payload.get(
                    "exit_http_status", "UNKNOWN"
                )
                errors.append(
                    f"{payload.get('symbol', instrument_id)}:CLOSE_REJECTED_{status_detail}"
                )
                report["blocked"] = int(report["blocked"]) + 1
                continue
            report["close_triggered"] = int(report["close_triggered"]) + 1
            position_id = str(matches[0].get("positionID", matches[0].get("positionId", "")))
            endpoint = f"{DEMO_CLOSE_URL_PREFIX}{position_id}"
            assert_demo_close_route(endpoint)
            headers = credentials.headers()
            prior_request_id = str(payload.get("exit_request_id") or "")
            retry_count = _nonnegative_int(payload.get("exit_retry_count"))
            next_retry_count = retry_count + 1 if corrected_close_retry else retry_count
            attempt_values: dict[str, object] = {
                "exit_request_payload_mode": DEMO_CLOSE_INSTRUMENT_PAYLOAD_MODE,
                "exit_retry_count": next_retry_count,
                "exit_request_id": headers["x-request-id"],
                "exit_last_attempt_at": observed_at.isoformat(),
                "exit_trigger_pnl_pct": str(pnl_pct),
                "exit_trigger_peak_pnl_pct": str(peak),
                "exit_trigger_position_pnl": str(position_pnl),
                "exit_trigger_initial_exposure": str(initial),
            }
            if corrected_close_retry:
                attempt_values["exit_instrument_id_retry_attempted"] = True
            if corrected_close_retry and prior_request_id:
                attempt_values["exit_retry_of_request_id"] = prior_request_id
            report["close_write_calls"] = int(report["close_write_calls"]) + 1
            if corrected_close_retry:
                report["close_retry_calls"] = int(report["close_retry_calls"]) + 1
            try:
                response = http.post_once(
                    endpoint,
                    headers,
                    # The position is matched to this instrument in both live
                    # broker views above. Include InstrumentId in the body:
                    # the close endpoint can reject a position-only payload
                    # with HTTP 400 despite the position ID in the path.
                    {"InstrumentId": instrument_id, "UnitsToDeduct": None},
                )
            except UnknownWriteOutcome:
                _persist(
                    registry,
                    key,
                    state=state,
                    values={
                        "exit_status": "UNKNOWN",
                        "exit_reason": decision.reason,
                        **attempt_values,
                    },
                )
                errors.append(f"{payload.get('symbol', instrument_id)}:CLOSE_OUTCOME_UNKNOWN")
                continue
            if not 200 <= response.status < 300:
                # A 4xx is a definitive broker rejection. A 5xx/redirect may
                # have happened after the broker accepted the write, so treat
                # it as ambiguous and reconcile by request reference instead
                # of risking a duplicate close.
                rejected = 400 <= response.status < 500
                response_state = "REJECTED" if rejected else "UNKNOWN"
                broker_error = _safe_close_error_details(response.body)
                attempt_values.update(broker_error)
                if broker_error:
                    diagnostics = report["response_diagnostics"]
                    assert isinstance(diagnostics, list)
                    diagnostics.append(
                        {
                            "symbol": str(payload.get("symbol", instrument_id)),
                            "http_status": response.status,
                            **broker_error,
                        }
                    )
                _persist(
                    registry,
                    key,
                    state=state,
                    values={
                        "exit_status": response_state,
                        "exit_http_status": response.status,
                        "exit_reason": decision.reason,
                        **attempt_values,
                    },
                )
                errors.append(
                    f"{payload.get('symbol', instrument_id)}:CLOSE_HTTP_{response.status}"
                )
                if not rejected:
                    report["pending_confirmation"] = int(report["pending_confirmation"]) + 1
                else:
                    report["blocked"] = int(report["blocked"]) + 1
                continue
            order_id: str | None = None
            try:
                order_id = _close_order_id_from_response(
                    response.json(), expected_position_id=position_id
                )
            except (ValueError, UnicodeDecodeError):
                # A 2xx still means the write was accepted; the request
                # reference remains available for the read-only lookup.
                pass
            if order_id is not None:
                attempt_values["exit_order_id"] = order_id
            _persist(
                registry,
                key,
                state=state,
                values={
                    "exit_status": "SUBMITTED",
                    "exit_reason": decision.reason,
                    "exit_submitted_at": observed_at.isoformat(),
                    **attempt_values,
                },
            )
            report["pending_confirmation"] = int(report["pending_confirmation"]) + 1
        report["errors"] = tuple(errors)
        return report
    except (EtoroApiError, DemoExitError, KeyError, TypeError, ValueError) as exc:
        report["errors"] = (type(exc).__name__,)
        report["blocked"] = int(report["blocked"]) + 1
        return report


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _close_order_id_from_response(
    raw: object, *, expected_position_id: str
) -> str | None:
    """Read either eToro close response shape without trusting another position."""
    if not isinstance(raw, dict):
        return None
    nested = raw.get("orderForClose")
    if isinstance(nested, dict):
        returned_position = nested.get("positionID", nested.get("positionId"))
        if returned_position is not None and str(returned_position) != expected_position_id:
            return None
        candidate = nested.get("orderID", nested.get("orderId"))
    else:
        candidate = raw.get("orderId", raw.get("orderID"))
    return str(candidate) if candidate is not None and str(candidate).isdigit() else None


def _instrument_close_retry_allowed(payload: dict[str, object]) -> bool:
    """One guarded correction for a definitively rejected pre-InstrumentId body.

    The caller independently rechecks the exact live position and current
    exit trigger before sending this request. Ambiguous outcomes are excluded.
    """
    return (
        str(payload.get("exit_status", "")).upper() == "REJECTED"
        and str(payload.get("exit_http_status", "")) == "400"
        and payload.get("exit_request_payload_mode")
        in {None, "", "OMIT_UNITS_TO_DEDUCT", DEMO_CLOSE_PAYLOAD_MODE,
            DEMO_CLOSE_OMITTED_PAYLOAD_MODE}
        and not payload.get("exit_instrument_id_retry_attempted")
        and _nonnegative_int(payload.get("exit_retry_count")) < 4
    )


def _safe_close_error_details(body: bytes) -> dict[str, str]:
    """Extract only short, allowlisted broker error fields; never persist raw bodies."""
    if not body or len(body) > 8192:
        return {}
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}

    code_keys = {"code", "errorcode", "errorid", "reasoncode"}
    message_keys = {"message", "errormessage", "detail", "description", "title"}
    values: dict[str, str] = {}

    def visit(value: object, depth: int = 0) -> None:
        if depth > 3 or len(values) >= 2:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold().replace("_", "").replace("-", "")
                target = (
                    "exit_broker_error_code"
                    if normalized in code_keys
                    else "exit_broker_error_message"
                    if normalized in message_keys
                    else None
                )
                if target is not None and isinstance(item, (str, int, float)):
                    text = _safe_error_text(str(item))
                    if text:
                        values.setdefault(target, text)
                elif isinstance(item, (dict, list)):
                    visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:8]:
                visit(item, depth + 1)

    visit(decoded)
    return values


def _safe_error_text(value: str) -> str:
    text = "".join(character for character in value if character.isprintable()).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(
        r"(?i)\b(x-api-key|x-user-key|api[-_ ]?key|user[-_ ]?key|authorization|token|password)\b\s*[:=]\s*[^,;\s]+",
        r"\1=[redacted]",
        text,
    )
    return text[:180]


def _reconcile_close_order(
    *,
    client: EtoroReadClient,
    registry: SqliteRecordStore,
    key: str,
    state: str,
    payload: dict[str, object],
    observed_at: datetime,
    errors: list[str],
) -> str | None:
    """Read an uncertain close by broker order ID or its request reference."""
    lookup = getattr(client, "demo_order_lookup", None)
    request_id = str(payload.get("exit_request_id") or "")
    order_id = str(payload.get("exit_order_id") or "")
    if not callable(lookup) or (not order_id and not request_id):
        return None
    raw_attempted_at = payload.get("exit_order_lookup_attempted_at")
    if isinstance(raw_attempted_at, str):
        try:
            previous_attempt = datetime.fromisoformat(raw_attempted_at)
        except ValueError:
            previous_attempt = None
        if previous_attempt is not None:
            if previous_attempt.tzinfo is None:
                previous_attempt = previous_attempt.replace(tzinfo=observed_at.tzinfo)
            if observed_at - previous_attempt < DEMO_CLOSE_LOOKUP_BACKOFF:
                return None
    lookup_at = observed_at.isoformat()
    try:
        result = (
            (
                "FILLED"
                if client.demo_close_order_position_affected(
                    order_id,
                    str(payload.get("broker_position_id") or payload.get("position_id") or ""),
                )
                else "UNKNOWN"
            )
            if order_id
            else lookup("", reference_id=request_id)
        )
    except (EtoroApiError, DemoExitError, ValueError, TypeError) as exc:
        safe_status = getattr(exc, "status", None)
        errors.append(
            f"{payload.get('symbol', 'POSITION')}:CLOSE_LOOKUP_"
            f"{safe_status or type(exc).__name__}"
        )
        _persist(
            registry,
            key,
            state=state,
            values={
                "exit_order_lookup_attempted_at": lookup_at,
                "exit_order_lookup_error": (
                    f"HTTP_{safe_status}" if safe_status else type(exc).__name__
                ),
            },
        )
        return None
    observed_state = str(getattr(result, "value", result)).upper()
    values: dict[str, object] = {
        "exit_order_lookup_attempted_at": lookup_at,
        "exit_order_lookup_error": None,
        "exit_order_state": observed_state,
    }
    if observed_state in {"REJECTED", "CANCELLED"}:
        values["exit_status"] = "REJECTED"
        values["exit_broker_terminal_status"] = observed_state
        errors.append(f"{payload.get('symbol', 'POSITION')}:CLOSE_ORDER_{observed_state}")
    elif observed_state == "FILLED":
        # The exact portfolio position is still visible in this poll. Retain
        # the lifecycle reservation and wait for that read-back to disappear.
        values["exit_status"] = "SUBMITTED"
    _persist(registry, key, state=state, values=values)
    return observed_state
