"""Authenticated, bounded relay for secondary-PC news evidence.

The relay carries sanitized candidate context only. It never carries broker
credentials, positions, orders, or a decision, and it cannot authorize an
execution by itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RELAY_VERSION = "aegis-news-relay-v1"
MAX_RELAY_BYTES = 262_144
MAX_CONTEXTS = 96
MAX_RELAY_AGE = timedelta(minutes=15)
RELAY_TTL = timedelta(minutes=10)
DEFAULT_RELAY_STORE = Path("work") / "secondary-news-relay.json"
_SOURCE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CONTEXT_FIELDS = {
    "asset_class",
    "freshness",
    "aggregate_sentiment",
    "aggregate_relevance",
    "aggregate_confidence",
    "aggregate_source_reliability",
    "aggregate_impact",
    "event_risk",
    "conflicting_news",
    "unique_event_count",
    "material_event_count",
    "latest_material_event_timestamp",
    "event_summaries",
    "news_risk_flags",
    "explanation",
    "as_of",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        raise ValueError("news relay redirect refused")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("relay time must include timezone")
    return current.astimezone(UTC)


def relay_key(values: Mapping[str, str] | None) -> bytes | None:
    raw = (values or {}).get("AEGIS_SECONDARY_NEWS_RELAY_KEY", "").strip()
    if len(raw) != 64:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def relay_url(values: Mapping[str, str] | None) -> str | None:
    raw = (values or {}).get("AEGIS_SECONDARY_NEWS_RELAY_URL", "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return raw.rstrip("/")


def relay_store_path(values: Mapping[str, str] | None) -> Path:
    raw = (values or {}).get("AEGIS_SECONDARY_NEWS_RELAY_STORE", "").strip()
    return Path(raw) if raw else DEFAULT_RELAY_STORE


def _safe_context(context: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _CONTEXT_FIELDS:
        value = context.get(key)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
        elif isinstance(value, (list, tuple)):
            result[key] = [str(item) for item in value[:8]]
    return result


def _cycle_dump(cycle: Any) -> dict[str, Any]:
    if hasattr(cycle, "model_dump"):
        dumped = cycle.model_dump(mode="json")
    elif isinstance(cycle, Mapping):
        dumped = dict(cycle)
    else:
        raise ValueError("unsupported cycle payload")
    if not isinstance(dumped, dict):
        raise ValueError("cycle payload must be an object")
    return dumped


def build_envelope(cycle: Any, *, key: bytes, source_id: str, now: datetime | None = None) -> dict[str, Any]:
    if len(key) != 32 or not _SOURCE_RE.fullmatch(source_id):
        raise ValueError("invalid relay key or source")
    current = _now(now)
    dumped = _cycle_dump(cycle)
    raw_contexts = dumped.get("news_asset_contexts", {})
    if not isinstance(raw_contexts, Mapping):
        raw_contexts = {}
    contexts: dict[str, dict[str, Any]] = {}
    for symbol, raw in raw_contexts.items():
        if not isinstance(raw, Mapping):
            continue
        context = _safe_context(raw)
        if context.get("freshness") not in {"NEWS_FRESH", "NEWS_DELAYED"}:
            continue
        if not context.get("latest_material_event_timestamp"):
            continue
        contexts[str(symbol)[:64]] = context
    if len(contexts) > MAX_CONTEXTS:
        contexts = dict(sorted(contexts.items(), key=lambda item: int(item[1].get("material_event_count", 0)), reverse=True)[:MAX_CONTEXTS])
    body = {
        "version": RELAY_VERSION,
        "source_id": source_id,
        "generated_at": current.isoformat(),
        "as_of": str(dumped.get("news_cutoff_timestamp") or dumped.get("scheduled_at") or current.isoformat()),
        "expires_at": (current + RELAY_TTL).isoformat(),
        "provider": str(dumped.get("news_provider") or "unknown")[:80],
        "provider_status": str(dumped.get("news_provider_status") or "unknown")[:40],
        "events_received": int(dumped.get("news_events_received") or 0),
        "events_fresh": int(dumped.get("news_events_fresh") or 0),
        "events_material": int(dumped.get("news_events_material") or 0),
        "contexts": contexts,
    }
    return {"body": body, "signature": hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()}


def verify_envelope(envelope: Any, *, key: bytes, now: datetime | None = None) -> dict[str, Any]:
    if len(key) != 32 or not isinstance(envelope, Mapping) or set(envelope) != {"body", "signature"}:
        raise ValueError("invalid relay envelope")
    body = envelope.get("body")
    signature = envelope.get("signature")
    if not isinstance(body, Mapping) or not isinstance(signature, str):
        raise ValueError("invalid relay body")
    expected = hmac.new(key, _canonical(dict(body)), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("relay signature mismatch")
    required = {"version", "source_id", "generated_at", "as_of", "expires_at", "provider", "provider_status", "events_received", "events_fresh", "events_material", "contexts"}
    if set(body) != required or body["version"] != RELAY_VERSION or not _SOURCE_RE.fullmatch(str(body["source_id"])):
        raise ValueError("invalid relay fields")
    current = _now(now)
    generated = datetime.fromisoformat(str(body["generated_at"])).astimezone(UTC)
    as_of = datetime.fromisoformat(str(body["as_of"])).astimezone(UTC)
    expires = datetime.fromisoformat(str(body["expires_at"])).astimezone(UTC)
    if generated > current + timedelta(seconds=60) or current - generated > MAX_RELAY_AGE or as_of > current + timedelta(seconds=60) or expires <= current or expires > current + MAX_RELAY_AGE:
        raise ValueError("stale or invalid relay timestamps")
    contexts = body["contexts"]
    if not isinstance(contexts, Mapping) or len(contexts) > MAX_CONTEXTS:
        raise ValueError("invalid relay contexts")
    return dict(body)


def store_envelope(envelope: Mapping[str, Any], *, key: bytes, path: Path = DEFAULT_RELAY_STORE, now: datetime | None = None) -> dict[str, Any]:
    body = verify_envelope(envelope, key=key, now=now)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"envelope": dict(envelope), "received_at": _now(now).isoformat()}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        temporary = Path(handle.name)
    temporary.replace(path)
    return body


def read_latest(*, key: bytes, path: Path = DEFAULT_RELAY_STORE, now: datetime | None = None) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return verify_envelope(payload["envelope"], key=key, now=now)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def publish(cycle: Any, values: Mapping[str, str], *, now: datetime | None = None) -> dict[str, Any]:
    key = relay_key(values)
    url = relay_url(values)
    if key is None or url is None:
        return {"status": "NOT_CONFIGURED"}
    source_id = values.get("AEGIS_SECONDARY_NEWS_SOURCE_ID", "secondary-pc").strip() or "secondary-pc"
    try:
        envelope = build_envelope(cycle, key=key, source_id=source_id, now=now)
        data = _canonical(envelope)
        if len(data) > MAX_RELAY_BYTES:
            return {"status": "PAYLOAD_TOO_LARGE"}
        request = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "X-Aegis-News-Relay-Signature": envelope["signature"]},
            method="POST",
        )
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                return {"status": "HTTP_ERROR", "http_status": response.status}
        return {"status": "SENT", "contexts": len(envelope["body"]["contexts"])}
    except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError):
        return {"status": "CONNECTION_OR_VALIDATION_FAILED"}
