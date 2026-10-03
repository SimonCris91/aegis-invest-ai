# News evidence recovery — 1 October 2026

Active checkout: `D:\Aegis\ahhh-s-ho-capito-cosa-intendi`.

Applied changes:
- Preserve Alpaca article tickers in `RawNewsItem.provider_symbols`; do not append them to prose that is truncated during normalization.
- Combine explicit headline and summary links, preserving all named assets. Exact source metadata is accepted only from the Alpaca adapter with the established financial-source classification. Broker aliases remain verified by the existing candidate pipeline.
- Handle a missing display name without crashing normalization.
- Do not invent USD news symbols for broker JPY/NZD and other non-USD crypto pairs; preserve already normalized USD pairs.

Checks: 83 focused evidence, precision, wiring and candidate tests passed; 17 additional existing global-provider, GDELT and cross-source tests passed before the pair-normalization addition. Ruff and Python syntax checks passed.

Read-only live Alpaca proof: 25 articles received, 22 containing structured source symbols, 30 source-symbol contexts reconstructed. No broker writes were made by this diagnostic. This proves article normalization, not admission of any broker order.

Deployment: continuous Demo runner stopped gracefully, lease released and restarted from the D: checkout to load the changed imports. Dashboard/tunnel remain on D:. Normal RiskManager, concentration caps, freshness and execution admission remain required.

Unresolved: candidate-specific absence of relevant articles, GDELT provider cooldown, unsupported foreign listing aliases and crypto concentration cap. No arbitrary broker suffix was stripped to force a match. A catalog timestamp from yesterday alone is not evidence that its contents are stale.

Public check: canonical Sites URL responds HTTP 401 with Cloudflare and the AEGIS access-page marker. The Sites connector returns project-not-found for the configured project in the current context, so authenticated public dashboard verification is unavailable. Anonymous HTTP 401 is not proof of working authenticated runtime connectivity.
