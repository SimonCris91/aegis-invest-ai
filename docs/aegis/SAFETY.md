# AEGIS INVEST AI — SAFETY

These invariants are mandatory.

## Execution

Execution mode must be explicit:

- `READ_ONLY`: Demo writes prohibited; Real writes prohibited.
- `DEMO_EXECUTION`: eToro Demo writes permitted only for genuine accepted opportunities after
  RiskManager sizing, execution admission, durable idempotency and authorized-capital checks.
- `REAL_EXECUTION`: unavailable.

Never:

- place real orders
- fall back from Demo to Real
- enable production trading
- modify broker credentials
- create an implicit broker write path
- silently enable execution
- bypass RiskManager, sizing, admission, idempotency or the authorized-capital envelope

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
