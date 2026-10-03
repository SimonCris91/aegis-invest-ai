# eToro bootstrap cooldown review — 30 September 2026

Source checkout: `D:\Aegis\ahhh-s-ho-capito-cosa-intendi`.

The catalog bootstrap now persists a retry deadline after the first HTTP 429.
Subsequent invocations before that deadline make zero network requests and leave
the checkpoint and active-universe artifact untouched. Provider `Retry-After`
seconds and HTTP dates take precedence; otherwise consecutive failures use a
15/30/60-minute local backoff. Old checkpoints containing instrument-level 429
records are recognized. Successful reads reset the failure count.

Reports distinguish untouched instruments (`pending`), failed instruments that
need another attempt (`retryable`), and their sum (`remaining`). An unfinished
pass no longer reports completion. `requests_attempted` refers to reads in the
current invocation, not historical records in the checkpoint.

Validation: 28 bootstrap tests and 21 continuous-runner tests passed, including cooldown persistence, no network
or artifact writes during the wait, resumption at the deadline, escalating
backoff, provider deadlines across catalog refresh, and invalid response headers.
Python compilation and diff whitespace checks passed. The two new files pass
Ruff lint/format and the helper passes strict type checking. Existing lint issues
elsewhere in the two previously modified files were not reformatted wholesale.

The executable bootstrap command loads this code on invocation. The trading
runner does not call this bootstrap. This change does not install a periodic
bootstrap scheduler. The runner was subsequently gracefully stopped and restarted
to activate and verify the coordinated catalog/cache/artifact refresh below.

Live recovery completed in `work/recovery-staging`: all 22 retryable records were
resolved. The 43-instrument delta finished with 20 BOOTSTRAPPED, 2 NO_DATA,
9 UNSUPPORTED and 12 UNSUPPORTED_INTERNAL. BOOTSTRAPPED means both required
timeframes returned data, not that a trading strategy has sufficient evidence.

Live cooldown verification at approximately 14:06:48 UTC: one GET received 429;
the provider supplied Retry-After, and the new checkpoint persisted
`retry_not_before=2026-09-30T14:07:42.816531+00:00` with source
`PROVIDER_RETRY_AFTER`. Earlier invocations during the legacy cooldown performed
zero GETs. Later bounded retries completed the recovery, respecting provider deadlines.

Runtime observation at 14:04:31 UTC: runner RUNNING, cycle
INSUFFICIENT_GLOBAL_SELECTION_COVERAGE, blocker FRESH_NEWS_REQUIRED, one equity
TOP, no broker writes in the last poll. These are timestamped observations, not
a claim that trading has been unblocked.

Public check at 16:24 Europe/Rome: the canonical owner-only Site responds HTTP
401 with Server cloudflare and CF-Ray. Owner-authenticated dashboard access was
not verified in this review.

Activation at approximately 16:15 Europe/Rome: full catalog 16,154; active artifact
11,416 -> 11,421 (seven admitted, two excluded by updated internal-instrument
metadata); 3,426 verified bars merged through the normal cache API. Catalog,
bootstrap progress and active artifact share snapshot `cbb2308cf669087b`.
Restore copies are in
`work/recovery-staging/backup-catalog-20260930T141530469368Z`.
The preparation/activation script requires a stopped runner and released lease,
checks source hashes, prepares offline, validates cached bars and retains rollback.

The Demo runner restarted at 16:17, generation 186. Its subsequent cycle confirms
16,154 catalog instruments and 11,421 active instruments. No Real writes.
The autonomous pipeline submitted ETF QQQ.RTH order 385152384 after reaching
RiskManager and execution admission. A subsequent independent read-only broker
lookup confirmed FILLED, position 3606902787, exposure USD 12.249492. The local
submission registry also records FILLED with ETORO_V2_ORDER_LOOKUP reconciliation.
This is evidence of a non-crypto Demo execution, not proof that all candidate
issues are resolved. Other candidates were rejected for unverified market-open
state or stale quotes; the news provider status remains PARTIAL.
