"""Explicit Stage B arming for one controlled eToro Demo submission."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
from app.brokers.etoro.runtime import runtime_credentials, runtime_settings
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
from app.domain.enums import (
    AssetClass,
    BrokerExecutionMode,
    OperatingMode,
    ProviderMode,
    RiskDecisionStatus,
)
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import AuthorizedCapitalEnvelope, RiskContext
from app.domain.universe import UniversalInstrument
from app.execution.gate import RiskEnforcedExecutionGate
from app.news.alpaca import AlpacaNewsProvider
from app.news.alpha_vantage import AlphaVantageNewsProvider
from app.news.crosscheck import CrossCheckedNewsProvider
from app.news.intelligence import (
    GlobalNewsIntelligenceEngine,
    GlobalNewsProvider,
    NewsProviderStatus,
)
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SqliteRecordStore


class DemoExecutionArmingError(RuntimeError):
    """Raised when Stage B cannot be armed safely."""


class _RunNewsSession:
    def __init__(self, provider: AlphaVantageNewsProvider) -> None:
        self.provider = provider
        self.engine = GlobalNewsIntelligenceEngine(provider)
        self.cache: dict[tuple[str, str], tuple[tuple[NewsItem, ...], dict[str, object]]] = {}
        self.rate_limit_triggered = False
        self.provider_request_count = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.requests_suppressed_after_rate_limit = 0
        self.last_status = "PROVIDER_UNAVAILABLE"
        self.provider_diagnostics: dict[str, object] = {}

    def diagnostics(self) -> dict[str, object]:
        return {
            "news_provider": self.provider.provider_name,
            "news_provider_status": self.last_status,
            "news_rate_limit_triggered": self.rate_limit_triggered,
            "news_provider_request_count": self.provider_request_count,
            "news_cache_hits": self.cache_hits,
            "news_cache_misses": self.cache_misses,
            "news_requests_suppressed_after_rate_limit": (
                self.requests_suppressed_after_rate_limit
            ),
            "news_provider_diagnostics": self.provider_diagnostics,
        }


class _ConfiguredDemoNewsProvider:
    """Adapt the configured read-only news source to the preflight contract."""

    def __init__(
        self,
        provider: GlobalNewsProvider,
        instrument: UniversalInstrument,
        *,
        session: _RunNewsSession | None = None,
    ) -> None:
        self._session = session
        self._engine = (
            session.engine if session is not None else GlobalNewsIntelligenceEngine(provider)
        )
        self._provider = provider
        self._instrument = instrument
        self._diagnostics: dict[str, object] = {}

    @property
    def diagnostics(self) -> dict[str, object]:
        return dict(self._diagnostics)

    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]:
        if self._instrument.symbol.casefold() not in {symbol.casefold() for symbol in symbols}:
            return ()
        if self._session is not None:
            key = (self._instrument.symbol.casefold(), as_of.isoformat())
            cached = self._session.cache.get(key)
            if cached is not None:
                self._session.cache_hits += 1
                self._diagnostics = dict(cached[1])
                self._diagnostics["news_cache_hit"] = True
                return cached[0]
            self._session.cache_misses += 1
            if self._session.rate_limit_triggered:
                self._session.requests_suppressed_after_rate_limit += 1
                self._session.last_status = "RATE_LIMITED/SUPPRESSED"
                self._diagnostics = {
                    "news_provider": self._provider.provider_name,
                    "news_provider_status": "RATE_LIMITED/SUPPRESSED",
                    "news_raw_event_count": 0,
                    "news_normalized_event_count": 0,
                    "news_relevant_event_count": 0,
                    "context_news_count": 0,
                    "news_request_suppressed_after_rate_limit": True,
                }
                return ()
            if hasattr(self._provider, "set_tickers"):
                self._provider.set_tickers((self._provider_symbol(),))
            self._session.provider_request_count += 1
        result = self._engine.analyze(instruments=(self._instrument,), as_of=as_of)
        relevant_events = tuple(
            event
            for event in result.normalized_events
            if any(
                link.symbol.casefold() == self._instrument.symbol.casefold()
                for link in event.companies_assets_affected
            )
        )
        provider_status = result.provider_status.value
        if self._session is not None:
            self._session.provider_diagnostics = dict(result.provider_diagnostics)
        if self._session is not None:
            self._session.last_status = provider_status
            if provider_status == NewsProviderStatus.RATE_LIMITED.value:
                self._session.rate_limit_triggered = True
        if provider_status == NewsProviderStatus.AVAILABLE.value and result.raw_event_count == 0:
            provider_status = "NO_EVENTS"
        if result.provider_status not in {
            NewsProviderStatus.AVAILABLE,
            NewsProviderStatus.PARTIAL,
        }:
            self._diagnostics = {
                "news_provider": result.provider_name,
                "news_provider_status": provider_status,
                "news_raw_event_count": result.raw_event_count,
                "news_normalized_event_count": len(result.normalized_events),
                "news_relevant_event_count": len(relevant_events),
                "context_news_count": 0,
            }
            if result.provider_diagnostics:
                self._diagnostics["news_provider_diagnostics"] = result.provider_diagnostics
            result_items: tuple[NewsItem, ...] = ()
            if self._session is not None:
                self._session.cache[key] = (result_items, dict(self._diagnostics))
            return result_items
        items: list[NewsItem] = []
        for event in result.normalized_events:
            relevance = tuple(
                link.symbol
                for link in event.companies_assets_affected
                if link.symbol.casefold() == self._instrument.symbol.casefold()
            )
            if not relevance:
                continue
            sentiment = {
                "POSITIVE": Decimal("0.8"),
                "NEGATIVE": Decimal("-0.8"),
                "MIXED": Decimal("0"),
                "NEUTRAL": Decimal("0"),
            }[event.sentiment.value]
            items.append(
                NewsItem(
                    news_id=event.event_id,
                    source=event.source,
                    timestamp=event.published_at,
                    headline=event.headline,
                    summary=event.explanation,
                    asset_relevance=relevance,
                    url=event.source_url_reference,
                    sentiment=sentiment,
                    importance=event.impact_score,
                    confidence=event.confidence,
                )
            )
        self._diagnostics = {
            "news_provider": result.provider_name,
            "news_provider_status": provider_status,
            "news_raw_event_count": result.raw_event_count,
            "news_normalized_event_count": len(result.normalized_events),
            "news_relevant_event_count": len(relevant_events),
            "context_news_count": len(items),
        }
        if result.provider_diagnostics:
            self._diagnostics["news_provider_diagnostics"] = result.provider_diagnostics
        result_items = tuple(items)
        self._diagnostics["news_request_suppressed_after_rate_limit"] = False
        if self._session is not None:
            self._session.cache[key] = (result_items, dict(self._diagnostics))
        return result_items

    def _provider_symbol(self) -> str:
        return (
            f"CRYPTO:{self._instrument.symbol}"
            if self._instrument.asset_class is AssetClass.CRYPTO
            else self._instrument.symbol
        )


def _demo_news_provider(
    config: ApplicationConfig,
    values: Mapping[str, str] | None,
    instrument: UniversalInstrument,
    *,
    session: _RunNewsSession | None = None,
) -> NewsProvider | None:
    if config.providers.news is not ProviderMode.ALPHA_VANTAGE:
        return None
    provider_symbol = (
        f"CRYPTO:{instrument.symbol}"
        if instrument.asset_class is AssetClass.CRYPTO
        else instrument.symbol
    )
    provider = (
        session.provider
        if session is not None
        else _configured_news_provider(config, values or {}, tickers=(provider_symbol,))
    )
    return _ConfiguredDemoNewsProvider(provider, instrument, session=session)


def _run_news_session(
    config: ApplicationConfig, values: Mapping[str, str] | None
) -> _RunNewsSession | None:
    if config.providers.news is not ProviderMode.ALPHA_VANTAGE:
        return None
    return _RunNewsSession(
        _configured_news_provider(config, values or {})
    )


def _configured_news_provider(
    config: ApplicationConfig,
    values: Mapping[str, str],
    *,
    tickers: tuple[str, ...] = (),
) -> AlphaVantageNewsProvider | CrossCheckedNewsProvider:
    primary = AlphaVantageNewsProvider(
        api_key=values.get("ALPHA_VANTAGE_API_KEY"),
        tickers=tickers,
    )
    if values.get("AEGIS_NEWS_SECONDARY_PROVIDER", "").strip().lower() != "alpaca":
        return primary
    secondary = AlpacaNewsProvider(
        api_key_id=_first_nonempty(values, "ALPACA_API_KEY_ID", "APCA_API_KEY_ID"),
        api_secret_key=_first_nonempty(values, "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY"),
        symbols=tickers,
    )
    return CrossCheckedNewsProvider(primary, secondary)


def _first_nonempty(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    return None


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
    preflight_config: ApplicationConfig | None = None,
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
        preflight_config or config,
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
                capital_envelope=_capital_envelope(config, registry),
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

                    from app.brokers.etoro.tradability_revalidation import fresh_demo_tradability

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
                        tradability_revalidator=lambda instrument_id, symbol, as_of: (
                            fresh_demo_tradability(read_client, instrument_id, symbol, as_of)
                        ),
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


def run_user_confirmed_demo_validation(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None,
    confirm_demo_write: bool,
    store: SqliteRecordStore,
    clock: Callable[[], datetime] | None = None,
    client: EtoroReadClient | None = None,
    agent: AegisAgent | None = None,
    news_provider: NewsProvider | None = None,
    http: DisciplinedHttpClient | None = None,
) -> dict[str, object]:
    """Run the repository-defined one-shot Demo transport validation."""
    if not confirm_demo_write:
        return {
            "status": "CONFIRM_DEMO_WRITE_REQUIRED",
            "demo_submission_attempts": 0,
            "demo_write_performed": False,
            "real_write_performed": False,
        }
    blockers = _demo_validation_config_blockers(config, store)
    config_checks = _demo_validation_config_checks(config, store)
    if blockers:
        return {
            "status": "BLOCKED",
            "blockers": blockers,
            "preflight_checks": config_checks,
            "failed_check": blockers[0],
            "failure_code": blockers[0],
            "failure_reason": blockers[0],
            "demo_submission_attempts": 0,
            "demo_write_performed": False,
            "real_write_performed": False,
        }

    now = (clock or (lambda: datetime.now(UTC)))()
    safe_preflight_config = config.model_copy(
        update={
            "broker_execution_mode": BrokerExecutionMode.READ_ONLY,
            "etoro_demo_execution_enabled": False,
            "etoro_demo_automatic_pilot_enabled": False,
            "demo_smoke_test_opt_in": False,
        }
    )
    stage_a = build_first_demo_preflight_report(
        safe_preflight_config,
        values=values,
        client=client,
        agent=agent,
        news_provider=news_provider,
        store=store,
        clock=lambda: now,
    )
    if not stage_a.ready_for_human_confirmation:
        diagnostics = _stage_a_diagnostics(stage_a)
        stage_checks = tuple(check.model_dump(mode="json") for check in stage_a.checks)
        return {
            "status": "PREFLIGHT_BLOCKED",
            "preflight": stage_a.pre_flight,
            "preflight_checks": (*config_checks, *stage_checks),
            "failed_check": diagnostics["failed_check"],
            "failure_code": diagnostics["failure_code"],
            "failure_reason": diagnostics["failure_reason"],
            "aegis_agent_rationale": diagnostics["aegis_agent_rationale"],
            "news_diagnostics": diagnostics["news_diagnostics"],
            "demo_submission_attempts": 0,
            "demo_write_performed": False,
            "real_write_performed": False,
        }
    if None in {
        stage_a.proposal_id,
        stage_a.proposal_digest,
        stage_a.risk_policy_digest,
        stage_a.instrument_id,
        stage_a.instrument,
        stage_a.demo_amount,
    }:
        raise DemoExecutionArmingError("complete Stage A material is required")
    assert stage_a.instrument_id is not None
    assert stage_a.demo_amount is not None
    confirmation = DemoExecutionConfirmation(
        proposal_id=str(stage_a.proposal_id),
        proposal_digest=str(stage_a.proposal_digest),
        risk_policy_digest=str(stage_a.risk_policy_digest),
        instrument_id=stage_a.instrument_id,
        symbol=str(stage_a.instrument),
        amount=stage_a.demo_amount,
        confirmed_at=now,
    )
    result = arm_and_submit_confirmed_demo_once(
        config,
        confirmation,
        values=values,
        client=client,
        agent=agent,
        news_provider=news_provider,
        store=store,
        http=http,
        clock=lambda: now,
        preflight_config=safe_preflight_config,
    )
    return {
        "status": result.final_state,
        "instrument": confirmation.symbol,
        "instrument_id": confirmation.instrument_id,
        "requested_amount_eur": str(confirmation.amount),
        "broker_order_id": result.broker_order_id,
        "broker_state": result.broker_state,
        "demo_submission_attempts": result.demo_submission_attempts,
        "demo_write_performed": result.demo_write_performed,
        "real_write_performed": result.real_write_performed,
        "preflight_checks": (
            *config_checks,
            *(check.model_dump(mode="json") for check in stage_a.checks),
        ),
        "failed_check": None,
        "failure_code": None,
        "failure_reason": None,
        "news_diagnostics": stage_a.news_diagnostics,
    }


def run_operational_demo_once(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None,
    confirm_demo_write: bool,
    store: SqliteRecordStore,
    diagnose_only: bool = False,
) -> dict[str, object]:
    """Explicit operational one-shot entry point; reuse the guarded Demo flow."""
    if not diagnose_only and (not confirm_demo_write or not config.demo_smoke_test_opt_in):
        return {
            "status": "EXPLICIT_DEMO_OPT_IN_REQUIRED",
            "demo_submission_attempts": 0,
            "demo_write_performed": False,
            "real_write_performed": False,
        }
    now = datetime.now(UTC)
    registry = SqliteRecordStore(Path("work") / "etoro-demo-runtime.sqlite3")
    credentials = runtime_credentials(values)
    if credentials is None or not config.etoro_api_enabled:
        return {
            "status": "ETORO_DEMO_READ_ACCESS_NOT_CONFIGURED",
            "demo_submission_attempts": 0,
            "demo_write_performed": False,
            "real_write_performed": False,
        }
    from app.brokers.etoro.live_candidates import current_catalog_candidates

    read_client = EtoroReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
    duplicate_diagnostics: list[dict[str, object]] = []
    selection_diagnostics: dict[str, object] = {}
    live_get_cap = int((values or {}).get("AEGIS_LIVE_SELECTION_GET_CAP", "50"))
    max_selection_batches = int(
        (values or {}).get("AEGIS_DEMO_ONE_SHOT_MAX_SELECTION_BATCHES", "0")
    )
    pacing_delay_seconds = float((values or {}).get("AEGIS_LIVE_SELECTION_PACING_SECONDS", "1"))
    selection_diagnostics["batches"] = []
    catalog_offset = 0
    catalog_count = None
    enriched_count = 0
    settings = runtime_settings(values)
    news_session = _run_news_session(config, values)
    maximum_quote_age = settings.maximum_quote_age_seconds
    maximum_future_skew = settings.maximum_future_quote_skew_seconds
    selected_instrument = None
    stale_quotes = 0
    quote_diagnostics: list[dict[str, object]] = []
    rejected_candidates: list[dict[str, object]] = []
    candidates_checked = 0
    while catalog_count is None or catalog_offset < catalog_count:
        batch_diagnostics: dict[str, object] = {}
        try:
            candidate_instruments = current_catalog_candidates(
                read_client,
                duplicate_diagnostics=duplicate_diagnostics,
                selection_diagnostics=batch_diagnostics,
                catalog_offset=catalog_offset,
                live_get_cap=live_get_cap,
                pacing_delay_seconds=pacing_delay_seconds,
            )
        except (EtoroApiError, RuntimeError, ValueError) as exc:
            selection_diagnostics.update(batch_diagnostics)
            return {
                "status": "LIVE_CATALOG_SELECTION_BLOCKED",
                "live_selection": selection_diagnostics,
                "live_search_duplicates": duplicate_diagnostics,
                "failure_type": type(exc).__name__,
                "failure_reason": str(exc),
                "demo_submission_attempts": 0,
                "demo_write_performed": False,
                "real_write_performed": False,
            }
        batches = selection_diagnostics.get("batches")
        if not isinstance(batches, list):
            raise DemoExecutionArmingError("selection batch diagnostics are unavailable")
        batches.append(dict(batch_diagnostics))
        local_prefilter_count = batch_diagnostics.get("local_prefilter_count")
        batch_catalog_count = batch_diagnostics.get("batch_catalog_count")
        if not isinstance(local_prefilter_count, int) or not isinstance(batch_catalog_count, int):
            raise DemoExecutionArmingError("selection batch progress is unavailable")
        catalog_count = local_prefilter_count
        batch_count = batch_catalog_count
        catalog_offset += batch_count
        enriched_count += len(candidate_instruments)
        for instrument in candidate_instruments:
            candidates_checked += 1
            try:
                quote, raw_dates, server_now = read_client.quote_with_diagnostics(
                    int(instrument.broker_instrument_id), instrument.symbol
                )
                freshness_now = server_now or now
                age = (freshness_now - quote.as_of).total_seconds()
                clock_skew = max(0.0, -age)
                within_future_tolerance = clock_skew <= maximum_future_skew
                fresh = age >= 0 or within_future_tolerance
                fresh = fresh and age <= maximum_quote_age
                if age < 0 and not within_future_tolerance:
                    freshness_result = "CLOCK_SKEW"
                elif age < 0:
                    freshness_result = "FRESH_CLOCK_SKEW"
                elif age > maximum_quote_age:
                    freshness_result = "STALE"
                else:
                    freshness_result = "FRESH"
                quote_diagnostics.append(
                    {
                        "instrument_id": instrument.broker_instrument_id,
                        "symbol": instrument.symbol,
                        "raw_rate_dates": raw_dates,
                        "parsed_quote_utc": quote.as_of.isoformat(),
                        "now_utc": freshness_now.isoformat(),
                        "quote_age_seconds": age,
                        "clock_skew_seconds": clock_skew,
                        "freshness_threshold_seconds": maximum_quote_age,
                        "future_skew_tolerance_seconds": maximum_future_skew,
                        "freshness_result": freshness_result,
                    }
                )
                if not fresh:
                    stale_quotes += 1
                    rejected_candidates.append(
                        {
                            "symbol": instrument.symbol,
                            "instrument_id": instrument.broker_instrument_id,
                            "rejection_gate": "market_rates",
                            "rejection_reason": freshness_result,
                        }
                    )
                    continue
            except (EtoroApiError, RuntimeError, ValueError):
                stale_quotes += 1
                rejected_candidates.append(
                    {
                        "symbol": instrument.symbol,
                        "instrument_id": instrument.broker_instrument_id,
                        "rejection_gate": "market_rates",
                        "rejection_reason": "quote could not be normalized",
                    }
                )
                continue

            selected_instrument = instrument
            if diagnose_only:
                continue
            selected_values = dict(values or {})
            selected_values["AEGIS_ETORO_READINESS_SYMBOL"] = instrument.symbol
            selected_values["AEGIS_ETORO_READINESS_INSTRUMENT_ID"] = instrument.broker_instrument_id
            news_provider = _demo_news_provider(
                config, selected_values, instrument, session=news_session
            )
            result = run_user_confirmed_demo_validation(
                config,
                values=selected_values,
                confirm_demo_write=True,
                store=registry,
                news_provider=news_provider,
            )
            submission_attempts = result.get("demo_submission_attempts", 0)
            if isinstance(submission_attempts, int) and submission_attempts > 0:
                return {
                    **result,
                    "live_selection": selection_diagnostics,
                    "live_search_duplicates": duplicate_diagnostics,
                    "live_session_refresh_count": enriched_count,
                    "open_tradable_checked": enriched_count,
                    "stale_quotes_skipped": stale_quotes,
                    "fresh_quote_found": True,
                    "candidates_checked": candidates_checked,
                    "rejected_candidates": rejected_candidates[:10],
                    **({} if news_session is None else news_session.diagnostics()),
                }
            rejected_candidates.append(
                {
                    "symbol": instrument.symbol,
                    "instrument_id": instrument.broker_instrument_id,
                    "rejection_gate": result.get("failed_check") or "preflight",
                    "rejection_reason": result.get("failure_reason")
                    or result.get("status", "preflight rejected"),
                    "aegis_agent_rationale": result.get("aegis_agent_rationale"),
                    "news_diagnostics": result.get("news_diagnostics", {}),
                }
            )
        if max_selection_batches > 0 and len(batches) >= max_selection_batches:
            break
    if selected_instrument is None:
        return {
            "status": "NO_CANDIDATE_PASSED_PREFLIGHT"
            if candidates_checked
            else "NO_CURRENT_OPEN_TRADABLE_INSTRUMENT",
            "live_selection": selection_diagnostics,
            "live_search_duplicates": duplicate_diagnostics,
            "live_session_refresh_count": enriched_count,
            "open_tradable_checked": enriched_count,
            "stale_quotes_skipped": stale_quotes,
            "quote_diagnostics": quote_diagnostics[:5],
            "demo_submission_attempts": 0,
            "demo_write_performed": False,
            "real_write_performed": False,
        }
    if diagnose_only:
        return {
            "status": "QUOTE_DIAGNOSTICS_COMPLETE",
            "live_selection": selection_diagnostics,
            "live_search_duplicates": duplicate_diagnostics,
            "live_session_refresh_count": enriched_count,
            "open_tradable_checked": enriched_count,
            "fresh_quote_found": True,
            "quote_diagnostics": quote_diagnostics[:5],
            "rejected_candidates": rejected_candidates[:10],
            **({} if news_session is None else news_session.diagnostics()),
            "demo_submission_attempts": 0,
            "demo_write_performed": False,
            "real_write_performed": False,
        }
    return {
        "status": "NO_CANDIDATE_PASSED_PREFLIGHT",
        "live_selection": selection_diagnostics,
        "live_search_duplicates": duplicate_diagnostics,
        "live_session_refresh_count": enriched_count,
        "open_tradable_checked": enriched_count,
        "stale_quotes_skipped": stale_quotes,
        "fresh_quote_found": True,
        "candidates_checked": candidates_checked,
        "rejected_candidates": rejected_candidates[:10],
        **({} if news_session is None else news_session.diagnostics()),
        "demo_submission_attempts": 0,
        "demo_write_performed": False,
        "real_write_performed": False,
    }


def verify_demo_validation_read_only(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None,
    store: SqliteRecordStore,
    client: EtoroReadClient | None = None,
) -> dict[str, object]:
    """Read Demo portfolio and the latest persisted validation; never submit."""
    credentials = runtime_credentials(values)
    if credentials is None:
        return {"status": "BLOCKED", "reason": "ETORO_CREDENTIALS_MISSING"}
    if config.operating_mode is not OperatingMode.ETORO_DEMO:
        return {"status": "BLOCKED", "reason": "ETORO_DEMO_REQUIRED"}
    read_client = client or EtoroReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
    identity = read_client.identity()
    portfolio = read_client.demo_account(identity)
    latest = store.latest_demo_submission()
    broker_order_state: str | None = None
    broker_order_error: dict[str, str] | None = None
    payload = None if latest is None else latest.get("payload")
    if isinstance(payload, dict):
        order_id = payload.get("broker_order_id")
        instrument_id = payload.get("instrument_id")
        if order_id is not None and instrument_id is not None:
            try:
                broker_order_state = read_client.demo_order_state(
                    identity, int(str(instrument_id)), str(order_id)
                ).value
            except EtoroApiError as exc:
                broker_order_error = dict(exc.safe_metadata())
    managed = store.managed_demo_exposure_eur()
    authorized = config.authorized_capital_eur
    remaining = (
        None if authorized is None or managed is None else max(Decimal("0"), authorized - managed)
    )
    return {
        "status": "READ_ONLY_VERIFIED",
        "demo_portfolio": portfolio.model_dump(mode="json"),
        "open_positions_count": len(portfolio.positions),
        "latest_aegis_demo_submission": latest,
        "broker_order_state": broker_order_state,
        "broker_order_read_error": broker_order_error,
        "demo_write_count": store.demo_write_attempt_count(),
        "real_write_count": 0,
        "authorized_capital_eur": None if authorized is None else str(authorized),
        "managed_exposure_eur": None if managed is None else str(managed),
        "remaining_authorized_capital_eur": None if remaining is None else str(remaining),
    }


def _demo_validation_config_blockers(
    config: ApplicationConfig, registry: SqliteRecordStore
) -> tuple[str, ...]:
    blockers: list[str] = []
    if config.operating_mode is not OperatingMode.ETORO_DEMO:
        blockers.append("ETORO_DEMO_REQUIRED")
    if config.broker_execution_mode is not BrokerExecutionMode.DEMO_EXECUTION:
        blockers.append("DEMO_EXECUTION_MODE_REQUIRED")
    if not config.etoro_api_enabled or not config.etoro_demo_execution_enabled:
        blockers.append("DEMO_API_EXECUTION_NOT_ENABLED")
    if config.production_trading_enabled:
        blockers.append("REAL_EXECUTION_MUST_REMAIN_DISABLED")
    if config.authorized_capital_eur != Decimal("2000"):
        blockers.append("AUTHORIZED_CAPITAL_MUST_EQUAL_EUR_2000")
    managed = registry.managed_demo_exposure_eur()
    if managed is None:
        blockers.append("MANAGED_EXPOSURE_NOT_AUTHORITATIVE")
    elif config.authorized_capital_eur is not None and managed >= config.authorized_capital_eur:
        blockers.append("AUTHORIZED_CAPITAL_EXHAUSTED")
    return tuple(blockers)


def _demo_validation_config_checks(
    config: ApplicationConfig, registry: SqliteRecordStore
) -> tuple[dict[str, object], ...]:
    managed = registry.managed_demo_exposure_eur()
    authorized = config.authorized_capital_eur
    remaining = None if managed is None or authorized is None else authorized - managed
    return (
        {
            "name": "operating_mode",
            "actual": config.operating_mode.value,
            "expected": OperatingMode.ETORO_DEMO.value,
            "status": "PASS" if config.operating_mode is OperatingMode.ETORO_DEMO else "FAIL",
        },
        {
            "name": "broker_execution_mode",
            "actual": config.broker_execution_mode.value,
            "expected": BrokerExecutionMode.DEMO_EXECUTION.value,
            "status": "PASS"
            if config.broker_execution_mode is BrokerExecutionMode.DEMO_EXECUTION
            else "FAIL",
        },
        {
            "name": "demo_execution_enabled",
            "actual": config.etoro_demo_execution_enabled,
            "expected": True,
            "status": "PASS" if config.etoro_demo_execution_enabled else "FAIL",
        },
        {
            "name": "authorized_capital_eur",
            "actual": None if authorized is None else str(authorized),
            "expected": "2000",
            "status": "PASS" if authorized == Decimal("2000") else "FAIL",
        },
        {
            "name": "managed_exposure_eur",
            "actual": None if managed is None else str(managed),
            "expected": "authoritative and <= authorized capital",
            "status": "PASS"
            if managed is not None and authorized is not None and managed <= authorized
            else "FAIL",
        },
        {
            "name": "remaining_authorized_capital_eur",
            "actual": None if remaining is None else str(remaining),
            "expected": "> 0",
            "status": "PASS" if remaining is not None and remaining > 0 else "FAIL",
        },
        {
            "name": "real_execution",
            "actual": False,
            "expected": False,
            "status": "PASS",
        },
    )


def _stage_a_diagnostics(stage_a: FirstDemoPreflightReport) -> dict[str, object]:
    failing = next(
        (
            check
            for check in stage_a.checks
            if check.status in {"FAIL", "BLOCKED", "NOT_CONFIGURED", "MARKET_CLOSED"}
        ),
        None,
    )
    return {
        "preflight_checks": tuple(check.model_dump(mode="json") for check in stage_a.checks),
        "aegis_agent_rationale": stage_a.aegis_agent_rationale,
        "news_diagnostics": stage_a.news_diagnostics,
        "failed_check": None if failing is None else failing.name,
        "failure_code": stage_a.blocker_code
        if failing is None or failing.status != "MARKET_CLOSED"
        else "MARKET_CLOSED",
        "failure_reason": None if failing is None else failing.reason,
    }


def _capital_envelope(
    config: ApplicationConfig, registry: SqliteRecordStore
) -> AuthorizedCapitalEnvelope | None:
    authorized = config.authorized_capital_eur
    if authorized is None:
        return None
    managed_open = registry.managed_demo_open_exposure_eur()
    reserved = registry.managed_demo_reserved_capital_eur()
    if managed_open is None or reserved is None:
        raise DemoExecutionArmingError("managed Demo exposure is not authoritative")
    return AuthorizedCapitalEnvelope(
        authorized_capital_eur=authorized,
        managed_exposure_eur=managed_open,
        reserved_capital_eur=reserved,
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
