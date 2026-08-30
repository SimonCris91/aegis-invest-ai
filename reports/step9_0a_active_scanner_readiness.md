# AEGIS INVEST AI — STEP 9.0A1
## Active Market Scanner — Readiness Inventory

Status: READ-ONLY INVENTORY COMPLETE
Repository root: `C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi`
Cache inspected: `work/market-data-cache.sqlite3`
Cache size inspected: 117,346,304 bytes
Active historical rows: 208,849
Quarantined historical rows: 926
Duplicate timestamp groups in active cache: 0
Broker write calls: 0
Demo execution: OFF
Real execution: unavailable

This report is a reconnaissance snapshot only. It does not change RiskPolicy, RiskManager, confidence profiles, ExitPolicy, frozen candidates/manifests, sizing rules, the EUR200 baseline, lifecycle behavior, credentials, or broker execution.

## A. Eligible Universe

The currently configured canonical research/scanner universe contains 34 symbols across EQUITY, ETF, and CRYPTO. All 34 have at least one verified Alpaca mapping in the cache. DIA is verified as an Alpaca ETF only; the previous invalid eToro DIA crypto mapping remains quarantined and is not treated as valid.

Fractional-position support below is the current simulator/scanner assumption created from cache mappings (`fractional_supported=True`, minimum order value `1`). It is not a broker-live execution proof.

| Symbol | Full asset name in repo | Asset class | Internal canonical symbol | Provider symbol | Mapping status | Historical provider(s) in cache | Fractional simulation support |
|---|---|---:|---|---|---|---|---|
| AAPL | AAPL | EQUITY | AAPL | AAPL | VERIFIED | Alpaca, eToro, Polygon | Assumed true in simulator |
| MSFT | MSFT | EQUITY | MSFT | MSFT | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| NVDA | NVDA | EQUITY | NVDA | NVDA | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| AMZN | AMZN | EQUITY | AMZN | AMZN | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| GOOGL | GOOGL | EQUITY | GOOGL | GOOGL | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| META | META | EQUITY | META | META | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| TSLA | TSLA | EQUITY | TSLA | TSLA | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| AMD | AMD | EQUITY | AMD | AMD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| JPM | JPM | EQUITY | JPM | JPM | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| UNH | UNH | EQUITY | UNH | UNH | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| XOM | XOM | EQUITY | XOM | XOM | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| COST | COST | EQUITY | COST | COST | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| SPY | SPY | ETF | SPY | SPY | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| QQQ | QQQ | ETF | QQQ | QQQ | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| VTI | VTI | ETF | VTI | VTI | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| IWM | IWM | ETF | IWM | IWM | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| DIA | DIA | ETF | DIA | DIA | VERIFIED / Alpaca only | Alpaca | Assumed true in simulator |
| XLK | XLK | ETF | XLK | XLK | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| XLF | XLF | ETF | XLF | XLF | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| XLE | XLE | ETF | XLE | XLE | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| XLV | XLV | ETF | XLV | XLV | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| XLU | XLU | ETF | XLU | XLU | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| TLT | TLT | ETF | TLT | TLT | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| GLD | GLD | ETF | GLD | GLD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| BTC | BTC | CRYPTO | BTC | BTC/USD | VERIFIED | Alpaca, eToro, Polygon | Assumed true in simulator |
| ETH | ETH | CRYPTO | ETH | ETH/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| SOL | SOL | CRYPTO | SOL | SOL/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| XRP | XRP | CRYPTO | XRP | XRP/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| ADA | ADA | CRYPTO | ADA | ADA/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| AVAX | AVAX | CRYPTO | AVAX | AVAX/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| LINK | LINK | CRYPTO | LINK | LINK/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| LTC | LTC | CRYPTO | LTC | LTC/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| BCH | BCH | CRYPTO | BCH | BCH/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |
| DOT | DOT | CRYPTO | DOT | DOT/USD | VERIFIED | Alpaca, eToro | Assumed true in simulator |

### Cache Coverage Snapshot

| Timeframe | Cached symbols | Cached series | Active bars | Providers |
|---|---:|---:|---:|---|
| 1D | 34 / 34 | 68 | 110,526 | Alpaca, eToro, Polygon |
| 4H | 34 / 34 | 68 | 98,323 | Alpaca, eToro, Polygon |
| 1H | 0 / 34 | 0 | 0 | None |
| Intraday generic | 0 / 34 | 0 | 0 | None |

Important 4H note: 4H is not READY. Although all 34 symbols have some cached 4H rows, coverage is fragmented. Many Alpaca 4H series cover early historical windows ending around 2017-2018, while eToro 4H is capped around 1,000 candles and mostly recent. AAPL/SPY/BTC/ETH have some recent Alpaca 4H continuity, but this is not enough to mark the full universe as active-scanner ready.

### Integrity Notes

| Issue | Current state |
|---|---|
| Active duplicate timestamp groups | 0 |
| Invalid DIA eToro crypto mapping | Quarantined: eToro `100580`, 706 rows across 1D/4H |
| Old false SOL Alpaca/eToro equivalence | Quarantined: broker id `100002` vs verified SOL `100063`, 220 rows across 1D/4H |
| DIA eToro reference | Unavailable / not accepted as broker-verified ETF mapping |

## B. Timeframe Readiness Matrix

| Timeframe | Status | Provider/path available | Cache available | Temporal coverage | Pagination support | Market-calendar handling | Freshness/timestamp support | Concrete blocker |
|---|---|---|---|---|---|---|---|---|
| 1D | READY | Alpaca historical cache primary; eToro and limited Polygon reference data also present | 34/34 symbols | Alpaca stocks/ETFs mostly 2016-01-04 to 2026-08-28; Alpaca crypto varies from 2021-07-31 or later to 2026-08-30 | Cache is idempotent; Alpaca adapter supports pagination via `next_page_token`; eToro capped to 1,000 candles | Daily historical validation has separate historical freshness semantics; weekends/closures handled outside live 300s quote freshness | Timestamps normalized in cache; provider provenance preserved | No blocker for offline 1D analysis. Fresh live daily polling still needs normal provider runtime validation when used prospectively |
| 4H | PARTIAL | Alpaca, eToro, limited Polygon paths exist | 34/34 symbols have some 4H rows | Fragmented: many Alpaca equity/ETF series start in 2016 but end in 2017-2018; eToro mostly recent 1,000-candle windows; crypto varies widely | Alpaca and Polygon adapters support pagination; eToro path uses capped candle count | Basic equity/ETF vs crypto distinction exists; full continuity/gap validation for active 4H scanner is incomplete | Timestamps/provenance are stored; active scanner freshness can classify stale/closed state | Need focused 4H continuity remediation for the intended active universe. Do not declare READY from the old partial backfill |
| 1H | BLOCKED | Code paths exist: Alpaca maps `TimeFrame.ONE_HOUR` to `1Hour`; eToro maps to `OneHour`; Polygon/Massive adapter does not support 1H | 0/34 cached 1H symbols | None in current cache | Alpaca adapter supports bounded pagination; eToro capped; Polygon 1H not implemented | `classify_intraday_freshness()` supports 1H equity/ETF market-closed distinction and crypto 24/7 handling | Freshness states exist: FRESH, DELAYED, STALE, INSUFFICIENT_HISTORY, PROVIDER_UNAVAILABLE, MARKET_CLOSED | Windows read-only 1H pilot remains pending because Codex network produced PermissionError. No cache evidence yet |
| 15m | NOT_IMPLEMENTED | No dedicated `TimeFrame` enum or provider mapping for 15m | None | None | None implemented | Not defined | Not defined | Add explicit timeframe enum, provider mappings, cache validation, calendar/freshness semantics before use |
| 5m | NOT_IMPLEMENTED | No dedicated `TimeFrame` enum or provider mapping for 5m | None | None | None implemented | Not defined | Not defined | Add explicit timeframe enum, provider mappings, cache validation, calendar/freshness semantics before use |

## C. Provider Capability Matrix

| Provider/path | Historical bars | Latest market data | Read-only intraday bars | Timestamps/freshness | Pagination | Current classification |
|---|---|---|---|---|---|---|
| Alpaca historical adapter | Implemented for 1D, 4H, 1H | Not a quote/latest provider in current code; recent bars can support scanner once fetched | Implemented for 1H via `1Hour`; 15m/5m not implemented | Timestamps normalized to UTC; scanner freshness contract exists for 1H | Implemented with `next_page_token`; cache upserts are idempotent | IMPLEMENTED for historical bars; IMPLEMENTABLE_WITH_CURRENT_PROVIDER for fresh 1H once Windows read-only pilot succeeds |
| eToro read-only client + historical adapter | Implemented for OneMinute/OneHour/FourHours/OneDay/OneWeek, capped by current candle-count semantics | Implemented read-only identity/search/rates/portfolio/readiness paths from earlier steps | Implemented path exists for OneMinute/OneHour, but not used as primary active intraday scanner feed | Transport has User-Agent and request id handling; live quote freshness remains separate | No deep historical pagination proven; current candle read uses `candles_count <= 1000` | IMPLEMENTED for broker/read-only reference; PARTIAL for historical depth and intraday research |
| Polygon/Massive historical adapter | Implemented for 1D and 4H only | Not implemented as latest active scanner provider | 1H/15m/5m not implemented | UTC aggregate normalization exists | Implemented via `next_url` | PARTIAL; plan/rate constraints make it non-primary currently |
| Stooq historical provider | Present in provider architecture for deterministic research fallback | Not latest | Not intraday | Historical only | Provider-specific | IMPLEMENTABLE/legacy fallback for equity/ETF research, not active scanner primary |
| Fake/fixture market data providers | Implemented for tests | Implemented offline only | Synthetic only | 300s stale quote checks in fake provider | Not applicable | IMPLEMENTED for tests only |
| Alpha Vantage news | Implemented as news provider, not market-data provider | Not market bars | Not market bars | News causality/freshness only | Request planning for news only | IMPLEMENTED for news context, not scanner price bars |

## D. Exact Blockers

1. 1H active scanner data is blocked by missing real cache evidence.
   - Code exists for Alpaca 1H acquisition and scanner integration.
   - Current cache has 0 1H bars for all 34 symbols.
   - Codex network was previously blocked by PermissionError; Windows pilot remains the correct validation environment.

2. 4H must stay PARTIAL.
   - There are cached 4H rows for 34/34 symbols, but coverage is not consistently recent or continuous across the full universe.
   - eToro 4H is capped around 1,000 candles.
   - Alpaca 4H cache contains fragmented historical windows for many symbols.

3. 15m and 5m are not implemented.
   - `TimeFrame` currently has `INTRADAY`, `1H`, `4H`, `1D`, `1W`; there is no explicit 15m or 5m model.
   - Provider adapters do not map 15m/5m to external provider syntax.
   - Freshness and market-calendar semantics are not defined at these granularities.

4. Latest/read-only live market state is not yet unified for continuous scanner operation.
   - eToro can provide read-only rates/reference information.
   - Alpaca historical bars can provide recent bars when fetched.
   - The continuous orchestration loop exists, but fresh real 1H provider ingestion still needs the Windows pilot.

5. DIA remains Alpaca-only verified for ETF identity.
   - The invalid eToro DIA crypto mapping is quarantined.
   - DIA can be researched as Alpaca ETF provenance, but must not be treated as eToro cross-provider verified.

## E. Recommended Smallest Next Implementation Step

The smallest next step is not a new scanner build. The scanner foundation and orchestrator already exist.

Recommended next step:

1. Run the already prepared Windows read-only 1H pilot for a tiny universe: AAPL, SPY, BTC.
2. If successful, populate only the required 120-bar 1H causal context per pilot symbol.
3. Verify scanner output from those cached 1H bars.
4. Only after that, expand 1H acquisition to the 34-symbol universe in a bounded/resumable way.

Do not proceed to 15m/5m until 1H is operational and measured.

## Terminal Summary

```text
STEP_9_0A1_STATUS
eligible_assets=34
1D=READY
4H=PARTIAL
1H=BLOCKED
15m=NOT_IMPLEMENTED
5m=NOT_IMPLEMENTED
critical_blockers=1H cache empty/pending Windows pilot; 4H fragmented and not full-universe ready; 15m/5m absent from TimeFrame/provider mappings
broker_write_calls=0
```
