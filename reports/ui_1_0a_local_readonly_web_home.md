# Aegis UI 1.0A Local Read-Only Web Home

## Implementation

The repository had no frontend framework, build system, router or HTTP API.
The Home uses a dependency-free Python `ThreadingHTTPServer` plus vanilla
HTML, CSS and JavaScript. This keeps the implementation local and small while
reusing the existing `active-scan-cycle` runtime report.

## Read-Only API

`GET /api/home` maps the existing scanner report through the typed
`AegisHomeSnapshot` adapter. HTTP errors remain errors; they are not converted
to zero opportunities. POST, PUT, PATCH and DELETE return `405`.

## Home Structure

The rendered order is:

1. Capital
2. Scanner State
3. Aegis Conclusion
4. Watchlist
5. Positions
6. Risk / Safety
7. Data Health

The UI has explicit loading, error, degraded, empty-watchlist and
empty-positions states. A successful zero-opportunity scan renders `NO TRADE
REQUIRED` and does not look like a failure.

## Files

- `app/web/home.py`: typed backend-to-Home adapter
- `app/web/server.py`: local GET-only HTTP server
- `web/index.html`: semantic Home shell
- `web/styles.css`: responsive layout and isolated design tokens
- `web/app.js`: read-only fetch and presentation rendering
- `tests/test_ui_home.py`: snapshot mapping and local HTTP read-only contract tests

## Start

From the repository root:

```text
.venv\Scripts\python.exe -m app.web.server
```

Browser URL: `http://127.0.0.1:8765`

## Data Mapping

Capital, scan timestamp, universe counts, classifications, watchlist and
safety values come from the current scanner report. The displayed capital is
the scanner's current simulated-capital context, not the frozen historical
research baseline. Positions are read from the management set; no positions
are invented when it is empty.

## Safety

- broker write calls: `0`
- Demo execution: OFF
- Real execution: unavailable
- no order controls
- no broker mutation endpoint
- no changes to scanner, strategy, risk, confidence or lifecycle code

## Verification

- Local smoke test: `GET /` = `200`, `GET /api/home` = `200`, `POST /api/home` = `405`.
- Targeted UI tests: `4 passed`.
- Full suite: `550 passed`.
- Ruff, format check, mypy and health: passed.
- Full repository coverage: `88%`; no frontend build applies because the repository has no frontend build system and the UI is served as static files.
