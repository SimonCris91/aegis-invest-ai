"""Read-only trace of the first ten existing bounded-selection lookups."""

import json

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.live_candidates import current_catalog_candidates
from app.brokers.etoro.runtime import runtime_credentials
from app.config.loader import load_config, load_runtime_values


def main() -> None:
    values = load_runtime_values()
    config = load_config(values)
    credentials = runtime_credentials(values)
    rows: list[dict[str, object]] = []
    stats: dict[str, object] = {}
    error: str | None = None
    try:
        if credentials is None or not config.etoro_api_enabled:
            raise ValueError("ETORO_READ_ACCESS_NOT_CONFIGURED")
        current_catalog_candidates(
            EtoroReadClient(
                credentials, DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
            ),
            session_diagnostics=rows,
            selection_diagnostics=stats,
            live_get_cap=min(10, int(values.get("AEGIS_LIVE_SELECTION_GET_CAP", "50"))),
            pacing_delay_seconds=float(values.get("AEGIS_LIVE_SELECTION_PACING_SECONDS", "1")),
        )
    except (EtoroApiError, ValueError) as exc:
        error = (
            exc.transport_detail or exc.category.value
            if isinstance(exc, EtoroApiError)
            else str(exc)
        )
        for row in rows:
            row["open_tradable_result"] = False
            row["selection_error"] = error
    print(
        json.dumps(
            {
                "crypto_checked": sum(
                    r.get("asset_class") == "CRYPTO" and r.get("http_status") == 200 for r in rows
                ),
                "first_10_results": rows,
                "selection": stats,
                "error": error,
                "demo_submission_attempts": 0,
                "demo_write_performed": False,
                "real_write_performed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
