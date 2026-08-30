# STEP 9.0A2E - 1H Temporal Semantics Final Verification

## Outcome

The 1H data layer now distinguishes session closure from stale data for US equities/ETFs, preserves explicit Alpaca IEX provenance, and proves Alpaca pagination through a deterministic multi-page fixture.

Final state: `ONE_HOUR_DATA_LAYER_READY_FOR_ACTIVE_SCANNER`

## Freshness Logic Found

The active scanner now evaluates 1H equity/ETF freshness against the expected completed bar for the current session, not only against wall-clock age.

Key rule in `app/scanner/active.py`:

- if equity/ETF and the market is open, the latest cached bar must be at or after the expected completed 1H bar;
- if the market is closed, missing a newer bar does not automatically mean stale;
- crypto continues to use 24/7 semantics.

## Minimal Fix

- added open-session expected-bar comparison for equity/ETF 1H freshness;
- preserved `MARKET_CLOSED` as a non-blocking state for closed sessions;
- preserved existing crypto freshness semantics;
- exposed `market_session_state`, `expected_latest_bar`, and `pagination_verified` in the 1H acquisition payload for reporting.

## Verified Semantics

### Weekend

- `market_session_state = CLOSED`
- `freshness = MARKET_CLOSED`
- `stale = false`

### Overnight Closed Session

- `market_session_state = CLOSED`
- `freshness = MARKET_CLOSED`
- `stale = false`

### Genuinely Stale During Open Session

- `market_session_state = OPEN`
- latest cached bar older than the expected completed bar
- `freshness = STALE`

### Current Session / Expected Latest Bar

- latest cached bar matches the expected completed bar
- `freshness = FRESH`

## SIP vs IEX

The Alpaca equity 1H pilot now explicitly requests `feed=iex` for the pilot path.

Verified provenance:

- equity/ETF pilot provenance = `ALPACA_IEX`
- crypto provenance = `ALPACA_CRYPTO_US`

The old SIP subscription failure is therefore no longer conflated with the IEX pilot path.

## Pagination Proof

Deterministic Alpaca pagination fixture verified:

- `pagination_pages_tested = 3`
- `pagination_requested = true`
- `pagination_token_observed = true`
- `second_page_fetched = true`
- `pagination_verified = true`
- `duplicate_timestamps = 0`
- ordering preserved

## Regression

The previously validated full-universe 1H readiness remains unchanged:

- `symbols_requested = 34`
- `symbols_1h_ready = 34`
- `global_1h = READY`
- `broker_write_calls = 0`

This step did not rerun the full sweep.

## Tests

Focused regression checks passed:

- weekend closure stays `MARKET_CLOSED`
- overnight closure stays `MARKET_CLOSED`
- open-session missing expected bar is `STALE`
- current-session expected bar is `FRESH`
- IEX pilot uses explicit `feed=iex`
- multi-page Alpaca pagination is verified
- report rendering and CLI remain clean
- broker writes remain at zero

## Final Contract

- `market_closed_vs_stale_verified = true`
- `expected_latest_bar_verified = true`
- `sip_vs_iex_semantics_verified = true`
- `pagination_pages_tested = 3`
- `pagination_verified = true`
- `duplicate_timestamp_groups = 0`
- `global_1h = READY`
- `broker_write_calls = 0`
