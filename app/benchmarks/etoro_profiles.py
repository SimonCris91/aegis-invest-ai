"""Bounded, sanitized eToro public-profile benchmark reads.

This module is deliberately separate from scanner selection, RiskManager, and
all broker write paths. A failed public-profile read changes only benchmark
display state.
"""

from __future__ import annotations

from threading import Lock
from time import monotonic
from typing import Any

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient

BENCHMARK_PROFILES: tuple[dict[str, str], ...] = (
    {"username": "TradingInvest890", "owner": "Massimiliano Spallanzani"},
    {"username": "ThomasPJ", "owner": "Thomas Parry Jones"},
    {"username": "RainbirdFX", "owner": "Profilo eToro multi-strategy"},
    {"username": "SimoneRizzetto88", "owner": "Profilo eToro"},
    {"username": "Sergius95", "owner": "Profilo eToro"},
    {"username": "iBore99", "owner": "Profilo eToro"},
    {"username": "pino428", "owner": "Profilo eToro"},
    {"username": "Kevin_Pando", "owner": "Profilo eToro"},
)
_CACHE_TTL_SECONDS = 900.0
_cache_lock = Lock()
_cache: tuple[float, dict[str, object]] | None = None


def read_etoro_benchmarks(client: EtoroReadClient | None) -> dict[str, object]:
    global _cache
    # Never serve a previously authenticated snapshot after credentials are
    # absent or the API is disabled.
    if client is None:
        with _cache_lock:
            _cache = None
        return _unavailable("NOT_CONFIGURED", "Credenziali eToro non configurate.")
    now = monotonic()
    with _cache_lock:
        if _cache is not None and now - _cache[0] < _CACHE_TTL_SECONDS:
            return _cache[1]
    result = {
        "status": "AVAILABLE",
        "source": "ETORO_PUBLIC_API_READ_ONLY",
        "profiles": [_read_profile(client, item) for item in BENCHMARK_PROFILES],
    }
    with _cache_lock:
        _cache = (now, result)
    return result


def _read_profile(client: EtoroReadClient, profile: dict[str, str]) -> dict[str, object]:
    username = profile["username"]
    result: dict[str, object] = {
        "username": username,
        "owner": profile["owner"],
        "href": f"https://www.etoro.com/people/{username}/stats",
        "status": "UNAVAILABLE",
        "source": "ETORO_PUBLIC_API",
    }
    try:
        gains = client.public_portfolio_gain(username, granularity="monthly", count=60)
        copiers = client.public_portfolio_copiers(username)
        assets = client.public_portfolio_assets(username, period="LastTwoYears")
    except EtoroApiError as exc:
        result["source"] = "ETORO_PUBLIC_API"
        result["diagnostics"] = exc.safe_metadata()
        result["message"] = _safe_status_message(exc)
        return result
    except (RuntimeError, ValueError) as exc:
        result["message"] = type(exc).__name__
        return result
    result.update(_sanitize_gain(gains))
    result.update(_sanitize_copiers(copiers))
    result.update(_sanitize_assets(assets))
    result["status"] = "AVAILABLE"
    return result


def _safe_status_message(exc: EtoroApiError) -> str:
    if exc.status in {401, 403}:
        return "API eToro non autorizzata per questo dato pubblico."
    if exc.status == 429:
        return "Limite richieste eToro raggiunto; riproverò dopo la cache."
    return "Dati pubblici eToro non disponibili."


def _unavailable(status: str, message: str) -> dict[str, object]:
    return {
        "status": status,
        "source": "ETORO_PUBLIC_API_READ_ONLY",
        "message": message,
        "profiles": [
            {
                **profile,
                "href": f"https://www.etoro.com/people/{profile['username']}/stats",
                "status": "NOT_VERIFIED",
                "source": "DA_VERIFICARE",
            }
            for profile in BENCHMARK_PROFILES
        ],
    }


def _sanitize_gain(raw: object) -> dict[str, object]:
    rows = _rows(raw)
    if isinstance(raw, dict) and isinstance(raw.get("gains"), list):
        rows = [row for row in raw["gains"] if isinstance(row, dict)]
    points: list[dict[str, object]] = []
    for row in rows[-60:]:
        date = _first(row, "date", "timestamp", "period", "periodStart")
        gain = _number(row, "gain", "return", "value", "percentage")
        if date is not None and gain is not None:
            # eToro returns gain as a ratio (0.0378 = 3.78%). The UI exposes
            # percentages, while preserving the raw source only implicitly.
            points.append({"date": str(date), "gain": gain * 100})
    return {"monthly_gain": points, "latest_monthly_gain": points[-1] if points else None}


def _sanitize_copiers(raw: object) -> dict[str, object]:
    rows = _rows(raw)
    row = rows[-1] if rows else (raw if isinstance(raw, dict) else {})
    copiers = _number(row, "copiers", "copierCount", "copiersCount", "numberOfCopiers")
    risk = _number(row, "riskScore", "risk", "risk_score")
    result: dict[str, object] = {"copiers": copiers, "risk_score": risk}
    return result


def _sanitize_assets(raw: object) -> dict[str, object]:
    rows = _rows(raw)
    latest = rows[-1] if rows else {}
    assets = latest.get("assets") if isinstance(latest.get("assets"), list) else rows
    clean: list[dict[str, object]] = []
    for item in assets[:10] if isinstance(assets, list) else []:
        if not isinstance(item, dict):
            continue
        symbol = _first(item, "symbol", "instrument", "asset", "ticker")
        share = _number(item, "valuePct", "valuePercentage", "percentage", "weight")
        if symbol is not None:
            clean.append({"symbol": str(symbol), "weight": share})
    return {"top_assets": clean, "assets_as_of": _first(latest, "date", "timestamp", "period")}


def _rows(raw: object) -> list[dict[str, object]]:
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("data", "items", "history", "results", "points", "assets"):
        value = raw.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return [raw]


def _first(row: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _number(row: dict[str, object], *keys: str) -> float | None:
    value = _first(row, *keys)
    try:
        number = float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None
