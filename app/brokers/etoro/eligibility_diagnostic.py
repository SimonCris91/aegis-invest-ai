"""Bounded GET-only eligibility diagnostics, isolated from execution."""

import json
from collections.abc import Mapping
from time import sleep

from app.brokers.etoro.client import BASE, DEMO_ELIGIBILITY_PATH, EtoroReadClient
from app.brokers.etoro.demo_preflight import _eligibility_blockers
from app.brokers.etoro.http import (
    DisciplinedHttpClient,
    HttpResponse,
    HttpTransport,
    UrllibTransport,
)
from app.brokers.etoro.live_candidates import current_catalog_candidates
from app.brokers.etoro.mapping import map_demo_eligibility
from app.brokers.etoro.runtime import runtime_credentials
from app.config.models import ApplicationConfig
from app.domain.enums import OperatingMode


class GetOnlyTransport:
    def __init__(self, transport: HttpTransport) -> None:
        self.transport = transport

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse:
        if method != "GET" or body is not None:
            raise ValueError("diagnostic permits GET without body only")
        return self.transport.request(method, url, headers)


class EligibilityDiagnosticTransport(GetOnlyTransport):
    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse:
        if method == "POST" and url == BASE + DEMO_ELIGIBILITY_PATH and body is not None:
            return self.transport.request(method, url, headers, body)
        return super().request(method, url, headers, body)


def sanitize(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if any(
                part in str(key).lower().replace("_", "").replace("-", "")
                for part in (
                    "apikey",
                    "userkey",
                    "token",
                    "secret",
                    "password",
                    "authorization",
                    "cookie",
                )
            )
            else sanitize(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def inspect_payload(raw: object, instrument_id: int, symbol: str) -> dict[str, object]:
    data = raw if isinstance(raw, dict) else {}
    items = data.get("eligibilities", [])
    items = items if isinstance(items, list) else []
    item = next(
        (
            x
            for x in items
            if isinstance(x, dict) and str(x.get("instrumentId")) == str(instrument_id)
        ),
        {},
    )
    configs = item.get("leverageConfigs", [])
    configs = configs if isinstance(configs, list) else []
    result: dict[str, object] = {
        "symbol": symbol,
        "instrument_id": instrument_id,
        "raw_eligibility_payload": raw,
        "raw_top_level_keys": list(data),
        "eligibilities_count": len(items),
        "leverage_configs_count": len(configs),
        "allow_open_position": item.get("allowOpenPosition"),
        "allowed_order_quantity_type": item.get("allowedOrderQuantityType"),
    }
    for output, field in (
        ("settlement_type", "settlementType"),
        ("direction", "direction"),
        ("leverage_values", "leverageValues"),
        ("min_position_amount", "minPositionAmount"),
    ):
        result[output] = [x.get(field) for x in configs if isinstance(x, dict)]
    try:
        eligibility = map_demo_eligibility(raw, instrument_id, symbol)
        result.update(
            mapping_result="PASS",
            mapping_exception_type=None,
            mapping_exception_message=None,
            exact_eligibility_rejection=list(_eligibility_blockers(eligibility)),
        )
    except (ValueError, RuntimeError) as exc:
        result.update(
            mapping_result="FAIL",
            mapping_exception_type=type(exc).__name__,
            mapping_exception_message=str(exc),
            exact_eligibility_rejection=["ELIGIBILITY_MAPPING_FAILED: " + str(exc)],
        )
        if exc.__cause__ is not None:
            result["mapping_cause_type"] = type(exc.__cause__).__name__
            result["mapping_cause_message"] = str(exc.__cause__)
    return result


def diagnose_demo_eligibility(
    config: ApplicationConfig,
    values: Mapping[str, str],
    *,
    transport: HttpTransport | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "demo_submission_attempts": 0,
        "demo_write_performed": False,
        "real_write_performed": False,
        "demo_writes": 0,
        "real_writes": 0,
        "samples": [],
    }
    credentials = runtime_credentials(values)
    if (
        config.operating_mode is not OperatingMode.ETORO_DEMO
        or not config.etoro_api_enabled
        or credentials is None
    ):
        return {**report, "status": "DEMO_READ_ACCESS_REQUIRED"}
    secrets = tuple(values.get(key, "") for key in ("ETORO_API_KEY", "ETORO_USER_KEY"))
    http = DisciplinedHttpClient(
        EligibilityDiagnosticTransport(transport or UrllibTransport(config.etoro_transport_mode)),
        max_read_attempts=1,
    )
    client = EtoroReadClient(credentials, http)
    samples: list[dict[str, object]] = []
    try:
        selection: dict[str, object] = {}
        report["live_selection"] = selection
        pacing = float(values.get("AEGIS_LIVE_SELECTION_PACING_SECONDS", "1"))
        candidates = current_catalog_candidates(
            client,
            selection_diagnostics=selection,
            live_get_cap=int(values.get("AEGIS_LIVE_SELECTION_GET_CAP", "50")),
            pacing_delay_seconds=pacing,
        )[:10]
        for candidate in candidates:
            sleep(pacing)
            iid = int(candidate.broker_instrument_id)
            sample: dict[str, object] = {
                "symbol": candidate.symbol,
                "instrument_id": iid,
                "http_status": None,
                "eligibility_endpoint": DEMO_ELIGIBILITY_PATH,
            }
            try:
                response = client.demo_eligibility_response(iid, candidate.symbol)
                sample["http_status"] = response.status
                try:
                    raw = response.json()
                except (ValueError, UnicodeDecodeError):
                    raw = {"non_json_response": True}
                if response.status == 200:
                    sample.update(inspect_payload(raw, iid, candidate.symbol))
                else:
                    sample["raw_eligibility_payload"] = raw
                    sample["mapping_result"] = "HTTP_ERROR"
                    sample["exact_eligibility_rejection"] = [f"HTTP_{response.status}"]
                if response.status == 429:
                    sample["retry_after"] = next(
                        (v for k, v in response.headers.items() if k.casefold() == "retry-after"),
                        None,
                    )
                    samples.append(sample)
                    report["status"] = "RATE_LIMITED_STOPPED_NO_RETRY"
                    break
            except (ValueError, RuntimeError) as exc:
                sample.update(
                    mapping_result="READ_ERROR", mapping_exception_type=type(exc).__name__
                )
            samples.append(sample)
        else:
            report["status"] = "ELIGIBILITY_DIAGNOSTICS_COMPLETE"
        report.update(samples=samples, current_candidates_diagnosed=len(samples))
    except (ValueError, RuntimeError, OSError) as exc:
        report.update(
            status="DIAGNOSTIC_UNAVAILABLE", error_type=type(exc).__name__, error=str(exc)
        )
    # Round-trip guarantees only JSON-safe, redacted data leaves the diagnostic.
    return dict(json.loads(json.dumps(sanitize(report, secrets))))
