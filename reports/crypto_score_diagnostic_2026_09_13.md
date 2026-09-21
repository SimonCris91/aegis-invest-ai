# Crypto scoring diagnostic — 2026-09-13

Scope: offline, no provider requests, no broker writes, no production changes.
Cycle: b1e3d9d34bd43543629a2d8752f667c7655a7fd7fe8748b4579d780f2ca21293.
Completed: 19:13:10 UTC; final bar start: 18:00 UTC.

Reconstruction uses today's cache restricted to the cycle bar cutoff, not an
immutable historical snapshot. Instrument session status is set OPEN for the
seven historically admitted symbols. Score and confidence match all seven
persisted candidates exactly. Each input has 60 consecutive hourly bars.

## Findings

- BTC, BCH, ETH, ETC, XRP: all five strategies HOLD; confidence 0.5075.
- API3: trend WATCH, other strategies HOLD; confidence 0.5275.
- LTC: trend BUY, momentum WATCH, breakout HOLD, mean-reversion REDUCE,
  defensive HOLD; agreement 0.40 and confidence 0.4475.
- All required directional features are available (sufficiency 1).
- All seven have missing volume_change, relative_volume, spread and
  liquidity_proxy; aggregate feature quality PARTIAL.
- The cache holds 121 pre-cutoff hourly bars per symbol, all with volume NULL.
  This establishes absence in stored data, not whether every upstream API lacks volume.
- Feature extraction requires bid/ask on the bar instrument for spread
  (app/intelligence/features.py:563). The reconstructed inputs lack these.
- PARTIAL produces data-quality score 65 and missing liquidity produces score45
  (app/intelligence/scoring.py). Regime confidence is 0.55
  (app/intelligence/regime.py:_confidence). Strategy base confidence also
  depends on aggregate feature quality (strategies.py:_confidence_from_quality).
- Therefore missing microstructure affects V2B confidence INDIRECTLY through
  regime and strategy confidence, although execution readiness is not a direct
  term in the V2B formula. This is not proof that completing data produces BUY.
- The active scanner passes no news_signal to analyze_candidate
  (app/scanner/active.py:452); the engine defaults to not_configured news
  (app/intelligence/service.py). All seven reconstructed news scores are 50.
  Observational news context does not recalculate ranking. Displayed positive
  news is therefore not equivalent to an integrated directional news signal.
- V2B provenance identifies a 1D calibration dataset while this runtime is 1H.
  That is a validation question, not authorization to change thresholds.

## Next steps

1. Audit provider payload/mapping for real volume and timestamped bid/ask;
   never fill absent volume with invented values or attach present quotes to past bars.
2. Specify whether news should remain observational or enter scoring. Any
   scoring integration needs causal timestamp, attribution and regression tests.
3. Validate the existing confidence model on 1H data before changing strategy
   semantics or thresholds. More assets alone does not repair missing inputs.

Diagnostic command: `.venv/Scripts/python.exe -m work.inspect_crypto_scores`.
The script follows the latest persisted cycle, so later runs may inspect a newer cycle.
