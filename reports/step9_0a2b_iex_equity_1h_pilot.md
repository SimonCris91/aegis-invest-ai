# STEP 9.0A2B IEX Equity 1H Pilot

- status: `ACTIVE_SCANNER_1H_IEX_EQUITY_PILOT_READY`
- phase: `STEP_9_0A2B_ALPACA_FREE_IEX_EQUITY_1H_PILOT`
- broker_write_calls: `0`
- demo_execution_enabled: `False`
- real_execution_available: `False`
- windows_cmd: `cd /d C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi && set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED" && set "AEGIS_EXIT_POLICY_PROFILE=EXITPOLICY_V2_GUARDED" && .venv\Scripts\python.exe -m app.main active-scanner-1h-iex-pilot`

## Requested Universe

```json
{"EQUITY": ["AAPL"], "ETF": ["SPY"]}
```

## Acquisition

| symbol | asset_class | provider | provider_symbol | requested_feed | feed_provenance | final_status | fetched_bars | cached_context_bars | earliest | latest | freshness | duplicate_timestamps | ohlcv_valid | pagination_verified | cache_round_trip | broker_write_calls |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- | ---: | --- | --- | --- | ---: |
| AAPL | EQUITY | alpaca | AAPL | iex | ALPACA_IEX | READY | 79 | 79 | 2026-08-17T12:00:00+00:00 | 2026-08-28T20:00:00+00:00 | MARKET_CLOSED | 0 | True | False | inserted=46, updated=0, unchanged=33 | 0 |
| SPY | ETF | alpaca | SPY | iex | ALPACA_IEX | READY | 89 | 89 | 2026-08-17T12:00:00+00:00 | 2026-08-28T19:00:00+00:00 | MARKET_CLOSED | 0 | True | False | inserted=52, updated=0, unchanged=37 | 0 |

## Scanner Output

```json
{"as_of": "2026-08-30T18:10:44.030709+00:00", "broker_write_calls": 0, "duplicate_decisions_prevented": 0, "existing_positions_monitored": 0, "no_trade_count": 2, "rejected_count": 0, "simulated_capital": "200", "timeframe": "1H", "top_opportunities": [], "total_candidates": 2, "watchlist": []}
```

## Final Classification

- repeated_intraday_shadow_scan_ready: `True`
- next_blocker: `None`
