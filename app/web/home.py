"""Typed Home snapshot adapter; strategy and broker layers stay behind this boundary."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WatchlistAsset(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    score: Decimal | None = None
    rank: int | None = None
    reasons: tuple[str, ...] = ()


class PositionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    value: Decimal | None = None
    state: str


class CapitalSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal | None = None
    currency: str | None = None
    mode: Literal["SIMULATED", "PAPER", "LIVE", "UNKNOWN"] = "UNKNOWN"


class ScannerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    isolation_status: str
    assets_scanned: int | None = Field(default=None, ge=0)
    assets_comparable: int | None = Field(default=None, ge=0)
    top_opportunities: int | None = Field(default=None, ge=0)
    watchlist_count: int | None = Field(default=None, ge=0)
    no_trade_count: int | None = Field(default=None, ge=0)


class PositionsSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    open_count: int | None = Field(default=None, ge=0)
    items: tuple[PositionSummary, ...] = ()


class SafetySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_mode: Literal["READ_ONLY", "PAPER", "LIVE", "UNKNOWN"]
    broker_write_calls: int | None = Field(default=None, ge=0)


class DataHealthSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    one_hour_status: Literal["READY", "DEGRADED", "MISSING", "UNKNOWN"]
    last_scan_at: datetime | None = None
    backend_status: Literal["READY", "DEGRADED", "ERROR", "UNKNOWN"]


class AegisHomeSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: datetime
    capital: CapitalSnapshot
    scanner: ScannerSnapshot
    watchlist: tuple[WatchlistAsset, ...]
    positions: PositionsSnapshot
    safety: SafetySnapshot
    data_health: DataHealthSnapshot


def home_snapshot_from_scan_cycle(report: dict[str, object]) -> AegisHomeSnapshot:
    """Map the existing read-only scanner report into the UI contract."""
    status = str(report.get("status", "UNKNOWN"))
    scanner_output = _mapping(report.get("scanner_output"))
    watchlist_rows = _sequence(report.get("watchlist"))
    positions = _sequence(report.get("positions_to_manage"))
    as_of = _parse_datetime(report.get("scan_cycle_timestamp"))
    comparable = _optional_int(report.get("assets_comparable"))
    scanned = _optional_int(report.get("assets_requested"))
    backend_status = "READY" if status == "READ_ONLY_ACTIVE_SCAN_CYCLE_READY" else "DEGRADED"
    one_hour_status = "READY" if comparable is not None and comparable == scanned else "DEGRADED"
    return AegisHomeSnapshot(
        as_of=as_of,
        capital=CapitalSnapshot(
            amount=_optional_decimal(scanner_output.get("simulated_capital")),
            currency="EUR",
            mode="SIMULATED",
        ),
        scanner=ScannerSnapshot(
            status=status,
            isolation_status="MULTI_CYCLE_ISOLATION_VERIFIED",
            assets_scanned=scanned,
            assets_comparable=comparable,
            top_opportunities=(
                _optional_int(report.get("top_opportunities_count"), default=0)
                if "top_opportunities_count" in report
                else len(_sequence(report.get("top_opportunities")))
            ),
            watchlist_count=(
                _optional_int(report.get("watchlist_count"), default=0)
                if "watchlist_count" in report
                else len(watchlist_rows)
            ),
            no_trade_count=(
                _optional_int(report.get("no_trade_count"), default=0)
                if "no_trade_count" in report
                else len(_sequence(report.get("no_trade")))
            ),
        ),
        watchlist=tuple(
            WatchlistAsset(
                symbol=str(row.get("symbol", "")),
                name=str(row.get("full_asset_name", row.get("symbol", ""))),
                score=_optional_decimal(row.get("opportunity_score")),
                rank=_optional_int(row.get("rank")),
                reasons=_strings(row.get("rejection_reasons")),
            )
            for row in watchlist_rows
        ),
        positions=PositionsSnapshot(
            open_count=len(positions),
            items=tuple(
                PositionSummary(
                    symbol=str(row.get("symbol", "")),
                    value=_optional_decimal(row.get("current_price")),
                    state=str(row.get("existing_position_state", "UNKNOWN")),
                )
                for row in positions
            ),
        ),
        safety=SafetySnapshot(execution_mode="READ_ONLY", broker_write_calls=0),
        data_health=DataHealthSnapshot(
            one_hour_status=one_hour_status,
            last_scan_at=as_of,
            backend_status=backend_status,
        ),
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _sequence(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(str(item) for item in value)


def _optional_int(value: object, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("Home snapshot requires a causal scan timestamp")
