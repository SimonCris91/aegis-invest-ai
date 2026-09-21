"""Authoritative, broker-sourced session and tradability observations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.mapping import EtoroMappingError
from app.domain.enums import AssetClass, MarketStatus
from app.domain.universe import UniversalInstrument
from app.storage.sqlite import SqliteRecordStore

SESSION_SOURCE = "etoro-market-data-search"
SESSION_TTL = timedelta(minutes=15)
RETRYABLE_SESSION_ERRORS = {
    "NETWORK_TRANSPORT_ERROR",
    "AUTH_API_PERMISSION_ERROR",
    "EDGE_WAF_BLOCK",
    "HTTP_ERROR",
}


def partition_runtime_instruments(
    instruments: tuple[UniversalInstrument, ...],
) -> tuple[tuple[UniversalInstrument, ...], tuple[UniversalInstrument, ...]]:
    """Exclude only catalog-proven internal records, never unresolved assets."""
    internal = tuple(item for item in instruments if "unsupported-internal" in item.tags)
    supported = tuple(item for item in instruments if "unsupported-internal" not in item.tags)
    return supported, internal


def session_state_for(*, market_status: MarketStatus, tradable: bool | None) -> str:
    """Apply explicit precedence; missing evidence never becomes CLOSED."""
    if market_status is MarketStatus.CLOSED:
        return "CLOSED"
    if market_status is not MarketStatus.OPEN:
        return "UNKNOWN"
    if tradable is True:
        return "OPEN_TRADABLE"
    if tradable is False:
        return "OPEN_NOT_TRADABLE"
    return "UNKNOWN"


def partition_temporarily_observed_dash(
    instruments: tuple[UniversalInstrument, ...],
    *,
    states: dict[str, dict[str, object]],
    as_of: datetime,
) -> tuple[tuple[UniversalInstrument, ...], tuple[UniversalInstrument, ...]]:
    """User-authorized DASH buy hold; refresh the full universe before calling.

    This does not remove catalog records or affect management of existing
    positions. Missing, expired or uncertain evidence is never an exclusion.
    """
    included, observed = [], []
    for item in instruments:
        state = states.get(item.broker_instrument_id, {})
        fresh = False
        try:
            seen = datetime.fromisoformat(str(state.get("observed_at")))
            expiry = datetime.fromisoformat(str(state.get("expires_at")))
            fresh = seen <= as_of < expiry
        except (ValueError, TypeError):
            pass
        hold = (
            item.broker == "etoro" and item.broker_instrument_id == "100004"
            and item.symbol == "DASH" and item.asset_class is AssetClass.CRYPTO
            and fresh and state.get("source") == SESSION_SOURCE
            and state.get("is_buy_enabled") is False
            and state.get("error_class") == "CRYPTO_BUY_DISABLED"
        )
        (observed if hold else included).append(item)
    return tuple(included), tuple(observed)


def enrich_instrument_session_state(
    *,
    client: EtoroReadClient,
    store: SqliteRecordStore,
    instruments: tuple[UniversalInstrument, ...],
    as_of: datetime,
    concurrency: int = 4,
    force_refresh: bool = False,
    batch_size: int | None = None,
    refresh_ahead: timedelta = timedelta(0),
) -> tuple[UniversalInstrument, ...]:
    """Refresh expired observations and apply current persisted evidence."""
    if as_of.tzinfo is None:
        raise ValueError("session enrichment timestamp must be timezone-aware")
    if batch_size is not None and batch_size <= 0:
        raise ValueError("session batch size must be positive")
    if not timedelta(0) <= refresh_ahead < SESSION_TTL:
        raise ValueError("refresh_ahead must be nonnegative and shorter than session TTL")
    prior = store.etoro_session_states(as_of=as_of)
    ordered = tuple(sorted(instruments, key=lambda item: (item.symbol, item.key)))
    refresh = tuple(
        item
        for item in ordered
        if item.broker == "etoro"
        and (
            item.broker_instrument_id not in prior
            or force_refresh
            or _is_retryable_error(prior[item.broker_instrument_id].get("error_class"))
            or datetime.fromisoformat(str(prior[item.broker_instrument_id]["expires_at"])) <= as_of + refresh_ahead
        )
    )
    # Missing evidence first, then nearest expiry, rather than ticker order.
    # Renewal does not extend the validity of the old observation.
    refresh = tuple(sorted(refresh, key=lambda item: (
        str(prior.get(item.broker_instrument_id, {}).get("expires_at", "")),
        item.symbol, item.key,
    )))

    def fetch(item: UniversalInstrument) -> dict[str, object]:
        try:
            instrument_id = int(item.broker_instrument_id)
            resolve_by_id = getattr(client, "resolve_session_instrument_id", None)
            if not callable(resolve_by_id):
                resolve_by_id = getattr(client, "resolve_instrument_id", None)
            if callable(resolve_by_id):
                resolution = resolve_by_id(instrument_id, symbol=item.symbol, as_of=as_of)
            else:
                resolution = client.resolve_instrument(item.symbol, as_of=as_of)
            metadata = resolution.classification_metadata
            exchange_id = _metadata_value(metadata, "exchangeID", "exchangeId")
            market_status = resolution.market_status
            if item.asset_class is AssetClass.CRYPTO:
                from app.brokers.etoro.demo_preflight import _preflight_market_status

                market_status = _preflight_market_status(resolution)
            tradable = resolution.is_currently_tradable if resolution.structurally_supported else False
            state = session_state_for(
                market_status=market_status,
                tradable=tradable,
            )
            return {
                "instrument_id": item.broker_instrument_id,
                "exchange_id": exchange_id,
                "is_exchange_open": resolution.is_exchange_open,
                "is_open": resolution.is_open,
                "is_currently_tradable": tradable,
                "is_buy_enabled": resolution.is_buy_enabled if resolution.structurally_supported else False,
                "session_state": state,
                "observed_at": as_of.isoformat(),
                "source": SESSION_SOURCE,
                "expires_at": (as_of + SESSION_TTL).isoformat(),
                "error_class": (
                    "CRYPTO_BUY_DISABLED"
                    if item.asset_class is AssetClass.CRYPTO
                    and resolution.structurally_supported
                    and resolution.is_buy_enabled is False
                    else
                    "CRYPTO_TRADABILITY_NOT_CONFIRMED"
                    if item.asset_class is AssetClass.CRYPTO and market_status is MarketStatus.UNKNOWN
                    else _unknown_resolution_reason(resolution)
                ),
            }
        except EtoroMappingError as exc:
            return _error_state(
                item, as_of, "NO_MATCH" if "not resolved" in str(exc) else "PARSE_ERROR"
            )
        except EtoroApiError as exc:
            # A rate limit must wait for the cached observation to expire;
            # generic HTTP errors are otherwise retried on every poll.
            error_class = "RATE_LIMITED" if exc.status == 429 else exc.category.value
            return _error_state(item, as_of, error_class)
        except (ValueError, TypeError, KeyError) as exc:
            return _error_state(item, as_of, type(exc).__name__)

    # A provider throttle applies to the session request stream, not only
    # the instrument that happened to receive it. Reuse its existing TTL.
    cooling_down = any(state.get("error_class") == "RATE_LIMITED" for state in prior.values())
    if refresh and not cooling_down:
        # Persist bounded tranches so one slow response cannot withhold all
        # completed observations, and stop scheduling requests after a 429.
        selected = refresh[:batch_size]
        workers = max(1, min(concurrency, len(selected)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0, len(selected), workers):
                states = tuple(pool.map(fetch, selected[offset:offset + workers]))
                for state in states:
                    store.upsert_etoro_session_state(state=state)
                if any(state.get("error_class") == "RATE_LIMITED" for state in states):
                    break

    current = store.etoro_session_states(as_of=as_of)
    return tuple(_apply_state(item, current.get(item.broker_instrument_id)) for item in ordered)


def _error_state(item: UniversalInstrument, as_of: datetime, error_class: str) -> dict[str, object]:
    return {
        "instrument_id": item.broker_instrument_id,
        "exchange_id": item.exchange,
        "is_exchange_open": None,
        "is_open": None,
        "is_currently_tradable": None,
        "is_buy_enabled": None,
        "session_state": "UNKNOWN",
        "observed_at": as_of.isoformat(),
        "source": SESSION_SOURCE,
        "expires_at": (as_of + SESSION_TTL).isoformat(),
        "error_class": error_class,
    }


def _apply_state(item: UniversalInstrument, state: dict[str, object] | None) -> UniversalInstrument:
    if state is None:
        return item
    session = str(state["session_state"])
    status = (
        MarketStatus.OPEN
        if session in {"OPEN_TRADABLE", "OPEN_NOT_TRADABLE"}
        else MarketStatus.CLOSED
        if session == "CLOSED"
        else MarketStatus.UNKNOWN
    )
    return item.model_copy(
        update={
            "market_status": status,
            "tradeable": state.get("is_currently_tradable"),
            "buy_allowed": state.get("is_buy_enabled"),
            "exchange": state.get("exchange_id") or item.exchange,
            "metadata_timestamp": datetime.fromisoformat(str(state["observed_at"])),
            "tags": tuple(dict.fromkeys((*item.tags, f"session-state:{session}"))),
        }
    )


def _is_retryable_error(error_class: object) -> bool:
    return str(error_class) in RETRYABLE_SESSION_ERRORS or str(error_class) == "TIMEOUT"


def _unknown_resolution_reason(resolution: object) -> str | None:
    if getattr(resolution, "is_delisted", None) is True:
        return "DELISTED"
    if getattr(resolution, "is_internal_instrument", None) is True:
        return "UNSUPPORTED_INTERNAL_INSTRUMENT"
    market_status = getattr(resolution, "market_status", MarketStatus.UNKNOWN)
    # eToro may omit the optional per-instrument flag while its exchange
    # status still gives a deterministic market-state answer.
    if market_status is not MarketStatus.UNKNOWN:
        return None
    missing = tuple(
        name
        for name, value in (
            ("IS_EXCHANGE_OPEN", getattr(resolution, "is_exchange_open", None)),
            ("IS_OPEN", getattr(resolution, "is_open", None)),
            ("IS_CURRENTLY_TRADABLE", getattr(resolution, "is_currently_tradable", None)),
            ("IS_BUY_ENABLED", getattr(resolution, "is_buy_enabled", None)),
        )
        if value is None
    )
    if missing:
        return "MISSING_" + "_AND_".join(missing)
    return "MISSING_SESSION_FIELDS"


def session_state_reconciliation(
    *,
    instruments: tuple[UniversalInstrument, ...],
    states: dict[str, dict[str, object]],
    as_of: datetime,
) -> tuple[dict[str, int], dict[str, int]]:
    """Return exact state and UNKNOWN-cause counts for the requested universe."""
    state_counts: dict[str, int] = {}
    unknown_reasons: dict[str, int] = {}
    for instrument in instruments:
        state = states.get(instrument.broker_instrument_id)
        session = "UNKNOWN" if state is None else str(state.get("session_state", "UNKNOWN"))
        if session == "UNKNOWN" and "unsupported-internal" in instrument.tags:
            state_counts["UNSUPPORTED_INTERNAL"] = state_counts.get("UNSUPPORTED_INTERNAL", 0) + 1
            continue
        state_counts[session] = state_counts.get(session, 0) + 1
        if session != "UNKNOWN":
            continue
        reason = (
            "STALE_OR_NOT_OBSERVED"
            if state is None
            else str(state.get("error_class") or "UNKNOWN_OTHER")
        )
        if state is not None and datetime.fromisoformat(str(state["expires_at"])) <= as_of:
            reason = "STALE_EXPIRED_STATE"
        unknown_reasons[reason] = unknown_reasons.get(reason, 0) + 1
    return state_counts, unknown_reasons


def _metadata_value(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None
