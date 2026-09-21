# eToro missing-input audit — 2026-09-14

Two read-only provider requests: BTC 100000, three OneHour candles and current rates.
No orders, strategy changes, threshold changes or runtime restart performed by this audit.

## Direct evidence

- Candle starts 02:00, 03:00, 04:00 UTC on September 14: each raw `volume` is null.
  The 04:00 candle was still forming when queried; it is not a calibration sample.
- Rate response: provider timestamp 04:53:55.431506 UTC, bid 77614.4,
  ask 77615.39 USD. These are historical observations of this diagnostic call,
  not current prices to trade on.
- `app/data/historical/etoro.py:_normalize_etoro_candles` reads per-candle volume;
  absence remains None. It must not substitute the group's aggregate volume.
- `HistoricalDataCache.upsert_bars` and `get_bars` preserve None, zero and positive
  volume. No volume loss was found along this path.
- `app/brokers/etoro/mapping.py:map_quote` retains bid, ask and provider date.
- The scanner constructs its analysis quote from the latest candle close and
  receives bars reconstructed with catalog instrument metadata. That path has
  no timestamped quote history. The spread feature reads bar.instrument.bid/ask.
  This is missing integration, not evidence that the rates parser drops bid/ask.

Scope limit: the raw volume observation concerns BTC and these three candles;
it does not establish that all eToro instruments/endpoints always lack volume.

## Tests

Added tests/test_etoro_volume_provenance.py: None/zero/positive volume through
normalization and SQLite, no aggregate-volume substitution, quote field/date
preservation. With market-data and acquisition tests: 21 passed.

## Next implementation boundary

Persist timestamped quote observations separately from OHLC candles, then
define an explicit causal join (provider timestamp AND local observation time
not later than the decision cutoff, bounded freshness, matching identity).
Do not attach a quote from 04:53 to an earlier historical decision/bar as if
it had been known then. Existing past scores remain unchanged.

Volume requires a separate source-availability investigation; do not synthesize
it or silently substitute another exchange's volume for eToro volume.

The official documentation index lists separate candle history and market-rate
endpoints: https://api-portal.etoro.com/llms.txt . Individual endpoint documents
were not retrievable through the browser tool during this audit; live payload
observations and source code above provide the substantive evidence.
