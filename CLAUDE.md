# AEGIS INVEST AI — CLAUDE INSTRUCTIONS

Before working on this repository, read and follow:

- AGENTS.md
- docs/aegis/SAFETY.md
- docs/aegis/ARCHITECTURE.md
- docs/aegis/FROZEN_COMPONENTS.md
- docs/aegis/DATA_STATUS.md
- docs/aegis/CURRENT_STATE.md
- docs/aegis/DEVELOPMENT_RULES.md

These files are the authoritative project instructions.

## Mandatory safety

broker_write_calls = 0
Demo execution = OFF
Real execution = unavailable

Never:

- place real orders
- place Demo orders
- modify broker credentials
- introduce broker write paths without explicit authorization

## Working rule

Do not rebuild validated architecture.
Do not modify frozen components unless explicitly instructed.
Prefer small, testable, auditable changes.
Anti-lookahead and causal data handling are mandatory.
Entry ranking and position management must remain independent.

Stop at the explicitly requested step.
Do not autonomously continue to the next project step.

If any instruction here conflicts with AGENTS.md or docs/aegis/, treat AGENTS.md and docs/aegis/ as the source of truth.
