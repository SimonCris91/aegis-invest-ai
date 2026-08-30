# STEP 9.0A4B Multi-Cycle Isolation Regression

## Outcome

`MULTI_CYCLE_ISOLATION_VERIFIED`

One `ActiveMarketScanner` instance was reused for two sequential cycles with
different timestamps, instruments, market data and portfolio state. The second
cycle matched a fresh scanner instance byte-for-byte at the Pydantic model level
for both the scan result and the temporal snapshot.

## Assertions

- `single_instance_multicycle_verified=true`
- `fresh_instance_equivalence=true`
- `dedup_state_reset=true`
- `cross_cycle_state_leakage=false`
- `market_closed_non_actionable=true`
- `broker_write_calls=0`

The duplicate counter was independently recomputed for the second cycle and
returned `1` for both reused and fresh instances. The first cycle's symbol,
timestamp, position and negative-drift data did not appear in the second cycle.
The second cycle used a new symbol, positive-drift data and an empty portfolio;
its management set was empty and its observations reported `NO_POSITION`.

## Market-Closed Regression

The controlled closed-market case asserts all required semantics:

- `eligible_for_entry_comparison=true`
- `bucket=NO_TRADE`
- `opportunity_score=0`
- `decision=HOLD`

## Verification

- targeted Step 9 tests: `27 passed`
- full test suite: `546 passed`
- Ruff: passed
- format check: passed
- mypy: passed
- health: passed
- production scanner behavior changed: `false`
- broker writes, Demo orders and Real orders: `0`

No scheduler, continuous loop, simulated trade or broker request was run.
