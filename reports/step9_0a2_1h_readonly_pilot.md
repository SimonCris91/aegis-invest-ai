# STEP 9.0A2 1H Read-Only Data Pilot

- status: `ACTIVE_SCANNER_1H_DATA_ACQUISITION_BLOCKER`
- phase: `STEP_9_0A2_1H_READ_ONLY_DATA_PILOT`
- broker_write_calls: `0`
- demo_execution_enabled: `False`
- real_execution_available: `False`
- windows_cmd: `cd /d C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi && set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED" && set "AEGIS_EXIT_POLICY_PROFILE=EXITPOLICY_V2_GUARDED" && .venv\Scripts\python.exe -m app.main active-scanner-1h-pilot`

## Requested Universe

```json
{"CRYPTO": ["BTC", "ETH"], "EQUITY": ["AAPL"], "ETF": ["SPY"]}
```

## Acquisition

| symbol | asset_class | provider | provider_symbol | final_status | fetched_bars | cached_context_bars | earliest | latest | freshness | duplicate_timestamps | ohlcv_valid | pagination_verified | cache_round_trip | broker_write_calls |
| --- | --- | --- | --- | --- | ---: | ---: | --- | --- | --- | ---: | --- | --- | --- | ---: |
| AAPL | EQUITY | alpaca |  | PROVIDER_UNAVAILABLE |  |  |  |  | PROVIDER_UNAVAILABLE |  |  | False | inserted=0, updated=0, unchanged=0 | 0 |
| SPY | ETF | alpaca |  | PROVIDER_UNAVAILABLE |  |  |  |  | PROVIDER_UNAVAILABLE |  |  | False | inserted=0, updated=0, unchanged=0 | 0 |
| BTC | CRYPTO | alpaca | BTC/USD | READY | 144 | 120 | 2026-08-25T18:00:00+00:00 | 2026-08-30T17:00:00+00:00 | FRESH | 0 | True | True | inserted=144, updated=0, unchanged=0 | 0 |
| ETH | CRYPTO | alpaca | ETH/USD | READY | 144 | 120 | 2026-08-25T18:00:00+00:00 | 2026-08-30T17:00:00+00:00 | FRESH | 0 | True | True | inserted=144, updated=0, unchanged=0 | 0 |

## Scanner Output

```json
{"as_of": "2026-08-30T17:48:18.905327+00:00", "broker_write_calls": 0, "duplicate_decisions_prevented": 0, "existing_positions_monitored": 0, "no_trade_count": 1, "rejected_count": 0, "simulated_capital": "200", "timeframe": "1H", "top_opportunities": [], "total_candidates": 2, "watchlist": [{"affordable_fractionally": true, "asset_class": "CRYPTO", "bucket": "WATCHLIST", "confidence": "0.45", "current_position_state": "NO_POSITION", "data_quality": "PARTIAL", "decision": "HOLD", "market_state": "CONTINUOUS_24_7", "opportunity_score": "63.00", "proposed_capital_allocation": "10.00", "provider_provenance": ["alpaca"], "rank": 2, "regime": ["RANGE", "LOW_VOLATILITY", "TRANSITION"], "rejection_reasons": [], "remaining_simulated_cash": "190.00", "risk_flags": ["market regime does not confirm a trend-following setup", "momentum is decelerating", "momentum is mixed", "breakout lacks volume expansion", "breakout confirmation is weak", "price is stretched above its recent mean", "data quality is incomplete"], "symbol": "BTC"}]}
```

## Final Classification

- repeated_intraday_shadow_scan_ready: `False`
- next_blocker: `obtain fresh sufficient 1H Alpaca bars for every pilot symbol`
