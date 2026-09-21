from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.demo import DEMO_ORDER_URL, DemoExecutionError, assert_demo_route
from app.brokers.etoro.demo_pilot import (
    EtoroAutomaticDemoPilot,
    EtoroDemoOrderIntent,
    EtoroDemoPilotBlocker,
    EtoroDemoPilotSettings,
    EtoroDemoPilotStatus,
    EtoroDemoSubmissionOutcome,
    EtoroDemoSubmissionPackage,
    RiskCheckedEtoroDemoSubmissionGateway,
    demo_pilot_settings,
)
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse, TransportError
from app.brokers.models import ExecutionState, PreflightDecision
from app.domain.enums import AssetClass, MarketStatus, OperatingMode
from app.domain.proposals import TradeProposal
from app.domain.risk import AuthorizedCapitalEnvelope, RiskAuthorization, RiskContext
from app.execution.gate import AuthorizedTrade, RiskEnforcedExecutionGate
from app.intelligence.models import AegisDecision, FeatureQuality, TimeFrame
from app.orchestration.active_intelligence import (
    ActiveIntelligenceCycleRecord,
    CycleChangeClassification,
    DataHealthState,
)
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.scanner.active import ActiveScannerBucket, ActiveScannerCandidate, ActiveScannerResult
from app.storage.sqlite import SqliteRecordStore


class StubEtoroTransport:
    def __init__(self, response: HttpResponse | None = None, *, error: bool = False) -> None:
        self.response = response or HttpResponse(202, {}, b'{"orderId":"order-1"}')
        self.error = error
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        if self.error:
            raise TransportError("synthetic post failure")
        return self.response


class StubReadback:
    def __init__(self, state: ExecutionState) -> None:
        self.state = state
        self.calls: list[tuple[int, str]] = []

    def demo_order_state(self, instrument_id: int, order_id: str) -> ExecutionState:
        self.calls.append((instrument_id, order_id))
        return self.state


class RejectingGate(RiskEnforcedExecutionGate):
    def admit(
        self,
        proposal: TradeProposal,
        authorization: RiskAuthorization,
        *,
        at: datetime | None = None,
    ) -> AuthorizedTrade:
        raise PermissionError("synthetic gate rejection")


class FakeDemoGateway:
    def __init__(self, store: SqliteRecordStore, *, submitted: bool = True) -> None:
        self.store = store
        self.submitted = submitted
        self.intents: list[EtoroDemoOrderIntent] = []

    def submit_demo_order(
        self,
        intent: EtoroDemoOrderIntent,
        *,
        package: EtoroDemoSubmissionPackage | None = None,
    ) -> EtoroDemoSubmissionOutcome:
        self.intents.append(intent)
        if not self.store.reserve_demo_submission(
            intent.idempotency_key,
            {
                "cycle_id": intent.cycle_id,
                "symbol": intent.symbol,
                "endpoint": intent.endpoint,
            },
        ):
            return EtoroDemoSubmissionOutcome(
                submitted=False,
                sanitized_status="DUPLICATE",
                broker_write_calls_real=0,
            )
        self.store.update_demo_submission(
            intent.idempotency_key,
            "SUBMITTED" if self.submitted else "REJECTED",
            {
                "symbol": intent.symbol,
                "sanitized_status": "SUBMITTED" if self.submitted else "REJECTED",
            },
        )
        return EtoroDemoSubmissionOutcome(
            submitted=self.submitted,
            broker_order_id="demo-order-1" if self.submitted else None,
            sanitized_status="SUBMITTED" if self.submitted else "REJECTED",
            demo_broker_write_calls=1 if self.submitted else 0,
            broker_write_calls_real=0,
        )


class SequencedDemoGateway(FakeDemoGateway):
    def __init__(self, store: SqliteRecordStore, outcomes: tuple[bool, ...]) -> None:
        super().__init__(store)
        self.outcomes = list(outcomes)

    def submit_demo_order(
        self,
        intent: EtoroDemoOrderIntent,
        *,
        package: EtoroDemoSubmissionPackage | None = None,
    ) -> EtoroDemoSubmissionOutcome:
        self.submitted = self.outcomes.pop(0)
        return super().submit_demo_order(intent, package=package)


def test_top_opportunity_becomes_eligible_demo_intent(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store)
    result = _pilot(store, gateway=gateway).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((_top_candidate("AAPL"),)),
        broker_instrument_ids={"AAPL": 1001},
    )

    assert result.status is EtoroDemoPilotStatus.SUBMITTED
    assert len(result.eligible_intents) == 1
    assert result.eligible_intents[0].endpoint == DEMO_ORDER_URL
    assert result.eligible_intents[0].environment is OperatingMode.ETORO_DEMO
    assert gateway.intents == list(result.eligible_intents)
    assert result.demo_broker_write_calls == 1
    assert result.broker_write_calls_real == 0


def test_unavailable_top_falls_back_to_package_ready_watchlist(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store)
    cycle = _cycle()
    fallback_proposal = proposal.model_copy(
        update={"symbol": "WATCH", "instrument_id": 1002}
    )
    fallback_intent = _intent_for_proposal(fallback_proposal, cycle=cycle)
    packages = {
        "WATCH": _package_for_intent(fallback_intent, fallback_proposal, context)
    }

    result = _pilot(store, gateway=gateway).run(
        cycle=cycle,
        scanner_result=_scanner_result(
            (_top_candidate("TOP"), _candidate("WATCH", ActiveScannerBucket.WATCHLIST))
        ),
        broker_instrument_ids={"TOP": 1001, "WATCH": 1002},
        submission_packages=packages,
        allow_exploratory_watchlist=True,
    )

    assert result.status is EtoroDemoPilotStatus.SUBMITTED
    assert [intent.symbol for intent in gateway.intents] == ["WATCH"]


def test_exploratory_watchlist_tries_next_candidate_after_gate_rejection(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = SequencedDemoGateway(store, (False, True))
    cycle = _cycle()
    first_proposal = proposal.model_copy(update={"symbol": "FIRST", "instrument_id": 1001})
    second_proposal = proposal.model_copy(update={"symbol": "SECOND", "instrument_id": 1002})
    first_intent = _intent_for_proposal(first_proposal, cycle=cycle)
    second_intent = _intent_for_proposal(second_proposal, cycle=cycle)
    packages = {
        "FIRST": _package_for_intent(first_intent, first_proposal, context),
        "SECOND": _package_for_intent(second_intent, second_proposal, context),
    }

    result = _pilot(store, gateway=gateway).run(
        cycle=cycle,
        scanner_result=_scanner_result(
            (
                _candidate("FIRST", ActiveScannerBucket.WATCHLIST),
                _candidate("SECOND", ActiveScannerBucket.WATCHLIST),
            )
        ),
        broker_instrument_ids={"FIRST": 1001, "SECOND": 1002},
        submission_packages=packages,
        allow_exploratory_watchlist=True,
    )

    assert result.status is EtoroDemoPilotStatus.SUBMITTED
    assert [intent.symbol for intent in gateway.intents] == ["FIRST", "SECOND"]
    assert result.demo_broker_write_calls == 1


@pytest.mark.parametrize(
    "bucket",
    (
        ActiveScannerBucket.WATCHLIST,
        ActiveScannerBucket.NO_TRADE,
        ActiveScannerBucket.REJECTED,
    ),
)
def test_non_top_candidates_submit_zero_orders(tmp_path: Path, bucket: ActiveScannerBucket) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store)
    result = _pilot(store, gateway=gateway).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((_candidate("AAPL", bucket),)),
        broker_instrument_ids={"AAPL": 1001},
    )

    assert result.status is EtoroDemoPilotStatus.NO_TOP_OPPORTUNITY
    assert result.demo_broker_write_calls == 0
    assert gateway.intents == []


def test_market_closed_non_top_submits_zero_orders(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store)
    candidate = _candidate(
        "SPY",
        ActiveScannerBucket.NO_TRADE,
        market_status=MarketStatus.CLOSED,
    )

    result = _pilot(store, gateway=gateway).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((candidate,)),
        broker_instrument_ids={"SPY": 3000},
    )

    assert result.status is EtoroDemoPilotStatus.NO_TOP_OPPORTUNITY
    assert result.demo_broker_write_calls == 0
    assert gateway.intents == []


def test_no_cycle_submits_zero_orders(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store)

    result = _pilot(store, gateway=gateway).run(
        cycle=None,
        scanner_result=None,
        broker_instrument_ids={"AAPL": 1001},
    )

    assert result.status is EtoroDemoPilotStatus.NO_CYCLE
    assert result.demo_broker_write_calls == 0
    assert gateway.intents == []


def test_duplicate_cycle_candidate_submits_at_most_once(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store)
    pilot = _pilot(store, gateway=gateway)
    cycle = _cycle()
    scanner_result = _scanner_result((_top_candidate("AAPL"),))

    first = pilot.run(
        cycle=cycle,
        scanner_result=scanner_result,
        broker_instrument_ids={"AAPL": 1001},
    )
    second = pilot.run(
        cycle=cycle,
        scanner_result=scanner_result,
        broker_instrument_ids={"AAPL": 1001},
    )

    assert first.status is EtoroDemoPilotStatus.SUBMITTED
    assert second.status is EtoroDemoPilotStatus.BLOCKED
    assert second.blockers == (EtoroDemoPilotBlocker.DUPLICATE_CYCLE_CANDIDATE,)
    assert len(gateway.intents) == 1


def test_ambiguous_instrument_blocks_before_submission(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store)
    result = _pilot(store, gateway=gateway).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((_top_candidate("AAPL"),)),
        broker_instrument_ids={},
    )

    assert result.status is EtoroDemoPilotStatus.BLOCKED
    assert result.blockers == (EtoroDemoPilotBlocker.AMBIGUOUS_INSTRUMENT_MAPPING,)
    assert result.demo_broker_write_calls == 0
    assert gateway.intents == []


def test_missing_or_invalid_pilot_notional_blocks(tmp_path: Path) -> None:
    assert demo_pilot_settings({}).enabled is False
    assert demo_pilot_settings({"AEGIS_ETORO_DEMO_PILOT_NOTIONAL_EUR": "0"}).enabled is False
    assert (
        demo_pilot_settings({"AEGIS_ETORO_DEMO_PILOT_NOTIONAL_EUR": "not-a-number"}).enabled
        is False
    )

    result = EtoroAutomaticDemoPilot(
        environment=OperatingMode.ETORO_DEMO,
        settings=EtoroDemoPilotSettings(enabled=False, notional_eur=None),
        registry=SqliteRecordStore(tmp_path / "store.sqlite3"),
        gateway=FakeDemoGateway(SqliteRecordStore(tmp_path / "gateway.sqlite3")),
    ).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((_top_candidate("AAPL"),)),
        broker_instrument_ids={"AAPL": 1001},
    )

    assert result.status is EtoroDemoPilotStatus.BLOCKED
    assert result.blockers == (EtoroDemoPilotBlocker.MISSING_PILOT_NOTIONAL,)
    assert result.demo_broker_write_calls == 0


def test_real_environment_hard_rejection(tmp_path: Path) -> None:
    gateway = FakeDemoGateway(SqliteRecordStore(tmp_path / "store.sqlite3"))
    result = EtoroAutomaticDemoPilot(
        environment=OperatingMode.ETORO_REAL_READ_ONLY,
        settings=EtoroDemoPilotSettings(enabled=True, notional_eur=Decimal("10")),
        registry=SqliteRecordStore(tmp_path / "store.sqlite3"),
        gateway=gateway,
    ).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((_top_candidate("AAPL"),)),
        broker_instrument_ids={"AAPL": 1001},
    )

    assert result.status is EtoroDemoPilotStatus.BLOCKED
    assert result.blockers == (EtoroDemoPilotBlocker.REAL_ENVIRONMENT_REJECTED,)
    assert gateway.intents == []
    assert result.broker_write_calls_real == 0


def test_real_endpoint_hard_rejection() -> None:
    with pytest.raises(DemoExecutionError):
        assert_demo_route("https://public-api.etoro.com/api/v1/trading/info/portfolio")


def test_api_rejection_does_not_create_false_success(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    gateway = FakeDemoGateway(store, submitted=False)
    result = _pilot(store, gateway=gateway).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((_top_candidate("AAPL"),)),
        broker_instrument_ids={"AAPL": 1001},
    )

    assert result.status is EtoroDemoPilotStatus.BLOCKED
    assert result.blockers == (EtoroDemoPilotBlocker.DEMO_SUBMISSION_FAILED,)
    assert result.submissions[0].submitted is False
    assert result.demo_broker_write_calls == 0
    assert result.broker_write_calls_real == 0


def test_credentials_never_appear_in_pilot_result(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    result = _pilot(store, gateway=FakeDemoGateway(store)).run(
        cycle=_cycle(),
        scanner_result=_scanner_result((_top_candidate("AAPL"),)),
        broker_instrument_ids={"AAPL": 1001},
    )

    dumped = result.model_dump_json()
    assert "api-secret" not in dumped
    assert "user-secret" not in dumped


def test_risk_checked_gateway_top_pass_posts_once_and_reconciles(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    transport = StubEtoroTransport(HttpResponse(202, {}, b'{"orderId":"order-1"}'))
    readback = StubReadback(ExecutionState.FILLED)
    intent = _intent_for_proposal(proposal)
    package = _package_for_intent(intent, proposal, context)
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=kill_switch,
        registry=store,
        readback=readback,
    )

    outcome = gateway.submit_demo_order(intent, package=package)

    assert outcome.submitted is True
    assert outcome.sanitized_status == ExecutionState.SUBMITTED.value
    assert outcome.reconciliation_status == ExecutionState.FILLED.value
    assert outcome.demo_broker_write_calls == 1
    assert outcome.broker_write_calls_real == 0
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][1] == DEMO_ORDER_URL
    assert "api-secret" not in outcome.model_dump_json()


def test_risk_manager_rejects_before_post(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    transport = StubEtoroTransport()
    intent = _intent_for_proposal(proposal)
    stale_package = _package_for_intent(
        intent,
        proposal,
        context.model_copy(update={"market_data_available": False, "price": None}),
    )
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=kill_switch,
        registry=store,
    )

    outcome = gateway.submit_demo_order(intent, package=stale_package)

    assert outcome.submitted is False
    assert outcome.sanitized_status == "RISK_MANAGER_REJECTED"
    assert outcome.risk_manager_reached is True
    assert transport.calls == []


def test_execution_admission_gate_rejects_before_post(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    transport = StubEtoroTransport()
    intent = _intent_for_proposal(proposal)
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RejectingGate(risk_manager),
        kill_switch=kill_switch,
        registry=store,
    )

    outcome = gateway.submit_demo_order(
        intent,
        package=_package_for_intent(intent, proposal, context),
    )

    assert outcome.submitted is False
    assert outcome.sanitized_status == "EXECUTION_ADMISSION_GATE_REJECTED"
    assert outcome.execution_admission_gate_reached is True
    assert transport.calls == []


def test_preflight_rejects_before_post(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    transport = StubEtoroTransport()
    intent = _intent_for_proposal(proposal)
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=kill_switch,
        registry=store,
    )

    outcome = gateway.submit_demo_order(
        intent,
        package=EtoroDemoSubmissionPackage(
            proposal=_proposal_for_intent(intent, proposal),
            risk_context=context,
            preflight=PreflightDecision(allowed=False, reasons=("blocked",)),
        ),
    )

    assert outcome.submitted is False
    assert outcome.sanitized_status == "DEMO_PREFLIGHT_REJECTED"
    assert transport.calls == []


def test_api_rejection_after_post_is_not_false_success(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    transport = StubEtoroTransport(HttpResponse(400, {}, b'{"error":"rejected"}'))
    intent = _intent_for_proposal(proposal)
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=kill_switch,
        registry=store,
    )

    outcome = gateway.submit_demo_order(
        intent,
        package=_package_for_intent(intent, proposal, context),
    )

    assert outcome.submitted is False
    assert outcome.sanitized_status == ExecutionState.REJECTED.value
    assert outcome.demo_broker_write_calls == 1
    assert len(transport.calls) == 1


def test_ambiguous_timeout_after_post_requires_reconciliation_and_no_blind_retry(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    transport = StubEtoroTransport(error=True)
    intent = _intent_for_proposal(proposal)
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=kill_switch,
        registry=store,
    )

    outcome = gateway.submit_demo_order(
        intent,
        package=_package_for_intent(intent, proposal, context),
    )

    assert outcome.submitted is False
    assert outcome.sanitized_status == ExecutionState.UNKNOWN.value
    assert outcome.demo_broker_write_calls == 1
    assert len(transport.calls) == 1
    assert kill_switch.state.active


def test_restart_after_success_blocks_duplicate_before_gateway(
    tmp_path: Path,
    proposal: TradeProposal,
    context: RiskContext,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    transport = StubEtoroTransport(HttpResponse(202, {}, b'{"orderId":"order-1"}'))
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=kill_switch,
        registry=store,
    )
    pilot = EtoroAutomaticDemoPilot(
        environment=OperatingMode.ETORO_DEMO,
        settings=EtoroDemoPilotSettings(enabled=True, notional_eur=proposal.amount),
        registry=store,
        gateway=gateway,
    )
    cycle = _cycle()
    scanner_result = _scanner_result((_candidate("TEST", ActiveScannerBucket.TOP_OPPORTUNITIES),))
    intent = _intent_for_proposal(proposal, cycle=cycle)
    packages = {"TEST": _package_for_intent(intent, proposal, context)}

    first = pilot.run(
        cycle=cycle,
        scanner_result=scanner_result,
        broker_instrument_ids={"TEST": proposal.instrument_id},
        submission_packages=packages,
    )
    second = pilot.run(
        cycle=cycle,
        scanner_result=scanner_result,
        broker_instrument_ids={"TEST": proposal.instrument_id},
        submission_packages=packages,
    )

    assert first.status is EtoroDemoPilotStatus.SUBMITTED
    assert second.status is EtoroDemoPilotStatus.BLOCKED
    assert second.blockers == (EtoroDemoPilotBlocker.DUPLICATE_CYCLE_CANDIDATE,)
    assert len(transport.calls) == 1


def _pilot(store: SqliteRecordStore, *, gateway: FakeDemoGateway) -> EtoroAutomaticDemoPilot:
    return EtoroAutomaticDemoPilot(
        environment=OperatingMode.ETORO_DEMO,
        settings=EtoroDemoPilotSettings(enabled=True, notional_eur=Decimal("10")),
        registry=store,
        gateway=gateway,
    )


def _scanner_result(
    candidates: tuple[ActiveScannerCandidate, ...],
) -> ActiveScannerResult:
    top = tuple(
        candidate
        for candidate in candidates
        if candidate.bucket is ActiveScannerBucket.TOP_OPPORTUNITIES
    )
    return ActiveScannerResult(
        as_of=datetime(2026, 8, 31, 10, tzinfo=UTC),
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
        candidates=candidates,
        top_opportunities=top,
        watchlist=tuple(
            candidate
            for candidate in candidates
            if candidate.bucket is ActiveScannerBucket.WATCHLIST
        ),
        no_trade=tuple(
            candidate
            for candidate in candidates
            if candidate.bucket is ActiveScannerBucket.NO_TRADE
        ),
        rejected=tuple(
            candidate
            for candidate in candidates
            if candidate.bucket is ActiveScannerBucket.REJECTED
        ),
        duplicate_decisions_prevented=0,
        existing_positions_monitored=0,
    )


def _candidate(
    symbol: str,
    bucket: ActiveScannerBucket,
    *,
    market_status: MarketStatus = MarketStatus.OPEN,
) -> ActiveScannerCandidate:
    return ActiveScannerCandidate(
        symbol=symbol,
        full_asset_name=symbol,
        asset_class=AssetClass.EQUITY if symbol != "BTC" else AssetClass.CRYPTO,
        timestamp=datetime(2026, 8, 31, 10, tzinfo=UTC),
        timeframe=TimeFrame.ONE_HOUR,
        current_market_state=market_status,
        opportunity_score=(
            Decimal("75") if bucket is ActiveScannerBucket.TOP_OPPORTUNITIES else Decimal("0")
        ),
        confidence=(
            Decimal("0.70") if bucket is ActiveScannerBucket.TOP_OPPORTUNITIES else Decimal("0.20")
        ),
        regime=("UPTREND",),
        decision=(
            AegisDecision.BUY
            if bucket is ActiveScannerBucket.TOP_OPPORTUNITIES
            else AegisDecision.HOLD
        ),
        bucket=bucket,
        data_quality_state=FeatureQuality.GOOD,
        current_position_state="NO_POSITION",
        freshness="FRESH",
        provider_provenance=("ALPACA_IEX",),
        affordable_fractionally=True,
        proposed_capital_allocation=Decimal("10"),
        remaining_simulated_cash=Decimal("190"),
        existing_exposure=Decimal("0"),
        diversification_concentration_impact="NEUTRAL",
    )


def _top_candidate(symbol: str) -> ActiveScannerCandidate:
    return _candidate(symbol, ActiveScannerBucket.TOP_OPPORTUNITIES)


def _intent_for_proposal(
    proposal: TradeProposal,
    *,
    cycle: ActiveIntelligenceCycleRecord | None = None,
) -> EtoroDemoOrderIntent:
    active_cycle = cycle or _cycle()
    return EtoroDemoOrderIntent(
        cycle_id=active_cycle.cycle_id,
        symbol=proposal.symbol,
        asset_class=proposal.asset_class.value,
        broker_instrument_id=proposal.instrument_id,
        notional_eur=proposal.amount,
        idempotency_key=f"etoro-demo-pilot:{active_cycle.cycle_id}:{proposal.symbol}:OPEN",
    )


def _proposal_for_intent(intent: EtoroDemoOrderIntent, proposal: TradeProposal) -> TradeProposal:
    return proposal.model_copy(
        update={
            "idempotency_key": intent.idempotency_key,
            "symbol": intent.symbol,
            "instrument_id": intent.broker_instrument_id,
            "amount": intent.notional_eur,
        }
    )


def _package_for_intent(
    intent: EtoroDemoOrderIntent,
    proposal: TradeProposal,
    context: RiskContext,
) -> EtoroDemoSubmissionPackage:
    checked_proposal = _proposal_for_intent(intent, proposal)
    checked_context = context.model_copy(
        update={
            "recent_idempotency_keys": frozenset(),
            "price": context.price.model_copy(
                update={
                    "instrument_id": intent.broker_instrument_id,
                    "symbol": intent.symbol,
                }
            )
            if context.price is not None
            else None,
            "instrument": context.instrument.model_copy(
                update={
                    "instrument_id": intent.broker_instrument_id,
                    "symbol": intent.symbol,
                    "asset_class": checked_proposal.asset_class,
                }
            )
            if context.instrument is not None
            else None,
            "capital_envelope": AuthorizedCapitalEnvelope(
                authorized_capital_eur=Decimal("50"), managed_exposure_eur=Decimal("0")
            ),
        }
    )
    return EtoroDemoSubmissionPackage(
        proposal=checked_proposal,
        risk_context=checked_context,
        preflight=PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")),
    )


def _cycle() -> ActiveIntelligenceCycleRecord:
    timestamp = datetime(2026, 8, 31, 10, tzinfo=UTC)
    return ActiveIntelligenceCycleRecord(
        cycle_id="cycle-" + "a" * 64,
        scheduled_at=timestamp,
        started_at=timestamp,
        completed_at=timestamp,
        market_data_timestamp=timestamp,
        news_cutoff_timestamp=timestamp,
        symbols_evaluated=("AAPL",),
        positions_monitored=0,
        fresh_news_events=0,
        duplicate_events_ignored=0,
        material_events=0,
        global_risk_context={},
        top_opportunities=("AAPL",),
        watchlist=(),
        no_trade=(),
        rejected=(),
        data_health_state=DataHealthState.HEALTHY,
        decision_change_events=(),
        change_classification=CycleChangeClassification.MARKET_STATE_CHANGED,
        scanner_result={},
        shadow_capital=Decimal("200"),
        available_simulated_cash=Decimal("200"),
        existing_exposure=Decimal("0"),
        allocation_diagnostics=(),
        broker_write_calls=0,
        scan_cycle_timestamp=timestamp,
    )
