"""Explicit Stage B arming for one controlled eToro Demo submission."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.demo import EtoroDemoAdapter
from app.brokers.etoro.demo_preflight import (
    AegisAgent,
    DemoPreflightCheck,
    FirstDemoPreflightReport,
    NewsProvider,
    build_first_demo_preflight_report,
    default_demo_preflight_store,
)
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.runtime import runtime_credentials
from app.brokers.models import (
    BrokerIdentity,
    BrokerSubmission,
    DemoEligibility,
    DemoPortfolioSnapshot,
    ExecutionState,
)
from app.brokers.preflight import evaluate_demo_preflight
from app.brokers.reconciliation import ReconciliationError, reconcile_demo_state
from app.config.models import ApplicationConfig
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import RiskDecisionStatus
from app.domain.market import InstrumentMetadata, MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.execution.gate import RiskEnforcedExecutionGate
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SqliteRecordStore


class DemoExecutionArmingError(RuntimeError):
    """Raised when Stage B cannot be armed safely."""


type StageAMaterial = tuple[
    BrokerIdentity,
    TradeProposal,
    DemoPortfolioSnapshot,
    PortfolioSnapshot,
    MarketQuote,
    DemoEligibility,
    InstrumentMetadata,
]


class DemoExecutionConfirmation(FrozenDomainModel):
    proposal_id: str = Field(min_length=1)
    proposal_digest: str = Field(min_length=64, max_length=64)
    risk_policy_digest: str = Field(min_length=64, max_length=64)
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    environment: str = "DEMO"
    simulated_funds: bool = True
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "confirmed_at")


class DemoStageBResult(FrozenDomainModel):
    generated_at: datetime
    stage: str = "STAGE_B_EXECUTION_ARMING"
    stage_a_pre_flight: str
    ready_for_human_confirmation: bool
    execution_arming_performed: bool = False
    final_authorization_minted: bool = False
    demo_submission_attempts: int = 0
    demo_write_performed: bool = False
    real_write_performed: bool = False
    broker_order_id: str | None = None
    broker_state: str | None = None
    final_state: str = "NOT_EXECUTED"
    kill_switch_status: str
    checks: tuple[DemoPreflightCheck, ...]

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "generated_at")


class OneShotDemoExecutionWindow:
    def __init__(self, kill_switch: KillSwitch, *, confirmed: bool) -> None:
        self._kill_switch = kill_switch
        self._confirmed = confirmed

    def __enter__(self) -> "OneShotDemoExecutionWindow":
        if not self._confirmed:
            raise DemoExecutionArmingError("explicit human confirmation is required")
        if not self._kill_switch.state.active:
            raise DemoExecutionArmingError("kill switch must start ACTIVE before one-shot arming")
        self._kill_switch.deactivate("explicit confirmed Demo execution window")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._kill_switch.activate("safe default after controlled Demo execution")


def arm_and_submit_confirmed_demo_once(
    config: ApplicationConfig,
    confirmation: DemoExecutionConfirmation,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    agent: AegisAgent | None = None,
    news_provider: NewsProvider | None = None,
    store: SqliteRecordStore | None = None,
    kill_switch: KillSwitch | None = None,
    http: DisciplinedHttpClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DemoStageBResult:
    now = (clock or (lambda: datetime.now(UTC)))()
    checks: list[DemoPreflightCheck] = []
    switch = kill_switch or KillSwitch(active=True, reason="safe default", clock=lambda: now)
    registry = store or default_demo_preflight_store()

    def add_check(name: str, status: str, reason: str) -> None:
        checks.append(
            DemoPreflightCheck(
                name=name,
                status=status,
                reason=reason,
                timestamp=now,
            )
        )

    stage_a = build_first_demo_preflight_report(
        config,
        values=values,
        client=client,
        agent=agent,
        news_provider=news_provider,
        store=registry,
        kill_switch=switch,
        clock=lambda: now,
    )
    checks.extend(stage_a.checks)

    credentials = runtime_credentials(values)
    if credentials is None:
        add_check("stage_b_credentials", "FAIL", "eToro credentials are not configured")
        return _stage_b_result(now, stage_a, switch, checks, final_state="NOT_EXECUTED")
    if not stage_a.ready_for_human_confirmation:
        add_check("stage_b_readiness", "FAIL", "Stage A is not ready for human confirmation")
        return _stage_b_result(now, stage_a, switch, checks, final_state="NOT_EXECUTED")
    if not _confirmation_matches_stage_a(confirmation, stage_a):
        add_check("human_confirmation", "FAIL", "confirmation does not match Stage A proposal")
        return _stage_b_result(now, stage_a, switch, checks, final_state="NOT_EXECUTED")

    material = _stage_a_material(stage_a)
    if material is None:
        add_check("stage_b_material", "FAIL", "Stage A material is unavailable after restart")
        return _stage_b_result(now, stage_a, switch, checks, final_state="NOT_EXECUTED")

    (
        identity,
        proposal,
        demo_portfolio,
        risk_portfolio,
        quote,
        eligibility,
        instrument,
    ) = material
    read_client = client or EtoroReadClient(
        credentials, http or DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
    )
    risk_manager = RiskManager(
        config.risk,
        switch,
        authorization_key=b"stage-b-demo-risk-authorization-key!",
        clock=lambda: now,
    )
    submission: BrokerSubmission | None = None
    final_state = "NOT_EXECUTED"
    final_authorization_minted = False
    attempts = 0
    armed = False

    try:
        with OneShotDemoExecutionWindow(switch, confirmed=True):
            armed = True
            risk_context = RiskContext(
                evaluated_at=now,
                portfolio=risk_portfolio,
                price=quote.to_price_snapshot(),
                instrument=instrument,
                market_data_available=True,
                news_data_available=bool(proposal.evidence),
                daily_new_trade_count=0,
                recent_idempotency_keys=registry.demo_submission_keys(),
                api_state_consistent=True,
            )
            evaluation = risk_manager.evaluate(proposal, risk_context)
            if evaluation.decision.status is not RiskDecisionStatus.APPROVED:
                add_check("stage_b_risk_manager", "FAIL", "Risk Manager rejected current state")
                final_state = "RISK_REJECTED"
            else:
                assert evaluation.authorization is not None
                final_authorization_minted = True
                gate = RiskEnforcedExecutionGate(risk_manager)
                authorized = gate.admit(proposal, evaluation.authorization, at=now)
                add_check("stage_b_risk_manager", "PASS", "fresh authorization was minted")

                preflight = evaluate_demo_preflight(
                    proposal=proposal,
                    portfolio=demo_portfolio,
                    eligibility=eligibility,
                    quote=quote,
                    instrument=instrument,
                    kill_switch=switch,
                    now=now,
                    maximum_age_seconds=config.risk.max_price_age_seconds,
                )
                if not preflight.allowed:
                    add_check("stage_b_preflight", "FAIL", "fresh execution pre-flight failed")
                    final_state = "PREFLIGHT_REJECTED"
                else:
                    add_check("stage_b_preflight", "PASS", "fresh execution pre-flight passed")

                    adapter = EtoroDemoAdapter(
                        credentials=credentials,
                        http=http
                        or DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
                        gate=gate,
                        kill_switch=switch,
                        registry=registry,
                        enabled=True,
                        explicit_opt_in=True,
                        clock=lambda: now,
                    )
                    attempts = 1
                    submission = adapter.submit_demo(authorized, preflight)
                    final_state = (
                        "UNKNOWN_EXECUTION_STATE"
                        if submission.state is ExecutionState.UNKNOWN
                        else submission.state.value
                    )
                    add_check(
                        "demo_submission",
                        "PASS",
                        "exactly one Demo submission was attempted",
                    )

                    if submission.broker_order_id is not None:
                        broker_state = _read_order_state(
                            read_client,
                            identity,
                            proposal.instrument_id,
                            submission.broker_order_id,
                        )
                        final_state = reconcile_demo_state(
                            submission, broker_state, switch, registry
                        ).value
    except ReconciliationError:
        switch.activate("Demo reconciliation failed")
        final_state = "RECONCILIATION_FAILED"
    except (DemoExecutionArmingError, EtoroApiError, RuntimeError, ValueError):
        switch.activate("controlled Demo execution failed")
        final_state = "UNKNOWN_EXECUTION_STATE" if attempts else "NOT_EXECUTED"

    return _stage_b_result(
        now,
        stage_a,
        switch,
        checks,
        final_state=final_state,
        final_authorization_minted=final_authorization_minted,
        submission=submission,
        attempts=attempts,
        armed=armed,
    )


def _stage_a_material(stage_a: FirstDemoPreflightReport) -> StageAMaterial | None:
    if (
        stage_a.identity is None
        or stage_a.trade_proposal is None
        or stage_a.demo_portfolio is None
        or stage_a.risk_portfolio is None
        or stage_a.market_quote is None
        or stage_a.demo_eligibility is None
        or stage_a.instrument_metadata is None
    ):
        return None
    return (
        stage_a.identity,
        stage_a.trade_proposal,
        stage_a.demo_portfolio,
        stage_a.risk_portfolio,
        stage_a.market_quote,
        stage_a.demo_eligibility,
        stage_a.instrument_metadata,
    )


def _read_order_state(
    client: EtoroReadClient,
    identity: BrokerIdentity,
    instrument_id: int,
    broker_order_id: str,
) -> ExecutionState:
    return client.demo_order_state(identity, instrument_id, broker_order_id)


def _confirmation_matches_stage_a(
    confirmation: DemoExecutionConfirmation, stage_a: FirstDemoPreflightReport
) -> bool:
    return (
        confirmation.environment == "DEMO"
        and confirmation.simulated_funds
        and confirmation.proposal_id == stage_a.proposal_id
        and confirmation.proposal_digest == stage_a.proposal_digest
        and confirmation.risk_policy_digest == stage_a.risk_policy_digest
        and confirmation.instrument_id == stage_a.instrument_id
        and confirmation.symbol == stage_a.instrument
        and confirmation.amount == stage_a.demo_amount
    )


def _stage_b_result(
    now: datetime,
    stage_a: FirstDemoPreflightReport,
    switch: KillSwitch,
    checks: list[DemoPreflightCheck],
    *,
    final_state: str,
    final_authorization_minted: bool = False,
    submission: BrokerSubmission | None = None,
    attempts: int = 0,
    armed: bool = False,
) -> DemoStageBResult:
    return DemoStageBResult(
        generated_at=now,
        stage_a_pre_flight=stage_a.pre_flight_stage_a,
        ready_for_human_confirmation=stage_a.ready_for_human_confirmation,
        execution_arming_performed=armed,
        final_authorization_minted=final_authorization_minted,
        demo_submission_attempts=attempts,
        demo_write_performed=attempts > 0,
        real_write_performed=False,
        broker_order_id=submission.broker_order_id if submission else None,
        broker_state=submission.state.value if submission else None,
        final_state=final_state,
        kill_switch_status="ACTIVE" if switch.state.active else "CLEAR",
        checks=tuple(checks),
    )
