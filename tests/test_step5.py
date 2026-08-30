"""Deterministic Step 5 broker, storage, performance, and safety tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.demo import (
    DEMO_ORDER_URL,
    DemoExecutionError,
    EtoroDemoAdapter,
    assert_demo_route,
)
from app.brokers.etoro.http import (
    DisciplinedHttpClient,
    HttpResponse,
    TransportError,
    UnknownWriteOutcome,
)
from app.brokers.etoro.mapping import EtoroMappingError, map_identity, map_quote
from app.brokers.models import (
    BrokerSubmission,
    ExecutionState,
    PerformanceSnapshot,
    PreflightDecision,
)
from app.brokers.reconciliation import ReconciliationError, reconcile_submission
from app.config import ConfigLoadError, load_config
from app.domain.enums import Currency
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.execution.gate import RiskEnforcedExecutionGate
from app.performance.metrics import (
    NOT_ENOUGH_DATA,
    benchmark_relative_return,
    maximum_drawdown,
    total_return,
)
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SecretPersistenceError, SqliteRecordStore


class StubTransport:
    def __init__(self, responses: list[HttpResponse] | None = None, *, error: bool = False) -> None:
        self.responses = responses or []
        self.error = error
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        if self.error:
            raise TransportError("synthetic failure")
        return self.responses.pop(0)


def test_config_modes_fail_closed() -> None:
    assert load_config({}).operating_mode.value == "OFFLINE_PAPER"
    with pytest.raises(ConfigLoadError):
        load_config({"AEGIS_OPERATING_MODE": "ETORO_DEMO"})
    with pytest.raises(ConfigLoadError):
        load_config({"AEGIS_OPERATING_MODE": "UNKNOWN"})
    config = load_config(
        {
            "AEGIS_OPERATING_MODE": "ETORO_DEMO",
            "ETORO_API_ENABLED": "true",
            "ETORO_DEMO_EXECUTION_ENABLED": "true",
        }
    )
    assert config.etoro_demo_execution_enabled


def test_credentials_are_redacted_and_request_ids_are_unique() -> None:
    credentials = EtoroCredentials(api_key="api-secret", user_key="user-secret")
    assert "api-secret" not in repr(credentials)
    first = credentials.headers(lambda: UUID(int=1))
    second = credentials.headers(lambda: UUID(int=2))
    assert first["x-request-id"] != second["x-request-id"]
    assert first["x-api-key"] == "api-secret"


def test_read_retries_are_bounded_and_writes_are_once() -> None:
    transport = StubTransport(
        [HttpResponse(429, {"Retry-After": "0"}, b"{}"), HttpResponse(200, {}, b"{}")]
    )
    client = DisciplinedHttpClient(transport)
    assert client.get("https://example.invalid", {}).status == 200
    assert len(transport.calls) == 2
    failing = StubTransport(error=True)
    with pytest.raises(UnknownWriteOutcome):
        DisciplinedHttpClient(failing).post_once("https://example.invalid", {}, {})
    assert len(failing.calls) == 1


def test_official_mappings_are_strict(now: datetime) -> None:
    identity = map_identity(
        {
            "gcid": "stable-user",
            "demoCid": 2,
            "realCid": 1,
            "username": "aegis-user",
            "scopes": ["read"],
        }
    )
    assert identity.demo_account_id == 2
    quote = map_quote(
        {
            "rates": [
                {
                    "instrumentID": 7,
                    "lastExecution": 10,
                    "bid": 9,
                    "ask": 11,
                    "date": now.isoformat(),
                }
            ]
        },
        instrument_id=7,
        symbol="TEST",
        currency=Currency.USD,
    )
    assert quote.price == 10
    with pytest.raises(EtoroMappingError):
        map_identity({"username": "incomplete"})


def test_demo_route_guard_has_no_fallback() -> None:
    assert_demo_route(DEMO_ORDER_URL)
    for url in (
        "http://public-api.etoro.com/api/v3/trading/execution/demo/orders",
        "https://public-api.etoro.com/api/v3/trading/execution/real/orders",
        "https://evil.invalid/api/v3/trading/execution/demo/orders",
    ):
        with pytest.raises(DemoExecutionError):
            assert_demo_route(url)


def test_demo_adapter_requires_gate_and_posts_verified_payload(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
    tmp_path: Path,
) -> None:
    evaluation = risk_manager.evaluate(proposal, context)
    assert evaluation.authorization is not None
    gate = RiskEnforcedExecutionGate(risk_manager)
    admitted = gate.admit(proposal, evaluation.authorization, at=now)
    transport = StubTransport([HttpResponse(202, {}, b'{"orderId":12,"referenceId":"ref"}')])
    adapter = EtoroDemoAdapter(
        credentials=EtoroCredentials(api_key="a", user_key="u"),
        http=DisciplinedHttpClient(transport),
        gate=gate,
        kill_switch=KillSwitch(active=False, reason="test", clock=lambda: now),
        registry=SqliteRecordStore(tmp_path / "demo.sqlite3"),
        enabled=True,
        explicit_opt_in=True,
        clock=lambda: now,
    )
    result = adapter.submit_demo(
        admitted, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1"))
    )
    assert result.state is ExecutionState.SUBMITTED
    assert transport.calls[0][1] == DEMO_ORDER_URL
    assert b'"leverage":1' in (transport.calls[0][3] or b"")
    assert b'"settlementType":"real"' in (transport.calls[0][3] or b"")


def test_storage_rejects_secrets_and_round_trips(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "aegis.sqlite3")
    record_id = store.append("shadow-run", {"status": "HOLD", "nested": {"count": 1}})
    assert record_id == 1
    assert store.list("shadow-run")[0]["status"] == "HOLD"
    with pytest.raises(SecretPersistenceError):
        store.append("bad", {"nested": {"token": "never"}})


def test_performance_metrics_and_insufficient_data(now: datetime) -> None:
    one = (
        PerformanceSnapshot(
            timestamp=now,
            strategy_version="v1",
            equity=Decimal("100"),
            benchmark_value=Decimal("100"),
            currency=Currency.USD,
        ),
    )
    assert total_return(one) == NOT_ENOUGH_DATA
    snapshots = one + (
        PerformanceSnapshot(
            timestamp=datetime(2026, 8, 29, tzinfo=UTC),
            strategy_version="v1",
            equity=Decimal("90"),
            benchmark_value=Decimal("95"),
            currency=Currency.USD,
        ),
    )
    assert total_return(snapshots) == Decimal("-0.1")
    assert maximum_drawdown(snapshots) == Decimal("0.1")
    assert benchmark_relative_return(snapshots) == Decimal("-0.05")


def test_reconciliation_mismatch_activates_kill_switch(now: datetime) -> None:
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    submission = BrokerSubmission(
        idempotency_key="idempotent-1",
        request_id="request",
        state=ExecutionState.SUBMITTED,
        broker_order_id="12",
        proposal_id="proposal",
        authorization_id="authorization",
        submitted_at=now,
    )
    with pytest.raises(ReconciliationError):
        reconcile_submission(submission, frozenset(), switch)
    assert switch.state.active
