# AEGIS INVEST AI — DEVELOPMENT RULES

## Roles

Technical strategy, step ordering and final interpretation are decided externally.

Repository agents implement only the explicitly requested step.

## Before coding

1. Read AGENTS.md and docs/aegis/.
2. Inspect existing implementation.
3. Reuse existing abstractions.
4. Identify the smallest valid change.
5. Preserve frozen components and safety invariants.

## Implementation philosophy

Prefer:

- small diffs
- deterministic behavior
- explicit enums / reason codes
- testable pure components
- causal state
- auditable provider provenance
- backward compatibility
- explicit failure

Avoid:

- unnecessary refactors
- speculative abstractions
- duplicated models
- hidden fallbacks
- silent provider switching
- parameter optimization
- giant historical backfills
- unrelated cleanup

## Temporal rules

For every causal evaluation:

data_timestamp <= evaluation_timestamp

Different asset classes do not require identical bar timestamps.

Cross-asset comparison must consider:

- causal availability
- freshness
- market-session state
- data quality
- provider provenance

Do not use wall-clock age alone when session semantics matter.

## Position rules

Entry eligibility does not control whether an existing position is managed.

An OPEN position must remain reachable by the position-management path even when excluded from new-entry ranking.

No operation may accidentally create a short position.

## Tests

After meaningful changes run the relevant tests.

Preferred checks where supported:

pytest
ruff check
ruff format --check
mypy
python -m app.main health

Do not hide or reinterpret failing tests.

Always verify:

broker_write_calls = 0

## Output after a step

Report:

1. files changed
2. architecture/behavior changed
3. tests added
4. tests executed
5. actual result
6. remaining blockers
7. safety status

Stop after the requested step.

Do not autonomously continue.
