"""Small, closed, credential-free analysis contract for a second computer."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import UTC, datetime
from typing import Any

VERSION = "ohlcv-shadow-v1"
MAX_BYTES = 512_000
CLASSES = {"EQUITY", "ETF", "CRYPTO", "FOREX", "COMMODITY", "INDEX"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed.astimezone(UTC)


def validate_payload(payload: dict[str, Any]) -> None:
    if set(payload) != {"symbol", "asset_class", "as_of", "bars"}:
        raise ValueError("unknown or missing analysis fields")
    if not isinstance(payload["symbol"], str) or not 1 <= len(payload["symbol"]) <= 40:
        raise ValueError("invalid symbol")
    if payload["asset_class"] not in CLASSES:
        raise ValueError("unsupported asset class")
    as_of = timestamp(payload["as_of"])
    bars = payload["bars"]
    if not isinstance(bars, list) or not 21 <= len(bars) <= 2048:
        raise ValueError("expected 21 to 2048 completed bars")
    previous = None
    for bar in bars:
        if not isinstance(bar, dict) or set(bar) != {"closed_at", "close"}:
            raise ValueError("only completed close observations are allowed")
        closed = timestamp(bar["closed_at"])
        price = bar["close"]
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError("invalid close price")
        if not math.isfinite(price) or price <= 0:
            raise ValueError("invalid close price")
        if closed > as_of or (previous is not None and closed <= previous):
            raise ValueError("future or unordered observation")
        previous = closed
    if len(canonical(payload)) > MAX_BYTES:
        raise ValueError("analysis payload too large")


def calculate(payload: dict[str, Any]) -> dict[str, Any]:
    validate_payload(payload)
    closes = [bar["close"] for bar in payload["bars"]]
    returns = [current / prior - 1 for prior, current in zip(closes[:-1], closes[1:], strict=True)]
    result = {
        "bars": len(closes),
        "momentum_5": closes[-1] / closes[-6] - 1,
        "momentum_20": closes[-1] / closes[-21] - 1,
        "sma_20": sum(closes[-20:]) / 20,
        "rms_return": math.sqrt(sum(value * value for value in returns) / len(returns)),
    }
    if any(not math.isfinite(value) for value in result.values()):
        raise ValueError("nonfinite analysis result")
    return result


def sign(value: dict[str, Any], key: bytes) -> dict[str, Any]:
    return {"body": value, "signature": hmac.new(key, canonical(value), hashlib.sha256).hexdigest()}


def verify(envelope: dict[str, Any], key: bytes) -> dict[str, Any]:
    if set(envelope) != {"body", "signature"} or not isinstance(envelope["body"], dict):
        raise ValueError("invalid signed envelope")
    expected = sign(envelope["body"], key)["signature"]
    if not isinstance(envelope["signature"], str) or not hmac.compare_digest(
        envelope["signature"], expected
    ):
        raise ValueError("signature mismatch")
    return envelope["body"]
