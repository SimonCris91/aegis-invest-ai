# AEGIS INVEST AI — SAFETY

These invariants are mandatory.

## Execution

broker_write_calls = 0
Demo execution = OFF
Real execution = unavailable

Never:

- place real orders
- place Demo orders
- enable production trading
- modify broker credentials
- create an implicit broker write path
- silently enable execution

Provider and market-data APIs may only be used read-only where authorized.

## Research safety

Historical simulations are research evidence only.

Do not present historical simulated performance as expected future performance.

Do not modify strategy/risk parameters merely to improve results.

## Data safety

Anti-lookahead is mandatory.

Never use:

- future bars
- incomplete future bars
- future confidence state
- future opportunity state
- future position state
- information unavailable at the causal scan timestamp

Always preserve provider provenance and auditability.
