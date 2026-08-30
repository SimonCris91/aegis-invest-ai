# AEGIS INVEST AI — AGENT INSTRUCTIONS

Before modifying this repository, read:

- docs/aegis/SAFETY.md
- docs/aegis/ARCHITECTURE.md
- docs/aegis/FROZEN_COMPONENTS.md
- docs/aegis/DATA_STATUS.md
- docs/aegis/CURRENT_STATE.md
- docs/aegis/DEVELOPMENT_RULES.md

## Core rules

- Continue from the current repository state.
- Do not rebuild validated architecture.
- Prefer small, incremental, testable changes.
- Reuse existing abstractions before creating new ones.
- Anti-lookahead and causal data handling are mandatory.
- Entry ranking and position management are separate concerns.
- Stop at the requested step.
- Never autonomously continue to the next project step.

## Mandatory safety

broker_write_calls = 0
Demo execution = OFF
Real execution = unavailable

Never place Real or Demo orders.
Never modify broker credentials.
Never introduce broker write paths without explicit human authorization.

Always report:
- files changed
- behavior actually verified
- tests run
- blockers
- broker_write_calls
