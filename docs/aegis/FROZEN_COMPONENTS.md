# AEGIS INVEST AI — FROZEN COMPONENTS

Do not modify the following unless explicitly instructed and technically justified.

## Confidence

Profile:

V2_B_GUARDED

Semantics:

SIGNAL_RELIABILITY_V2

V1_LEGACY remains available where already supported.

Do not lower confidence thresholds to create additional trades.

## Exit Policy

EXITPOLICY_V2_GUARDED

Precedence:

CLOSE > REDUCE > HOLD

REDUCE/CLOSE cannot create short positions.

Cooldown and existing lifecycle semantics must be preserved.

## Frozen candidate

exitpolicy-v2-balanced-guarded-candidate

Fingerprint:

e9808af62107387e976e1f792e3861aef2ae5746e197c3f0fbcf1b9da8facba3

Do not alter this candidate based on subsequent outcomes.

## Risk

Do not modify merely to increase trading frequency:

- RiskPolicy
- RiskManager semantics
- sizing rules

## Research baseline

The €200 research baseline is frozen.

Starting equity:
€200

Ending equity:
€224.5301270773

Research-exposed return:
+12.2651%

Max drawdown:
8.5863%

Completed lifecycles:
7

Wins / losses:
6 / 1

This is RESEARCH_EXPOSED evidence.

It is not pristine out-of-sample evidence and must not be presented as expected future return.
