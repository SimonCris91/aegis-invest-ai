"""Read-only P/L attribution. Never substitute an exposure ceiling for a baseline."""

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _amount(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _history_date(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def demo_performance_review(
    snapshot: dict[str, object],
    path: Path = Path("work/etoro-demo-runtime.sqlite3"),
    *,
    excluded_position_ids: set[str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "UNAVAILABLE",
        "scope": "AEGIS_LEDGER_MATCHED_POSITIONS",
        "as_of": snapshot.get("as_of"),
        "currency": snapshot.get("currency"),
        "realized_pnl": None,
        "unrealized_pnl": None,
        "combined_pnl": None,
        "total_return_pct": None,
        "maximum_drawdown_pct": None,
        "benchmark_relative_return_pct": None,
        "realized_trade_count": 0,
        "realized_win_count": 0,
        "realized_loss_count": 0,
        "realized_flat_count": 0,
        "realized_win_rate": None,
        "realized_profit_factor": None,
        "realized_average_win": None,
        "realized_average_loss": None,
        "realized_history": [],
        "contested_history": [],
        "order_history": [],
        "evaluation": "NOT_ENOUGH_VERIFIED_CLOSED_TRADES",
        "validation_status": "NOT_VALIDATED",
        "limitations": ["HISTORICAL_EQUITY_AND_CASHFLOW_BASELINE_NOT_VERIFIED",
                        "MATCHED_BENCHMARK_NOT_VERIFIED"],
        "broker_write_calls": 0,
        "manual_excluded_position_count": 0,
        "certified_closed_count": 0,
        "contested_closed_count": 0,
        "contested_pnl_known": None,
        "certified_realized_pnl": None,
        "adjusted_realized_pnl": None,
        "adjusted_realized_trade_count": 0,
        "adjusted_realized_win_rate": None,
        "adjusted_realized_profit_factor": None,
        "closure_audit_status": "UNAVAILABLE",
        "closure_audit_note": "Registro chiusure non disponibile.",
    }
    if snapshot.get("status") != "LIVE_READ_ONLY" or not snapshot.get("currency"):
        return result
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            raw = connection.execute(
                "SELECT payload FROM demo_submissions WHERE state='FILLED'"
            ).fetchall()
        finally:
            connection.close()
        records = [json.loads(row[0]) for row in raw]
        if any(not isinstance(row, dict) for row in records):
            return result
    except (sqlite3.Error, ValueError, OSError):
        return result
    records = [r for r in records if str(r.get("action", "OPEN")).upper() in {"OPEN", "INCREASE"}]
    # Positions touched by a confirmed user-directed order remain part of the
    # Demo account, but are outside Aegis strategy attribution.  Excluding
    # them here prevents a manual close from changing Aegis P/L or trade stats.
    manual_ids = {str(value) for value in (excluded_position_ids or set()) if str(value)}
    excluded_records = [
        row for row in records
        if str(row.get("broker_position_id") or row.get("position_id") or "") in manual_ids
    ]
    records = [
        row for row in records
        if str(row.get("broker_position_id") or row.get("position_id") or "") not in manual_ids
    ]
    positions = snapshot.get("positions")
    if not isinstance(positions, list) or any(not isinstance(p, dict) for p in positions):
        return result
    position_ids = [str(p.get("position_id") or "") for p in positions if str(p.get("position_id") or "") not in manual_ids]
    duplicate_live = {key for key, count in Counter(position_ids).items() if count > 1}
    live_by_id = {str(p.get("position_id") or ""): p for p in positions}
    ledger_ids = [str(r.get("broker_position_id") or r.get("position_id") or "") for r in records]
    duplicate_ledger = {key for key, count in Counter(ledger_ids).items() if count > 1}
    closed = [r for r in records if r.get("exit_status") == "CLOSED"]
    open_records = [r for r in records if r.get("exit_status") != "CLOSED"]
    realized = Decimal("0")
    unrealized = Decimal("0")
    order_history: list[dict[str, object]] = []
    for row in records:
        broker_order_id = row.get("broker_order_id")
        if (
            row.get("broker_order_status") == "FILLED"
            and row.get("reconciliation") == "verified"
            and broker_order_id
        ):
            order_history.append({
                "order_id": str(broker_order_id),
                "position_id": str(row.get("broker_position_id") or row.get("position_id") or ""),
                "symbol": str(row.get("symbol") or "—"),
                "instrument_id": row.get("instrument_id"),
                "asset_class": row.get("asset_class"),
                "action": str(row.get("action") or "OPEN"),
                "status": "FILLED",
                "amount": row.get("executed_exposure_account_currency") or row.get("amount_account_currency"),
                "currency": row.get("account_currency") or snapshot.get("currency"),
                "executed_at": row.get("broker_order_reconciled_at") or row.get("reconciled_at") or row.get("submitted_at") or row.get("created_at"),
            })
    verified_closed = matched_open = 0
    matched_ids: set[str] = set()
    issues: set[str] = set()
    for row in records:
        position_id = str(row.get("broker_position_id") or row.get("position_id") or "")
        if row.get("account_currency") != snapshot["currency"]:
            issues.add("LEDGER_CURRENCY_NOT_VERIFIED")
            continue
        if not position_id or position_id in duplicate_ledger or position_id in duplicate_live:
            issues.add("POSITION_ID_MISSING_OR_AMBIGUOUS")
            continue
        if row.get("exit_status") == "CLOSED":
            amount = _amount(row.get("exit_realized_pnl_account_currency"))
            if (row.get("exit_trade_history_confirmed") is not True
                    or row.get("exit_confirmation_basis") != "DEMO_TRADE_HISTORY_POSITION_ID"
                    or amount is None or position_id in live_by_id):
                issues.add("CLOSED_TRADE_PNL_NOT_VERIFIED")
                continue
            realized += amount
            verified_closed += 1
        else:
            position = live_by_id.get(position_id)
            if position is None or str(position.get("instrument_id")) != str(row.get("instrument_id")):
                issues.add("OPEN_POSITION_NOT_MATCHED")
                continue
            amount = _amount(position.get("unrealized_pnl"))
            if amount is None:
                issues.add("OPEN_POSITION_PNL_UNAVAILABLE")
                continue
            unrealized += amount
            matched_open += 1
            matched_ids.add(position_id)
    money = lambda value: str(value.quantize(Decimal("0.01")))
    realized_complete = verified_closed == len(closed)
    open_complete = matched_open == len(open_records)
    verified_realized = []
    realized_history: list[dict[str, object]] = []
    contested_history: list[dict[str, object]] = []
    contested_amounts: list[Decimal] = []
    for row in closed:
        position_id = str(row.get("broker_position_id") or row.get("position_id") or "")
        if position_id in duplicate_ledger or position_id in duplicate_live:
            continue
        amount = _amount(row.get("exit_realized_pnl_account_currency"))
        if (
            row.get("account_currency") == snapshot["currency"]
            and row.get("exit_trade_history_confirmed") is True
            and row.get("exit_confirmation_basis") == "DEMO_TRADE_HISTORY_POSITION_ID"
            and amount is not None
            and position_id not in live_by_id
        ):
            verified_realized.append(amount)
            realized_history.append({
                "symbol": str(row.get("symbol") or "—"),
                "instrument_id": row.get("instrument_id"),
                "asset_class": row.get("asset_class"),
                "realized_pnl": money(amount),
                "realized_return_pct": row.get("exit_realized_return_pct"),
                "closed_at": row.get("exit_broker_closed_at") or row.get("exit_confirmed_at"),
                "closed_at_basis": "BROKER_EXECUTION" if row.get("exit_broker_closed_at") else "CONFIRMATION",
                "opened_at": row.get("broker_opened_at") or row.get("submitted_at") or row.get("broker_reconciled_at") or row.get("broker_order_reconciled_at") or row.get("reconciled_at"),
                "opened_at_basis": (
                    "BROKER_EXECUTION" if row.get("broker_opened_at")
                    else "SUBMISSION" if row.get("submitted_at") else "CONFIRMATION"
                ),
                "purchase_amount": row.get("executed_exposure_account_currency"),
                "reason": row.get("last_exit_reason") or row.get("exit_reason"),
                "position_id": position_id,
            })
        else:
            issues_for_row: list[str] = []
            if row.get("account_currency") != snapshot["currency"]:
                issues_for_row.append("LEDGER_CURRENCY_NOT_VERIFIED")
            if not position_id or position_id in duplicate_ledger or position_id in duplicate_live:
                issues_for_row.append("POSITION_ID_MISSING_OR_AMBIGUOUS")
            if row.get("exit_trade_history_confirmed") is not True:
                issues_for_row.append("EXIT_TRADE_HISTORY_UNCONFIRMED")
            if row.get("exit_confirmation_basis") != "DEMO_TRADE_HISTORY_POSITION_ID":
                issues_for_row.append("EXIT_CONFIRMATION_BASIS_NOT_VERIFIED")
            if amount is None:
                issues_for_row.append("CLOSED_TRADE_PNL_UNAVAILABLE")
            if position_id in live_by_id:
                issues_for_row.append("POSITION_STILL_PRESENT_ON_READBACK")
            if not issues_for_row:
                issues_for_row.append("CLOSURE_REQUIRES_MANUAL_REVIEW")
            if amount is not None:
                contested_amounts.append(amount)
            contested_history.append({
                "symbol": str(row.get("symbol") or "—"),
                "instrument_id": row.get("instrument_id"),
                "asset_class": row.get("asset_class"),
                "realized_pnl": money(amount) if amount is not None else None,
                "closed_at": row.get("exit_broker_closed_at") or row.get("exit_confirmed_at"),
                "reason": row.get("last_exit_reason") or row.get("exit_reason"),
                "position_id": position_id,
                "audit_status": "CONTESTED",
                "exclusion_reasons": issues_for_row,
            })
    wins = [amount for amount in verified_realized if amount > 0]
    losses = [amount for amount in verified_realized if amount < 0]
    gross_wins = sum(wins, Decimal("0"))
    gross_losses = abs(sum(losses, Decimal("0")))
    trade_count = len(verified_realized)
    result.update({
        "status": "PARTIAL" if issues else "AVAILABLE",
        "closed_count": len(closed),
        "verified_closed_count": verified_closed,
        "open_ledger_count": len(open_records),
        "matched_open_count": matched_open,
        "unattributed_broker_positions": len(position_ids) - len(matched_ids),
        "manual_excluded_position_count": len({
            str(row.get("broker_position_id") or row.get("position_id") or "")
            for row in excluded_records
        }),
        "realized_pnl_known": money(realized),
        "realized_pnl": money(realized) if realized_complete else None,
        "unrealized_pnl": money(unrealized) if open_complete else None,
        "combined_pnl": money(realized + unrealized) if realized_complete and open_complete else None,
        "issues": sorted(issues),
        "realized_trade_count": trade_count,
        "realized_win_count": len(wins),
        "realized_loss_count": len(losses),
        "realized_flat_count": trade_count - len(wins) - len(losses),
        "realized_win_rate": str((Decimal(len(wins)) / Decimal(trade_count) * 100).quantize(Decimal("0.01"))) if trade_count else None,
        "realized_profit_factor": money(gross_wins / gross_losses) if gross_losses else None,
        "realized_average_win": money(gross_wins / Decimal(len(wins))) if wins else None,
        "realized_average_loss": money(gross_losses / Decimal(len(losses))) if losses else None,
        "realized_history": sorted(realized_history, key=lambda row: _history_date(row.get("closed_at")), reverse=True)[:50],
        "contested_history": sorted(contested_history, key=lambda row: _history_date(row.get("closed_at")), reverse=True)[:50],
        "order_history": list(reversed(order_history[-100:])),
        "certified_closed_count": verified_closed,
        "contested_closed_count": len(contested_history),
        "contested_pnl_known": money(sum(contested_amounts, Decimal("0"))) if contested_amounts else "0.00" if not contested_history else None,
        "certified_realized_pnl": money(realized),
        "adjusted_realized_pnl": money(realized),
        "adjusted_realized_trade_count": trade_count,
        "adjusted_realized_win_rate": str((Decimal(len(wins)) / Decimal(trade_count) * 100).quantize(Decimal("0.01"))) if trade_count else None,
        "adjusted_realized_profit_factor": money(gross_wins / gross_losses) if gross_losses else None,
        "closure_audit_status": (
            "ALL_CERTIFIED" if not contested_history and verified_closed == len(closed)
            else "PARTIAL_CERTIFICATION" if verified_closed or contested_history
            else "NO_CERTIFIED_CLOSURES"
        ),
        "closure_audit_note": (
            "Tutte le chiusure del registro hanno conferma broker e P/L riconciliato. Nessuna esclusione tecnica è stata applicata automaticamente."
            if not contested_history and verified_closed == len(closed)
            else "Il P/L ricalcolato usa soltanto le chiusure certificate; le chiusure contestate/non verificabili sono escluse."
        ),
        "evaluation": (
            "POSITIVE_BUT_PRELIMINARY"
            if trade_count >= 1 and gross_wins > gross_losses and trade_count < 30
            else "POSITIVE_PRELIMINARY"
            if trade_count >= 30 and gross_wins > gross_losses
            else "NOT_ENOUGH_VERIFIED_CLOSED_TRADES"
        ),
    })
    return result
