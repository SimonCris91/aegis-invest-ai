"""First controlled eToro Demo pre-flight; it never submits orders."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from pydantic import Field, field_validator

from app.agent.context import AegisAgentContext
from app.agent.models import AegisAgentResult
from app.agent.service import DeterministicAegisAgent
from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.demo import DEMO_ORDER_URL, assert_demo_route
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.mapping import (
    asset_class_from_etoro_instrument_type,
    classify_etoro_instrument_metadata,
)
from app.brokers.etoro.runtime import runtime_credentials, runtime_settings
from app.brokers.identity import AccountIdentityGuard
from app.brokers.market_validation import (
    MarketObservationError,
    validate_market_observation,
)
from app.brokers.models import (
    AccountKind,
    BrokerIdentity,
    DemoEligibility,
    DemoPortfolioSnapshot,
    InstrumentResolution,
    PreflightDecision,
)
from app.brokers.preflight import evaluate_demo_preflight
from app.config.models import ApplicationConfig
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import (
    AssetClass,
    Environment,
    MarketStatus,
    RiskDecisionStatus,
    SettlementType,
)
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SqliteRecordStore

CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_BLOCKED = "BLOCKED"
CHECK_NOT_CONFIGURED = "NOT_CONFIGURED"
CHECK_MARKET_CLOSED = "MARKET_CLOSED"
CHECK_WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
CHECK_NOT_ARMED = "NOT_ARMED"

PREFLIGHT_PASS = "PASS"
PREFLIGHT_FAIL = "FAIL"


class AegisAgent(Protocol):
    def analyze(self, context: AegisAgentContext) -> AegisAgentResult: ...


class NewsProvider(Protocol):
    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]: ...


class _EmptyNewsProvider:
    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]:
        return ()


class DemoPreflightCheck(FrozenDomainModel):
    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    timestamp: datetime
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")


class FirstDemoPreflightReport(FrozenDomainModel):
    generated_at: datetime
    stage: str = "STAGE_A_READINESS_PREFLIGHT"
    pre_flight: str = Field(min_length=1)
    pre_flight_stage_a: str = Field(min_length=1)
    blocker_code: str | None = Field(default=None, min_length=1)
    checks: tuple[DemoPreflightCheck, ...]
    broker: str = "ETORO"
    environment: str = "DEMO"
    simulated_funds: bool = True
    real_execution_available: bool = False
    instrument: str | None = Field(default=None, min_length=1)
    instrument_id: int | None = Field(default=None, gt=0)
    proposed_action: str | None = Field(default=None, min_length=1)
    proposal_id: str | None = Field(default=None, min_length=1)
    proposal_digest: str | None = Field(default=None, min_length=64, max_length=64)
    risk_policy_digest: str | None = Field(default=None, min_length=64, max_length=64)
    demo_amount: Decimal | None = Field(default=None, gt=0)
    reference_price: Decimal | None = Field(default=None, gt=0)
    minimum_broker_supported_amount: Decimal | None = Field(default=None, gt=0)
    demo_balance_before_trade: Decimal | None = Field(default=None, ge=0)
    projected_cash_reserve: Decimal | None = Field(default=None, ge=0)
    projected_position_exposure: Decimal | None = Field(default=None, ge=0)
    aegis_agent_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    aegis_agent_rationale: str | None = Field(default=None, min_length=1)
    news_diagnostics: dict[str, object] = Field(default_factory=dict)
    risk_manager_result: str | None = Field(default=None, min_length=1)
    market_status: str = "UNKNOWN"
    eligibility_result: str = "NOT_EVALUATED"
    kill_switch_status: str = Field(min_length=1)
    demo_environment_confirmation: bool
    ready_for_human_confirmation: bool = False
    execution_technically_ready: bool = False
    execution_arming_performed: bool = False
    final_authorization_minted: bool = False
    human_confirmation_required: bool = False
    demo_write_performed: bool = False
    real_write_performed: bool = False
    identity: BrokerIdentity | None = Field(default=None, exclude=True, repr=False)
    trade_proposal: TradeProposal | None = Field(default=None, exclude=True, repr=False)
    demo_portfolio: DemoPortfolioSnapshot | None = Field(default=None, exclude=True, repr=False)
    risk_portfolio: PortfolioSnapshot | None = Field(default=None, exclude=True, repr=False)
    market_quote: MarketQuote | None = Field(default=None, exclude=True, repr=False)
    demo_eligibility: DemoEligibility | None = Field(default=None, exclude=True, repr=False)
    instrument_metadata: InstrumentMetadata | None = Field(default=None, exclude=True, repr=False)

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "generated_at")


def build_first_demo_preflight_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    agent: AegisAgent | None = None,
    news_provider: NewsProvider | None = None,
    store: SqliteRecordStore | None = None,
    kill_switch: KillSwitch | None = None,
    risk_manager: RiskManager | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FirstDemoPreflightReport:
    now = (clock or (lambda: datetime.now(UTC)))()
    checks: list[DemoPreflightCheck] = []
    state = _PreflightState(kill_switch_status="UNKNOWN")

    def add_check(
        name: str, status: str, reason: str, metadata: Mapping[str, str] | None = None
    ) -> None:
        checks.append(
            DemoPreflightCheck(
                name=name,
                status=status,
                reason=reason,
                timestamp=now,
                metadata=dict(metadata or {}),
            )
        )

    switch = kill_switch or KillSwitch(
        active=config.kill_switch,
        reason="configured kill switch state",
        clock=lambda: now,
    )
    state.kill_switch_status = "ACTIVE" if switch.state.active else "CLEAR"
    state.demo_environment_confirmation = _demo_environment_confirmed(config)

    add_check(
        "demo_environment",
        CHECK_PASS if state.demo_environment_confirmation else CHECK_FAIL,
        "broker=ETORO, environment=DEMO, simulated funds, real execution absent"
        if state.demo_environment_confirmation
        else "Demo-only execution environment could not be proven",
        {
            "broker": "ETORO",
            "environment": Environment.DEMO.value,
            "simulated_funds": "true",
            "real_execution_available": "false",
        },
    )
    try:
        assert_demo_route(DEMO_ORDER_URL)
        add_check("demo_route_guard", CHECK_PASS, "exact Demo route guard is present")
    except RuntimeError:
        switch.activate("Demo route guard failed")
        add_check("demo_route_guard", CHECK_FAIL, "exact Demo route guard failed")
        return _finalize(now, checks, state, switch, store)

    # A read-only preflight must remain diagnostically useful while Demo
    # execution is disabled.  Only reject here when the configuration is
    # attempting to enable Demo execution without the complete guarded mode.
    demo_execution_configuration_incomplete = config.etoro_demo_execution_enabled and (
        config.broker_execution_mode.value != "DEMO_EXECUTION"
        or not config.demo_smoke_test_opt_in
    )
    if demo_execution_configuration_incomplete:
        add_check(
            "demo_execution_disabled",
            CHECK_FAIL,
            "Demo execution requires the guarded Demo mode and explicit human confirmation",
        )
        return _finalize(now, checks, state, switch, store)
    add_check(
        "demo_execution_disabled",
        CHECK_PASS,
        "read-only preflight is allowed while Demo execution is disabled"
        if not config.etoro_demo_execution_enabled
        else "Demo execution has explicit human confirmation",
        {
            "etoro_demo_execution_enabled": str(config.etoro_demo_execution_enabled).lower(),
            "demo_smoke_test_opt_in": str(config.demo_smoke_test_opt_in).lower(),
            "broker_execution_mode": config.broker_execution_mode.value,
        },
    )

    if store is not None and store.unresolved_demo_submissions():
        add_check(
            "restart_replay_guard",
            CHECK_FAIL,
            "unresolved Demo submission must be reconciled before another pre-flight",
        )
        return _finalize(now, checks, state, switch, store)
    add_check("restart_replay_guard", CHECK_PASS, "no unresolved Demo submission is pending")

    try:
        settings = runtime_settings(values)
    except ValueError as exc:
        add_check("runtime_configuration", CHECK_FAIL, str(exc))
        return _finalize(now, checks, state, switch, store)

    credentials = runtime_credentials(values)
    if credentials is None:
        add_check("credentials", CHECK_NOT_CONFIGURED, "eToro API credentials are not configured")
        return _finalize(now, checks, state, switch, store)
    add_check("credentials", CHECK_PASS, "eToro credential placeholders are configured")

    if not config.etoro_api_enabled:
        add_check("api_enablement", CHECK_NOT_CONFIGURED, "ETORO_API_ENABLED is false")
        return _finalize(now, checks, state, switch, store)
    add_check("api_enablement", CHECK_PASS, "official eToro API reads are enabled")

    read_client = client or _live_client(credentials, config)
    try:
        identity = read_client.identity()
        state.identity = identity
        add_check("authentication", CHECK_PASS, "authenticated profile was read")
    except EtoroApiError as exc:
        add_check(
            "authentication",
            CHECK_FAIL,
            f"authenticated profile could not be read: {exc.category.value}",
            exc.safe_metadata(),
        )
        return _finalize(now, checks, state, switch, store)
    except (RuntimeError, ValueError):
        add_check("authentication", CHECK_FAIL, "authenticated profile could not be read")
        return _finalize(now, checks, state, switch, store)

    if not settings.expected_username_configured and not settings.expected_gcid_configured:
        add_check("identity_guard", CHECK_BLOCKED, "no expected eToro identity is configured")
        return _finalize(now, checks, state, switch, store)
    if not _identity_matches(identity, settings.expected_username, settings.expected_gcid):
        switch.activate("identity mismatch")
        add_check("identity_guard", CHECK_FAIL, "authenticated identity mismatch")
        return _finalize(now, checks, state, switch, store)
    add_check("identity_guard", CHECK_PASS, "authenticated identity matches expected identity")

    if settings.readiness_symbol is None:
        add_check("instrument_resolution", CHECK_BLOCKED, "no readiness symbol is configured")
        return _finalize(now, checks, state, switch, store)

    try:
        demo_portfolio = read_client.demo_account(identity)
        AccountIdentityGuard(identity, AccountKind.DEMO, switch).verify(
            identity, demo_portfolio.context
        )
        state.demo_balance_before_trade = demo_portfolio.cash
        state.demo_portfolio = demo_portfolio
        add_check("demo_portfolio", CHECK_PASS, "Demo aggregate portfolio was read")
    except EtoroApiError as exc:
        add_check(
            "demo_portfolio",
            CHECK_FAIL,
            f"Demo aggregate portfolio could not be read: {exc.category.value}",
            exc.safe_metadata(),
        )
        return _finalize(now, checks, state, switch, store)
    except (RuntimeError, ValueError):
        add_check("demo_portfolio", CHECK_FAIL, "Demo aggregate portfolio could not be verified")
        return _finalize(now, checks, state, switch, store)

    try:
        read_client.real_portfolio_read_only({})
        add_check(
            "real_portfolio_read_only",
            CHECK_PASS,
            "Real portfolio was read only for account readiness validation",
        )
    except EtoroApiError as exc:
        add_check(
            "real_portfolio_read_only",
            CHECK_FAIL,
            f"Real portfolio read-only check failed: {exc.category.value}",
            exc.safe_metadata(),
        )
        return _finalize(now, checks, state, switch, store)
    except (RuntimeError, ValueError):
        add_check(
            "real_portfolio_read_only",
            CHECK_FAIL,
            "Real portfolio read-only check could not be verified",
        )
        return _finalize(now, checks, state, switch, store)

    try:
        resolution = read_client.resolve_instrument(settings.readiness_symbol, as_of=now)
        if (
            settings.readiness_instrument_id is not None
            and resolution.instrument_id != settings.readiness_instrument_id
        ):
            add_check("instrument_resolution", CHECK_FAIL, "resolved instrument ID mismatch")
            return _finalize(now, checks, state, switch, store)
        if not resolution.structurally_supported:
            add_check(
                "instrument_resolution",
                CHECK_FAIL,
                f"instrument resolution failed: {resolution.structural_status}",
            )
            return _finalize(now, checks, state, switch, store)
        state.instrument = resolution.internal_symbol_full
        state.instrument_id = resolution.instrument_id
        state.market_status = _preflight_market_status(resolution).value
        add_check(
            "instrument_resolution",
            CHECK_PASS,
            "configured symbol resolved exactly to a valid instrument ID",
            {
                "instrument_id": str(resolution.instrument_id),
                "symbol": resolution.internal_symbol_full,
            },
        )
    except EtoroApiError as exc:
        add_check(
            "instrument_resolution",
            CHECK_FAIL,
            f"instrument could not be resolved: {exc.category.value}",
            exc.safe_metadata(),
        )
        return _finalize(now, checks, state, switch, store)
    except (RuntimeError, ValueError):
        add_check("instrument_resolution", CHECK_FAIL, "instrument could not be resolved")
        return _finalize(now, checks, state, switch, store)

    try:
        quote = read_client.quote(resolution.instrument_id, resolution.internal_symbol_full)
        quote = quote.model_copy(update={"market_status": _preflight_market_status(resolution)})
        reference_price = quote.ask or quote.price
        state.reference_price = reference_price
        state.market_quote = quote
        if quote.market_status is not MarketStatus.OPEN:
            add_check(
                "market_state",
                CHECK_MARKET_CLOSED,
                "instrument market is not open for the intended Demo operation",
                {"market_status": quote.market_status.value},
            )
            return _finalize(now, checks, state, switch, store)
        add_check("market_state", CHECK_PASS, "instrument market is open")
        validate_market_observation(
            quote,
            _instrument_from_resolution(resolution, now),
            now=read_client.last_server_now or now,
            maximum_age_seconds=settings.maximum_quote_age_seconds,
            maximum_future_skew_seconds=runtime_settings(values).maximum_future_quote_skew_seconds,
        )
    except EtoroApiError as exc:
        add_check(
            "market_rates",
            CHECK_FAIL,
            f"fresh live market rate could not be read: {exc.category.value}",
            exc.safe_metadata(),
        )
        return _finalize(now, checks, state, switch, store)
    except MarketObservationError as exc:
        add_check(
            "market_rates",
            CHECK_FAIL,
            f"fresh live market rate could not be verified: {exc}",
            {"validation_error": str(exc)},
        )
        return _finalize(now, checks, state, switch, store)
    except (RuntimeError, ValueError):
        add_check("market_rates", CHECK_FAIL, "fresh live market rate could not be verified")
        return _finalize(now, checks, state, switch, store)

    add_check("market_rates", CHECK_PASS, "fresh live market rate was normalized")

    try:
        eligibility = read_client.demo_eligibility(
            resolution.instrument_id, resolution.internal_symbol_full
        )
        eligibility_reasons = _eligibility_blockers(eligibility)
        state.minimum_broker_supported_amount = eligibility.minimum_position
        state.eligibility_result = "APPROVED" if not eligibility_reasons else "REJECTED"
        state.demo_eligibility = eligibility
        if eligibility_reasons:
            add_check(
                "demo_eligibility",
                CHECK_FAIL,
                "Demo eligibility blocks opening this instrument",
                {"reasons": "; ".join(eligibility_reasons)},
            )
            return _finalize(now, checks, state, switch, store)
        add_check("demo_eligibility", CHECK_PASS, "Demo eligibility permits opening")
    except EtoroApiError as exc:
        add_check(
            "demo_eligibility",
            CHECK_FAIL,
            f"Demo eligibility could not be read: {exc.category.value}",
            exc.safe_metadata(),
        )
        return _finalize(now, checks, state, switch, store)
    except (RuntimeError, ValueError):
        add_check("demo_eligibility", CHECK_FAIL, "Demo eligibility could not be verified")
        return _finalize(now, checks, state, switch, store)

    add_check(
        "stage_a_kill_switch",
        CHECK_PASS,
        "kill switch state is accepted for read-only Stage A",
        {"kill_switch": "ACTIVE" if switch.state.active else "CLEAR"},
    )

    demo_portfolio_for_risk = _portfolio_from_demo_snapshot(
        demo_portfolio,
        target_instrument_id=resolution.instrument_id,
        target_symbol=resolution.internal_symbol_full,
    )
    instrument = _instrument_from_eligibility(resolution, eligibility, now)
    state.risk_portfolio = demo_portfolio_for_risk
    state.instrument_metadata = instrument
    provider = news_provider or _EmptyNewsProvider()
    context_news = provider.get_news((resolution.internal_symbol_full,), as_of=now)
    provider_diagnostics = getattr(provider, "diagnostics", {})
    state.news_diagnostics = (
        dict(provider_diagnostics) if isinstance(provider_diagnostics, Mapping) else {}
    )
    state.news_diagnostics["context_news_count"] = len(context_news)
    news_unavailable_demo_override = (
        not context_news
        and str(state.news_diagnostics.get("news_provider_status", "")).startswith("RATE_LIMITED")
    )
    agent_impl = agent or DeterministicAegisAgent(
        allow_news_unavailable_demo=news_unavailable_demo_override
    )
    agent_result = agent_impl.analyze(
        AegisAgentContext(
            portfolio=demo_portfolio_for_risk,
            quotes=(quote,),
            news=context_news,
            instruments=(instrument,),
            analysis_timestamp=now,
            strategy=config.strategy,
            minimum_trade_amount=eligibility.minimum_position,
        )
    )
    state.aegis_agent_confidence = agent_result.analysis.confidence
    state.aegis_agent_rationale = agent_result.analysis.rationale
    if agent_result.proposal is None:
        add_check(
            "aegis_agent",
            CHECK_FAIL,
            "DEMO SMOKE TEST NOT EXECUTED - AEGIS DID NOT PROPOSE A VALID TRADE",
        )
        return _finalize(now, checks, state, switch, store)
    proposal = agent_result.proposal
    state.trade_proposal = proposal
    state.proposed_action = f"{proposal.intent.value}/{proposal.side.value}"
    state.proposal_id = str(proposal.proposal_id)
    state.demo_amount = proposal.amount
    state.projected_cash_reserve, state.projected_position_exposure = _projected_weights(
        demo_portfolio_for_risk, proposal
    )
    add_check("aegis_agent", CHECK_PASS, "Aegis Agent produced a TradeProposal")

    existing_keys = store.demo_submission_keys() if store is not None else frozenset()
    manager = risk_manager or RiskManager(
        config.risk,
        switch,
        authorization_key=b"first-demo-preflight-risk-key-32!!",
        clock=lambda: now,
    )
    risk_context = RiskContext(
        evaluated_at=now,
        portfolio=demo_portfolio_for_risk,
        price=quote.to_price_snapshot(),
        instrument=instrument,
        market_data_available=True,
        news_data_available=bool(agent_result.analysis.supporting_factors),
        daily_new_trade_count=_daily_demo_trade_count(store),
        recent_idempotency_keys=existing_keys,
        api_state_consistent=True,
    )
    evaluation = manager.evaluate(
        proposal,
        risk_context,
        issue_authorization=False,
        ignore_kill_switch=True,
    )
    state.risk_manager_result = evaluation.decision.status.value
    state.proposal_digest = manager.proposal_digest(proposal)
    state.risk_policy_digest = manager.policy_digest
    if evaluation.decision.status is not RiskDecisionStatus.APPROVED:
        add_check(
            "risk_manager",
            CHECK_FAIL,
            "Risk Manager rejected the TradeProposal",
            {"violations": ",".join(v.code.value for v in evaluation.decision.violations)},
        )
        return _finalize(now, checks, state, switch, store)
    assert evaluation.authorization is None
    add_check(
        "risk_manager",
        CHECK_PASS,
        "Risk Manager preliminary evaluation approved without final authorization",
        {
            "proposal_digest": state.proposal_digest,
            "policy_digest": manager.policy_digest,
            "authorization_deferred": "true",
        },
    )
    add_check(
        "execution_arming",
        CHECK_NOT_ARMED,
        "Stage A does not mint or consume the final execution authorization",
        {"final_authorization_minted": "false", "execution_arming_performed": "false"},
    )

    preflight = evaluate_demo_preflight(
        proposal=proposal,
        portfolio=demo_portfolio,
        eligibility=eligibility,
        quote=quote,
        instrument=instrument,
        kill_switch=KillSwitch(
            active=False,
            reason="Stage A non-execution feasibility check",
            clock=lambda: now,
        ),
        now=now,
        maximum_age_seconds=settings.maximum_quote_age_seconds,
    )
    state.preflight_decision = preflight
    if not preflight.allowed:
        add_check(
            "stage_a_feasibility",
            CHECK_FAIL,
            "Stage A feasibility rejected the proposal",
            {"reasons": "; ".join(preflight.reasons)},
        )
        return _finalize(now, checks, state, switch, store)
    unit_blocker = _max_units_blocker(eligibility, quote, proposal)
    if unit_blocker is not None:
        add_check("stage_a_feasibility", CHECK_FAIL, unit_blocker)
        return _finalize(now, checks, state, switch, store)
    add_check("stage_a_feasibility", CHECK_PASS, "Stage A non-execution feasibility passed")

    add_check(
        "human_confirmation",
        CHECK_WAITING_CONFIRMATION,
        "pre-flight passed; waiting for explicit confirmation before any Demo write",
    )
    state.ready_for_human_confirmation = True
    state.human_confirmation_required = True
    return _finalize(now, checks, state, switch, store)


class _PreflightState:
    def __init__(self, *, kill_switch_status: str) -> None:
        self.kill_switch_status = kill_switch_status
        self.identity: BrokerIdentity | None = None
        self.demo_environment_confirmation = False
        self.instrument: str | None = None
        self.instrument_id: int | None = None
        self.proposed_action: str | None = None
        self.proposal_id: str | None = None
        self.proposal_digest: str | None = None
        self.risk_policy_digest: str | None = None
        self.demo_amount: Decimal | None = None
        self.reference_price: Decimal | None = None
        self.minimum_broker_supported_amount: Decimal | None = None
        self.demo_balance_before_trade: Decimal | None = None
        self.projected_cash_reserve: Decimal | None = None
        self.projected_position_exposure: Decimal | None = None
        self.aegis_agent_confidence: Decimal | None = None
        self.aegis_agent_rationale: str | None = None
        self.news_diagnostics: dict[str, object] = {}
        self.risk_manager_result: str | None = None
        self.market_status = "UNKNOWN"
        self.eligibility_result = "NOT_EVALUATED"
        self.ready_for_human_confirmation = False
        self.execution_technically_ready = False
        self.execution_arming_performed = False
        self.final_authorization_minted = False
        self.human_confirmation_required = False
        self.preflight_decision: PreflightDecision | None = None
        self.trade_proposal: TradeProposal | None = None
        self.demo_portfolio: DemoPortfolioSnapshot | None = None
        self.risk_portfolio: PortfolioSnapshot | None = None
        self.market_quote: MarketQuote | None = None
        self.demo_eligibility: DemoEligibility | None = None
        self.instrument_metadata: InstrumentMetadata | None = None


def _finalize(
    generated_at: datetime,
    checks: list[DemoPreflightCheck],
    state: _PreflightState,
    kill_switch: KillSwitch,
    store: SqliteRecordStore | None,
) -> FirstDemoPreflightReport:
    blocked = any(
        check.status in {CHECK_FAIL, CHECK_BLOCKED, CHECK_NOT_CONFIGURED, CHECK_MARKET_CLOSED}
        for check in checks
    )
    blocker = _blocker_code(checks)
    report = FirstDemoPreflightReport(
        generated_at=generated_at,
        pre_flight=PREFLIGHT_FAIL if blocked else PREFLIGHT_PASS,
        pre_flight_stage_a=PREFLIGHT_FAIL if blocked else PREFLIGHT_PASS,
        blocker_code=blocker,
        checks=tuple(checks),
        instrument=state.instrument,
        instrument_id=state.instrument_id,
        proposed_action=state.proposed_action,
        proposal_id=state.proposal_id,
        proposal_digest=state.proposal_digest,
        risk_policy_digest=state.risk_policy_digest,
        demo_amount=state.demo_amount,
        reference_price=state.reference_price,
        minimum_broker_supported_amount=state.minimum_broker_supported_amount,
        demo_balance_before_trade=state.demo_balance_before_trade,
        projected_cash_reserve=state.projected_cash_reserve,
        projected_position_exposure=state.projected_position_exposure,
        aegis_agent_confidence=state.aegis_agent_confidence,
        aegis_agent_rationale=state.aegis_agent_rationale,
        news_diagnostics=state.news_diagnostics,
        risk_manager_result=state.risk_manager_result,
        market_status=state.market_status,
        eligibility_result=state.eligibility_result,
        kill_switch_status="ACTIVE" if kill_switch.state.active else state.kill_switch_status,
        demo_environment_confirmation=state.demo_environment_confirmation,
        ready_for_human_confirmation=state.ready_for_human_confirmation and not blocked,
        execution_technically_ready=state.execution_technically_ready and not blocked,
        execution_arming_performed=state.execution_arming_performed and not blocked,
        final_authorization_minted=state.final_authorization_minted and not blocked,
        human_confirmation_required=state.human_confirmation_required and not blocked,
        identity=state.identity,
        trade_proposal=state.trade_proposal,
        demo_portfolio=state.demo_portfolio,
        risk_portfolio=state.risk_portfolio,
        market_quote=state.market_quote,
        demo_eligibility=state.demo_eligibility,
        instrument_metadata=state.instrument_metadata,
    )
    if store is not None:
        store.append("etoro-first-demo-preflight", report.model_dump(mode="json"))
    return report


def _blocker_code(checks: list[DemoPreflightCheck]) -> str | None:
    for check in checks:
        if check.status == CHECK_MARKET_CLOSED:
            return "MARKET_OR_ELIGIBILITY_BLOCK"
        if check.status in {CHECK_FAIL, CHECK_BLOCKED, CHECK_NOT_CONFIGURED}:
            if check.metadata.get("category") == "NETWORK_TRANSPORT_ERROR":
                return "NETWORK_TRANSPORT_ERROR"
            if check.name in {"instrument_resolution", "market_rates", "demo_eligibility"}:
                return "MARKET_OR_ELIGIBILITY_BLOCK"
            return check.name.upper()
    return None


def _live_client(credentials: EtoroCredentials, config: ApplicationConfig) -> EtoroReadClient:
    return EtoroReadClient(
        credentials, DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
    )


def _demo_environment_confirmed(config: ApplicationConfig) -> bool:
    return config.environment is Environment.DEMO and not config.production_trading_enabled


def _identity_matches(
    identity: object, expected_username: str | None, expected_gcid: str | None
) -> bool:
    stable_user_id = getattr(identity, "stable_user_id", None)
    username = getattr(identity, "username", None)
    username_matches = expected_username is None or (
        isinstance(username, str) and username.casefold() == expected_username.casefold()
    )
    gcid_matches = expected_gcid is None or stable_user_id == expected_gcid
    return username_matches and gcid_matches


def _preflight_market_status(resolution: InstrumentResolution) -> MarketStatus:
    asset_class = asset_class_from_etoro_instrument_type(resolution.instrument_type)
    if asset_class is AssetClass.UNKNOWN:
        asset_class = classify_etoro_instrument_metadata(
            resolution.classification_metadata
        ).asset_class
    if asset_class is not AssetClass.CRYPTO:
        return resolution.market_status

    from app.brokers.etoro.live_candidates import _crypto_tradability_rejection

    rejection = _crypto_tradability_rejection(
        {
            "isCurrentlyTradable": resolution.is_currently_tradable,
            "isBuyEnabled": resolution.is_buy_enabled,
            "isActiveInPlatform": resolution.is_active_in_platform,
            "isInternalInstrument": resolution.is_internal_instrument,
            "isHiddenFromClient": resolution.is_hidden_from_client,
            "isDelisted": resolution.is_delisted,
        }
    )
    return MarketStatus.OPEN if rejection is None else MarketStatus.UNKNOWN


def _instrument_from_resolution(
    resolution: InstrumentResolution, now: datetime
) -> InstrumentMetadata:
    asset_class = asset_class_from_etoro_instrument_type(resolution.instrument_type)
    if asset_class is AssetClass.UNKNOWN:
        asset_class = classify_etoro_instrument_metadata(
            resolution.classification_metadata
        ).asset_class
    return InstrumentMetadata(
        instrument_id=resolution.instrument_id,
        symbol=resolution.internal_symbol_full,
        asset_class=asset_class,
        settlement_type=SettlementType.REAL,
        is_valid=resolution.structurally_supported,
        is_tradable=resolution.structurally_supported,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        metadata_as_of=now,
        source="etoro-official-api-search",
    )


def _instrument_from_eligibility(
    resolution: InstrumentResolution,
    eligibility: DemoEligibility,
    now: datetime,
) -> InstrumentMetadata:
    asset_class = asset_class_from_etoro_instrument_type(resolution.instrument_type)
    if asset_class is AssetClass.UNKNOWN:
        asset_class = classify_etoro_instrument_metadata(
            resolution.classification_metadata
        ).asset_class
    return InstrumentMetadata(
        instrument_id=resolution.instrument_id,
        symbol=resolution.internal_symbol_full,
        asset_class=asset_class,
        settlement_type=SettlementType.REAL,
        is_valid=resolution.structurally_supported and eligibility.verified,
        is_tradable=resolution.structurally_supported and eligibility.verified,
        allows_long=eligibility.allow_open,
        allows_short=False,
        allowed_leverages=(eligibility.leverage,),
        min_position_amount=eligibility.minimum_position,
        metadata_as_of=now,
        source="etoro-official-api-demo-eligibility",
    )


def _portfolio_from_demo_snapshot(
    snapshot: DemoPortfolioSnapshot, *, target_instrument_id: int, target_symbol: str
) -> PortfolioSnapshot:
    positions = tuple(
        Position(
            position_id=str(position.instrument_id),
            instrument_id=position.instrument_id,
            symbol=(
                target_symbol
                if position.instrument_id == target_instrument_id
                else f"INSTRUMENT-{position.instrument_id}"
            ),
            settlement_type=SettlementType.REAL,
            units=position.units,
            average_entry_price=position.average_open_rate,
            market_price=position.current_exposure / position.units,
        )
        for position in snapshot.positions
        if position.units > 0 and position.current_exposure > 0
    )
    return PortfolioSnapshot(
        as_of=snapshot.as_of,
        currency=snapshot.currency,
        cash=snapshot.cash,
        positions=positions,
        reported_total_value=snapshot.total_value,
        peak_value=snapshot.total_value if snapshot.total_value > 0 else None,
    )


def _eligibility_blockers(eligibility: DemoEligibility) -> tuple[str, ...]:
    reasons: list[str] = []
    if not eligibility.verified:
        reasons.append("eligibility is not verified")
    if not eligibility.allow_open:
        reasons.append("allowOpenPosition is false")
    if eligibility.settlement_type is not SettlementType.REAL:
        reasons.append("real-asset settlement is unavailable")
    if eligibility.leverage != 1:
        reasons.append("unleveraged Demo trading is unavailable")
    if eligibility.allowed_order_quantity_types and not _allows_amount_order(
        eligibility.allowed_order_quantity_types
    ):
        reasons.append("amount order quantity type is not supported")
    return tuple(reasons)


def _allows_amount_order(values: tuple[str, ...]) -> bool:
    normalized = {value.strip().casefold().replace("_", "").replace("-", "") for value in values}
    return bool(normalized & {"all", "amount", "cash", "byamount", "amountorder"})


def _max_units_blocker(
    eligibility: DemoEligibility, quote: MarketQuote, proposal: TradeProposal
) -> str | None:
    if eligibility.max_units_per_order is None:
        return None
    reference = quote.ask or quote.price
    proposed_units = proposal.amount / reference
    if proposed_units > eligibility.max_units_per_order:
        return "proposal exceeds maxUnitsPerOrder"
    return None


def _projected_weights(
    portfolio: PortfolioSnapshot, proposal: TradeProposal
) -> tuple[Decimal | None, Decimal | None]:
    if portfolio.total_value <= 0:
        return None, None
    projected_cash = portfolio.cash - proposal.amount
    projected_position = portfolio.market_value_for(proposal.instrument_id) + proposal.amount
    return projected_cash / portfolio.total_value, projected_position / portfolio.total_value


def _daily_demo_trade_count(store: SqliteRecordStore | None) -> int:
    if store is None:
        return 0
    return sum(
        1
        for record in store.list("track-record:ETORO_DEMO")
        if bool(record.get("demo_execution", False))
    )


def default_demo_preflight_store(path: Path | None = None) -> SqliteRecordStore:
    target = path or Path("work") / "first-demo-preflight.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    return SqliteRecordStore(target)
