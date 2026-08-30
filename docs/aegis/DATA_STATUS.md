# AEGIS INVEST AI — DATA STATUS

## Current eligible universe

34 assets currently validated for the 1H active-scanner data layer.

Asset classes:

- EQUITY
- ETF
- CRYPTO

## 1H status

global_1h = READY
full_universe_ready = 34/34

Equity:
READY

ETF:
READY

Crypto:
READY

## Providers

Equity / ETF:

ALPACA_IEX

Crypto:

ALPACA_CRYPTO_US

The previous SIP equity blocker is resolved.

SIP subscription failure must not be interpreted as Alpaca being unavailable when IEX is available.

## Verified temporal semantics

Verified:

- MARKET_CLOSED vs STALE_DATA
- expected completed bar semantics
- explicit IEX provenance
- SIP vs IEX distinction
- causal timestamps
- pagination
- 3-page deterministic pagination proof
- duplicate timestamps = 0

Final validated 1H data state:

ONE_HOUR_DATA_LAYER_READY_FOR_ACTIVE_SCANNER

## Other timeframes

1D:
READY

4H:
PARTIAL

Do not declare 4H READY without new verification.

Known historical issues included:

- fragmented/truncated cache
- pagination
- temporal coverage
- naive gap detection
- market-calendar handling

15m:
NOT_IMPLEMENTED

5m:
NOT_IMPLEMENTED

Do not imply unsupported intraday readiness.
