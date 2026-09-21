"""Current supported candidates from live search joined to the complete catalog."""

from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from time import sleep

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.mapping import (
    SAFE_CLASSIFICATION_FIELDS,
    EtoroMappingError,
    map_instrument_resolution,
)
from app.data.runtime import (
    _catalog_asset_class,
    _catalog_items,
    _catalog_value,
    _etoro_bootstrap_instrument,
    catalog_session_exclusion_reason,
    read_etoro_instrument_catalog_snapshot,
)
from app.domain.enums import MarketStatus
from app.domain.universe import UniversalInstrument
from app.orchestration.session_state import SESSION_TTL, session_state_for

_CRITICAL_FIELDS = (
    "internalSymbolFull",
    "displayname",
    *SAFE_CLASSIFICATION_FIELDS,
    "isOpen",
    "isExchangeOpen",
    "isCurrentlyTradable",
    "isBuyEnabled",
    "isInternalInstrument",
    "isHiddenFromClient",
    "isDelisted",
    "isActiveInPlatform",
    "currency",
    "currencyId",
    "isin",
)


def _identity_metadata(row: dict[str, object]) -> dict[str, object]:
    return {key: row[key] for key in _CRITICAL_FIELDS if row.get(key) is not None}


def _crypto_tradability_rejection(row: dict[str, object]) -> str | None:
    for field, expected in (
        ("isCurrentlyTradable", True),
        ("isBuyEnabled", True),
        ("isActiveInPlatform", True),
        ("isInternalInstrument", False),
        ("isHiddenFromClient", False),
        ("isDelisted", False),
    ):
        if row.get(field) is not expected:
            return f"CRYPTO_REQUIRES_{field}={str(expected).lower()}"
    return None


def current_catalog_candidates(
    client: EtoroReadClient,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    duplicate_diagnostics: list[dict[str, object]] | None = None,
    selection_diagnostics: dict[str, object] | None = None,
    session_diagnostics: list[dict[str, object]] | None = None,
    live_get_cap: int = 50,
    catalog_offset: int = 0,
    pacing_delay_seconds: float = 1.0,
    sleeper: Callable[[float], None] = sleep,
) -> tuple[UniversalInstrument, ...]:
    if not 1 <= live_get_cap <= 50:
        raise ValueError("LIVE_SELECTION_GET_CAP_MUST_BE_1_TO_50")
    if catalog_offset < 0:
        raise ValueError("LIVE_SELECTION_CATALOG_OFFSET_MUST_BE_NON_NEGATIVE")
    if not isfinite(pacing_delay_seconds) or pacing_delay_seconds < 1:
        raise ValueError("LIVE_SELECTION_PACING_MUST_BE_AT_LEAST_ONE_SECOND")
    snapshot = read_etoro_instrument_catalog_snapshot()
    if snapshot is None:
        raise ValueError("FULL_CATALOG_MISSING")
    raw_items = _catalog_items(snapshot.get("raw_response"))
    catalog = {
        _catalog_value(item, "instrumentID", "instrumentId"): item
        for item in raw_items
        if catalog_session_exclusion_reason(item) is None
    }
    prefilter_count = len(catalog)
    stats = selection_diagnostics if selection_diagnostics is not None else {}
    stats.update(
        {
            "catalog_count": len(
                {_catalog_value(x, "instrumentID", "instrumentId") for x in raw_items}
            ),
            "local_prefilter_count": prefilter_count,
            "live_get_count": 0,
            "live_get_cap": live_get_cap,
            "pacing_delay_seconds": pacing_delay_seconds,
            "cache_hits": 0,
            "http_429_count": 0,
            "retry_after_seconds": None,
            "live_open_tradable_count": 0,
            "catalog_offset": catalog_offset,
            "unchecked_due_to_cap_count": max(0, prefilter_count - catalog_offset - live_get_cap),
        }
    )
    seen: set[str] = set()
    metadata: dict[str, dict[str, object]] = {}
    sources: dict[str, list[dict[str, object]]] = {}
    reports: dict[str, dict[str, object]] = {}
    excluded_ids: set[str] = set()
    conflict_fields: dict[str, set[str]] = {}
    candidates: list[UniversalInstrument] = []
    # Crypto first, then numeric broker ID: stable and independent of old candidates.
    catalog_ids = sorted(
        catalog, key=lambda i: (_catalog_asset_class(catalog[i]) != "CRYPTO", int(i))
    )
    catalog_ids = catalog_ids[catalog_offset : catalog_offset + live_get_cap]
    batch_catalog_count = len(catalog_ids)
    batch_end_offset = catalog_offset + batch_catalog_count
    stats["batch_catalog_count"] = batch_catalog_count
    stats["batch_end_offset"] = batch_end_offset
    stats["catalog_exhausted"] = batch_end_offset >= prefilter_count
    for offset in range(len(catalog_ids)):
        requested_ids = catalog_ids[offset : offset + 1]
        session_report: dict[str, object] = {
            "symbol": _catalog_value(catalog[requested_ids[0]], "symbolFull"),
            "instrument_id": requested_ids[0],
            "asset_class": _catalog_asset_class(catalog[requested_ids[0]]),
            "endpoint": "/api/v1/market-data/search",
            "query_instrument_id": requested_ids[0],
            "http_status": None,
            "raw_session_fields": [],
            "open_tradable_result": False,
            "exact_rejection_condition": "NOT_OBSERVED",
        }
        if session_diagnostics is not None:
            session_diagnostics.append(session_report)
        if offset:
            sleeper(pacing_delay_seconds)
        stats["live_get_count"] = offset + 1
        try:
            raw = client.session_catalog_ids(tuple(int(i) for i in requested_ids))
        except EtoroApiError as exc:
            session_report.update(
                {
                    "http_status": exc.status,
                    "exact_rejection_condition": exc.transport_detail or exc.category.value,
                }
            )
            if exc.status == 429:
                stats["http_429_count"] = 1
                headers = {k.casefold(): v for k, v in exc.response_headers.items()}
                retry_after = headers.get("retry-after")
                if retry_after is not None:
                    try:
                        seconds = float(retry_after)
                    except ValueError:
                        try:
                            reference = (
                                parsedate_to_datetime(headers["date"])
                                if "date" in headers
                                else clock()
                            )
                            seconds = (
                                parsedate_to_datetime(retry_after) - reference
                            ).total_seconds()
                        except (ValueError, TypeError, KeyError, OverflowError):
                            seconds = float("nan")
                    if isfinite(seconds):
                        stats["retry_after_seconds"] = max(0.0, seconds)
                stats["status"] = "RATE_LIMITED_STOPPED_NO_RETRY"
            raise
        observed_at = clock()
        session_report["http_status"] = 200
        session_report["observed_at"] = observed_at.isoformat()
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            session_report["exact_rejection_condition"] = "LIVE_SEARCH_PAGE_INVALID"
            raise ValueError("LIVE_SEARCH_PAGE_INVALID")
        items = raw["items"]
        session_report["raw_session_fields"] = [
            {"instrumentId": row.get("instrumentId"), **_identity_metadata(row)}
            for row in items
            if isinstance(row, dict)
        ]
        session_report["exact_rejection_condition"] = "REQUESTED_INSTRUMENT_ID_NOT_RETURNED"
        for index, row in enumerate(items):
            if not isinstance(row, dict) or row.get("instrumentId") is None:
                raise ValueError("LIVE_SEARCH_ID_MISSING")
            iid = str(row["instrumentId"])
            if iid not in requested_ids:
                continue
            identity = _identity_metadata(row)
            source = {
                "endpoint": "/api/v1/market-data/search",
                "batch_number": offset + 1,
                "page_number": 1,
                "row_index": index,
                "instrument_id": iid,
                "metadata": identity,
            }
            if iid in seen:
                stats["cache_hits"] = int(str(stats["cache_hits"])) + 1
                conflicts = sorted(
                    key
                    for key in identity.keys() | metadata[iid].keys()
                    if (key.startswith("is") or key in identity.keys() & metadata[iid].keys())
                    and (
                        type(identity.get(key)) is not type(metadata[iid].get(key))
                        or identity.get(key) != metadata[iid].get(key)
                    )
                )
                sources[iid].append(source)
                if iid not in reports:
                    reports[iid] = {"duplicate_id": iid, "rows": sources[iid]}
                    if duplicate_diagnostics is not None:
                        duplicate_diagnostics.append(reports[iid])
                report = reports[iid]
                conflict_fields.setdefault(iid, set()).update(conflicts)
                report.update(
                    {
                        "occurrence_count": len(sources[iid]),
                        "duplicate_count": len(sources[iid]) - 1,
                        "classification": "CONFLICTING"
                        if conflict_fields[iid]
                        else (
                            "IDENTICAL"
                            if identity == metadata[iid]
                            and report.get("classification") != "COMPATIBLE"
                            else "COMPATIBLE"
                        ),
                        "conflicting_fields": sorted(conflict_fields[iid]),
                    }
                )
                if conflicts:
                    excluded_ids.add(iid)
                    report["action"] = "EXCLUDED_CONFLICTING_ID"
                    session_report["open_tradable_result"] = False
                    session_report["exact_rejection_condition"] = "CONFLICTING_DUPLICATE_ID"
                elif iid not in excluded_ids:
                    report["action"] = "DEDUPLICATED"
                elif not conflict_fields[iid]:
                    report["action"] = "EXCLUDED_UNUSABLE_ID"
                # Retain the first actual observation, including its timestamp.
                # Do not stitch missing session flags into synthetic OPEN evidence.
                metadata[iid].update(identity)
                continue
            seen.add(iid)
            metadata[iid] = identity
            sources[iid] = [source]
            for field in (
                "isOpen",
                "isExchangeOpen",
                "isCurrentlyTradable",
                "isBuyEnabled",
                "isActiveInPlatform",
                "isInternalInstrument",
            ):
                session_report[field] = row.get(field)
            if (
                row.get("isInternalInstrument") is True
                or row.get("isActiveInPlatform") is False
                or row.get("isCurrentlyTradable") is False
            ):
                excluded_ids.add(iid)
                session_report["exact_rejection_condition"] = (
                    "isInternalInstrument=true"
                    if row.get("isInternalInstrument") is True
                    else "isActiveInPlatform=false"
                    if row.get("isActiveInPlatform") is False
                    else "isCurrentlyTradable=false"
                )
                continue
            if iid not in catalog:
                continue
            symbol = _catalog_value(catalog[iid], "symbolFull")
            try:
                resolution = map_instrument_resolution(
                    {"items": [row]},
                    symbol=symbol,
                    as_of=observed_at,
                    expected_instrument_id=int(iid),
                )
            except (EtoroMappingError, ValueError) as exc:
                session_report["exact_rejection_condition"] = "SESSION_MAPPING_ERROR"
                session_report["mapping_exception_type"] = type(exc).__name__
                continue
            if _catalog_asset_class(catalog[iid]) == "CRYPTO":
                crypto_rejection = _crypto_tradability_rejection(row)
                if crypto_rejection is not None:
                    session_report["exact_rejection_condition"] = crypto_rejection
                    continue
                session = "OPEN_TRADABLE"
            else:
                session = session_state_for(
                    market_status=resolution.market_status,
                    tradable=resolution.is_currently_tradable,
                )
            if (
                resolution.is_internal_instrument is True
                or not resolution.structurally_supported
                or resolution.is_buy_enabled is not True
                or session != "OPEN_TRADABLE"
            ):
                session_report["exact_rejection_condition"] = (
                    "isInternalInstrument=true"
                    if resolution.is_internal_instrument is True
                    else "STRUCTURALLY_UNSUPPORTED"
                    if not resolution.structurally_supported
                    else "isBuyEnabled!=true"
                    if resolution.is_buy_enabled is not True
                    else "SESSION_STATE=" + session
                )
                continue
            session_report["open_tradable_result"] = True
            session_report["exact_rejection_condition"] = None
            instrument = _etoro_bootstrap_instrument(
                catalog[iid], snapshot_id=str(snapshot["snapshot_id"]), now=observed_at
            )
            candidates.append(
                instrument.model_copy(
                    update={
                        "market_status": MarketStatus.OPEN,
                        "tradeable": True,
                        "buy_allowed": True,
                        "tags": (*instrument.tags, "session-state:OPEN_TRADABLE"),
                    }
                )
            )
    candidates = [x for x in candidates if x.broker_instrument_id not in excluded_ids]
    now = clock()
    if any(
        not 0 <= (now - x.metadata_timestamp).total_seconds() <= SESSION_TTL.total_seconds()
        for x in candidates
    ):
        raise ValueError("LIVE_SEARCH_SESSION_EVIDENCE_EXPIRED")
    stats["live_open_tradable_count"] = len(candidates)
    stats["status"] = "BOUNDED_SELECTION_COMPLETE"
    return tuple(sorted(candidates, key=lambda x: (x.symbol, x.broker_instrument_id)))
