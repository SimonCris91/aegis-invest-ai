# Aegis Invest AI

Aegis Invest AI is a safety-first investment analysis and local paper-trading
application. Its internal analysis component is **Aegis Agent**. eToro is only
an optional read-only integration boundary; this project is not affiliated
with or endorsed by eToro.

This repository is engineering infrastructure, not investment advice. No test,
simulation, or analysis establishes profitability.

The project was renamed from AI INVESTOR ETORO after Step 3. Historical Step 1
and Step 3 reports retain their original names as immutable project records.

## Current Capability

Step 7 adds live eToro read-only readiness orchestration. It can verify
credentials, authenticated identity, expected username/GCID, instrument
resolution, fresh rates, Demo aggregate portfolio, Demo eligibility, real
portfolio read-only availability, and Shadow Mode ingestion without broker
writes. All broker access is disabled by default.
There is no real-money execution interface or route.

Operating modes are closed and fail-closed:

```text
OFFLINE_PAPER
SHADOW
ETORO_DEMO
ETORO_REAL_READ_ONLY
```

`ETORO_DEMO` requires both `ETORO_API_ENABLED=true` and
`ETORO_DEMO_EXECUTION_ENABLED=true`. Credentials are runtime-only `SecretStr`
values and are not included in application configuration, logs, audit events,
or persistence.

Step 4 implements a complete offline pipeline:

```text
Portfolio Data
      -> Market Data
      -> News Context
      -> Aegis Agent
      -> TradeProposal
      -> Risk Manager
      -> RiskAuthorization
      -> ExecutionAdmissionGate
      -> Paper Trading Engine
```

All market/news providers used by tests are fake or fixture-backed and require
no network. External AI is optional; the deterministic baseline works offline.

## Aegis Agent

Aegis Agent receives only immutable normalized portfolio, quote, news,
instrument, timestamp, and safe strategy data. It has no broker client,
credentials, HMAC key, RiskAuthorization factory, execution interface, policy
mutation capability, or kill-switch mutation interface.

Its only investment-action output is an immutable `TradeProposal`. The default
`DeterministicAegisAgent` is conservative: missing, weak, stale, or ineligible
evidence produces `HOLD` and no proposal. External text remains named data and
is never interpreted as an application instruction.

The optional `AIAnalysisProvider` boundary receives a secret-free normalized
DTO. Pydantic validation rejects malformed output, unknown actions/symbols,
leverage, invalid direction, and allocation above 10% before proposal creation.

## Risk Manager

The independent deterministic Risk Manager preserves every Step 3 rule:

- leverage must equal 1
- no short selling, CFDs, margin, futures, options, or derivatives
- no martingale or blind averaging down
- no all-in trades
- maximum single-position exposure: 25%
- maximum new trade size: 10% of portfolio value
- minimum cash reserve: 10%
- maximum daily new trades: 3
- stale/missing market data and missing news fail closed
- invalid metadata, duplicates, state inconsistency, and currency mismatch fail closed
- a critical operational error activates the global kill switch
- the kill switch is active by default and blocks every order
- proposal confidence must meet the configured threshold

Approval creates a short-lived HMAC-signed `RiskAuthorization` bound to the
exact proposal and policy digest. The admission gate verifies and consumes the
authorization once and records an in-process admission. The paper engine
accepts only a trade minted by that gate.

This is a programmatic capability boundary, not a prompt instruction. Python
code already executing inside the trusted process is outside this threat model;
model/provider output is always treated as data and receives no code execution.

## Paper Trading

Paper trading is local simulation only. `PaperPortfolioState`, `PaperPosition`,
and `PaperFill` are distinct from read-only broker portfolio types. The engine
supports simulated open, increase, reduce, and close operations; tracks cash,
weighted entry price, realized/unrealized P&L, marks, and history; and prevents
negative cash, negative quantity, shorts, replay, duplicate proposals, and
duplicate idempotency keys.

Fees and slippage are explicit configurable simulation values. A `PaperFill`
is never represented as a real broker fill.

## Broker Integrations

`app/brokers` is the canonical broker abstraction. `app/brokers/etoro` maps only
fields and routes verified in the official eToro API documentation. Reads use
bounded retry behavior. Demo writes are attempted once; transport ambiguity
becomes `UNKNOWN`, activates the kill switch, and requires reconciliation.

`app/brokers/etoro/readiness.py` is fail-closed: credentials and
`ETORO_API_ENABLED=true` are required before authentication; configured expected
identity is required before any portfolio, rate, eligibility, or Shadow Mode
readiness check proceeds.

The only write URL present is the verified asynchronous Demo endpoint. A route
guard checks scheme, host, and exact Demo path. No real execution port, URL, or
fallback exists.

Real broker and paper portfolio state are separate domain types. Paper execution
does not call eToro or any other broker.

`app/etoro` remains as a legacy Step 3 read-only compatibility package. New code
should use `app/brokers` unless it is maintaining that compatibility surface.

## Real Trading

```text
REAL-MONEY EXECUTION: NOT IMPLEMENTED
```

There is no order manager, write-capable broker protocol, trading HTTP request,
production execution mode, or real close/modify/submit operation. Configuration
rejects `PRODUCTION` and `production_trading_enabled=True`.

Project-scoped `.codex/config.toml` keeps these eToro MCP tools disabled:

```text
execute-write
place-trade
place-close
```

## Package Boundaries

```text
app/agent          normalized context, deterministic agent, validated AI boundary
app/brokers        canonical broker read, Demo, identity, readiness boundaries
app/config         typed fail-closed application, provider, strategy, and paper settings
app/domain         immutable provider-neutral domain models
app/etoro          read-only protocol and fake client
app/execution      one-time risk authorization admission gate
app/main           read-only CLI
app/market_data    provider protocol plus fake and fixture adapters
app/news           provider protocol plus fake and fixture adapters
app/orchestration  complete AegisInvestmentService pipeline
app/paper_trading  isolated local ledger and simulated fills
app/portfolio      deterministic portfolio calculations
app/reporting      secret-free audit sinks and JSON logging
app/risk           kill switch and independent Risk Manager
app/strategy       proposal-only strategy protocol retained from Step 3
```

## Configuration

At startup the CLI loads the project-local `.env` file, if present, and overlays
real process environment variables on top of it. Values are kept in memory only:
they are not written into `ApplicationConfig`, logs, audit events, reports, or
persistence. `.env.example` contains safe values and empty placeholders only;
real `.env` files are ignored by Git.

```text
AEGIS_ENVIRONMENT=DEMO
AEGIS_DOTENV_ENABLED=true
AEGIS_KILL_SWITCH=true
AEGIS_PAPER_TRADING_ENABLED=true
AEGIS_MARKET_DATA_PROVIDER=fixture
AEGIS_NEWS_PROVIDER=fixture
AEGIS_AI_PROVIDER=deterministic
AEGIS_BROKER_PROVIDER=none
AEGIS_ETORO_READ_ENABLED=false
AEGIS_OPERATING_MODE=OFFLINE_PAPER
ETORO_API_ENABLED=false
ETORO_DEMO_EXECUTION_ENABLED=false
ETORO_DEMO_SMOKE_TEST_OPT_IN=false
AEGIS_EXECUTION_POLICY=ADVISORY
ETORO_EXPECTED_USERNAME=
ETORO_EXPECTED_GCID=
AEGIS_ETORO_READINESS_SYMBOL=
AEGIS_ETORO_READINESS_INSTRUMENT_ID=
AEGIS_ETORO_READINESS_MAX_QUOTE_AGE_SECONDS=300
```

Provider secrets are not part of `ApplicationConfig` and are never logged.
If `ETORO_USER_KEY` is missing or empty, eToro credentials are considered not
configured and authenticated eToro reads are not attempted.
EUR/USD conversion is not implemented or assumed; mismatches are rejected.

## Commands

```powershell
.venv\Scripts\python.exe -m app.main health
.venv\Scripts\python.exe -m app.main portfolio
.venv\Scripts\python.exe -m app.main analyze
.venv\Scripts\python.exe -m app.main paper-status
.venv\Scripts\python.exe -m app.main broker-status
.venv\Scripts\python.exe -m app.main etoro-status
.venv\Scripts\python.exe -m app.main etoro-readiness
.venv\Scripts\python.exe -m app.main shadow-run
.venv\Scripts\python.exe -m app.main demo-status
.venv\Scripts\python.exe -m app.main reconcile
.venv\Scripts\python.exe -m app.main performance
.venv\Scripts\python.exe -m app.main demo-smoke-test
```

The `analyze` command reports `not_configured` until explicit offline fixture
symbols are supplied by an application composition root. It does not invent data.

Verification:

```powershell
.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
```

## Audit

Typed audit events cover run lifecycle, portfolio/market/news reads, agent
analysis, proposal creation/rejection, risk approval/rejection, paper admission,
paper completion/rejection, and provider failures. The event schema has no free
form headers, tokens, credentials, secret payloads, or provider request bodies.

## Known Blockers

- No real market/news/AI provider is configured.
- No live eToro credentials are configured in this workspace.
- eToro readiness is `NOT_CONFIGURED` until credentials, API enablement, and
  expected identity settings are supplied at runtime.
- No FX provider is configured; conversion fails closed unless a fresh verified
  rate is injected.
- Demo smoke test remains blocked until identity, Demo account, quote,
  eligibility, FX when needed, RiskAuthorization, gate admission, kill switch,
  and explicit opt-in all succeed.
- Real trading requires a separately authorized future phase and is structurally
  absent from this repository.
