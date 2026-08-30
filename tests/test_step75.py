"""Step 7.5 first controlled Demo execution pre-flight tests."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from app.agent.context import AegisAgentContext
from app.agent.models import AegisAgentResult, AegisAnalysis
from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.demo import (
    DEMO_ORDER_URL,
    DemoExecutionError,
    EtoroDemoAdapter,
    assert_demo_route,
)
from app.brokers.etoro.demo_execution import (
    DemoExecutionConfirmation,
    OneShotDemoExecutionWindow,
    arm_and_submit_confirmed_demo_once,
)
from app.brokers.etoro.demo_preflight import (
    CHECK_MARKET_CLOSED,
    PREFLIGHT_FAIL,
    PREFLIGHT_PASS,
    FirstDemoPreflightReport,
    build_first_demo_preflight_report,
)
from app.brokers.etoro.http import (
    DisciplinedHttpClient,
    EtoroHttpFailureKind,
    HttpResponse,
    TransportError,
)
from app.brokers.models import (
    AccountKind,
    BrokerAccountContext,
    BrokerIdentity,
    BrokerSubmission,
    DemoEligibility,
    DemoPortfolioPosition,
    DemoPortfolioSnapshot,
    ExecutionState,
    InstrumentResolution,
    PreflightDecision,
    TrackRecordKind,
)
from app.brokers.preflight import evaluate_demo_preflight
from app.brokers.reconciliation import ReconciliationError, reconcile_demo_state
from app.config import ConfigLoadError, load_config
from app.config.models import ApplicationConfig, RiskPolicyConfig
from app.domain.enums import (
    Currency,
    ExecutionPolicy,
    HoldingPeriod,
    MarketStatus,
    OperatingMode,
    RecommendedAction,
    RiskDecisionStatus,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem, InstrumentMetadata, MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.execution.gate import (
    AuthorizationAlreadyConsumedError,
    AuthorizedTrade,
    RiskEnforcedExecutionGate,
)
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskAuthorizationError, RiskManager
from app.storage.sqlite import SecretPersistenceError, SqliteRecordStore
from tests.conftest import TEST_INSTRUMENT_ID


class FakeReadClient:
    def __init__(
        self,
        *,
        identity: BrokerIdentity | None = None,
        demo: DemoPortfolioSnapshot | None = None,
        real: PortfolioSnapshot | None = None,
        resolution: InstrumentResolution | None = None,
        quote: MarketQuote | None = None,
        eligibility: DemoEligibility | BaseException | None = None,
        order_state: ExecutionState | BaseException = ExecutionState.FILLED,
    ) -> None:
        self._identity = identity or _identity()
        self._demo = demo or _demo_snapshot()
        self._real = real or _portfolio()
        self._resolution = resolution or _resolution()
        self._quote = quote or _quote()
        self._eligibility = eligibility or _eligibility()
        self._order_state = order_state
        self.calls: list[str] = []

    def identity(self) -> BrokerIdentity:
        self.calls.append("identity")
        return self._identity

    def demo_account(self, identity: BrokerIdentity) -> DemoPortfolioSnapshot:
        self.calls.append("demo_account")
        return self._demo

    def real_portfolio_read_only(
        self, symbols: dict[int, str], *, currency: Currency = Currency.USD
    ) -> PortfolioSnapshot:
        self.calls.append("real_portfolio_read_only")
        return self._real

    def resolve_instrument(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> InstrumentResolution:
        self.calls.append("resolve_instrument")
        return self._resolution

    def quote(self, instrument_id: int, symbol: str) -> MarketQuote:
        self.calls.append("quote")
        return self._quote

    def demo_eligibility(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> DemoEligibility:
        self.calls.append("demo_eligibility")
        if isinstance(self._eligibility, BaseException):
            raise self._eligibility
        return self._eligibility

    def demo_order_state(
        self, identity: BrokerIdentity, instrument_id: int, order_id: str
    ) -> ExecutionState:
        self.calls.append("demo_order_state")
        if isinstance(self._order_state, BaseException):
            raise self._order_state
        return self._order_state


class FailingReadClient(FakeReadClient):
    def __init__(self, failures: Mapping[str, BaseException]) -> None:
        super().__init__()
        self._failures = failures

    def _raise_if_configured(self, method: str) -> None:
        if method in self._failures:
            raise self._failures[method]

    def identity(self) -> BrokerIdentity:
        self.calls.append("identity")
        self._raise_if_configured("identity")
        return self._identity

    def demo_account(self, identity: BrokerIdentity) -> DemoPortfolioSnapshot:
        self.calls.append("demo_account")
        self._raise_if_configured("demo_account")
        return self._demo

    def real_portfolio_read_only(
        self, symbols: dict[int, str], *, currency: Currency = Currency.USD
    ) -> PortfolioSnapshot:
        self.calls.append("real_portfolio_read_only")
        self._raise_if_configured("real_portfolio_read_only")
        return self._real

    def resolve_instrument(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> InstrumentResolution:
        self.calls.append("resolve_instrument")
        self._raise_if_configured("resolve_instrument")
        return self._resolution

    def quote(self, instrument_id: int, symbol: str) -> MarketQuote:
        self.calls.append("quote")
        self._raise_if_configured("quote")
        return self._quote

    def demo_eligibility(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> DemoEligibility:
        self.calls.append("demo_eligibility")
        self._raise_if_configured("demo_eligibility")
        if isinstance(self._eligibility, BaseException):
            raise self._eligibility
        return self._eligibility


class PositiveNewsProvider:
    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]:
        return (_news(as_of),)


class HoldAgent:
    def analyze(self, context: AegisAgentContext) -> AegisAgentResult:
        return AegisAgentResult(
            analysis=AegisAnalysis(
                timestamp=context.analysis_timestamp,
                market_assessment="hold",
                portfolio_assessment="hold",
                opportunity_summary="no proposal",
                risk_summary="no proposal",
                confidence=Decimal("0"),
                supporting_factors=(),
                risk_factors=("test hold",),
                recommended_action=RecommendedAction.HOLD,
                rationale="test hold",
            )
        )


class FixedProposalAgent:
    def __init__(self, proposal: TradeProposal) -> None:
        self._proposal = proposal

    def analyze(self, context: AegisAgentContext) -> AegisAgentResult:
        return AegisAgentResult(
            analysis=AegisAnalysis(
                timestamp=context.analysis_timestamp,
                market_assessment="test",
                portfolio_assessment="test",
                opportunity_summary="test",
                risk_summary="test",
                confidence=self._proposal.confidence,
                supporting_factors=("test evidence",),
                risk_factors=("market risk",),
                recommended_action=RecommendedAction.OPEN,
                rationale="test proposal",
                symbol=self._proposal.symbol,
            ),
            proposal=self._proposal,
        )


class SequencedTransport:
    def __init__(self, responses: list[HttpResponse | BaseException]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ActivatingStore(SqliteRecordStore):
    def __init__(self, path: Path, switch: KillSwitch) -> None:
        super().__init__(path)
        self._switch = switch

    def reserve_demo_submission(self, idempotency_key: str, payload: Mapping[str, object]) -> bool:
        reserved = super().reserve_demo_submission(idempotency_key, payload)
        self._switch.activate("activated immediately before HTTP")
        return reserved


def _now() -> datetime:
    return datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _identity() -> BrokerIdentity:
    return BrokerIdentity(
        stable_user_id="stable-user-0001",
        demo_account_id=222,
        real_account_id=111,
        username="aegis-user",
    )


def _values() -> dict[str, str]:
    return {
        "ETORO_API_KEY": "api-secret",
        "ETORO_USER_KEY": "user-secret",
        "ETORO_EXPECTED_USERNAME": "aegis-user",
        "ETORO_EXPECTED_GCID": "stable-user-0001",
        "AEGIS_ETORO_READINESS_SYMBOL": "TEST",
        "AEGIS_ETORO_READINESS_INSTRUMENT_ID": str(TEST_INSTRUMENT_ID),
        "AEGIS_ETORO_READINESS_MAX_QUOTE_AGE_SECONDS": "300",
    }


def _config(
    *,
    kill_switch: bool = False,
    risk: RiskPolicyConfig | None = None,
) -> ApplicationConfig:
    return ApplicationConfig(
        etoro_api_enabled=True,
        kill_switch=kill_switch,
        risk=risk or RiskPolicyConfig(),
    )


def _resolution(
    *,
    market_status: MarketStatus = MarketStatus.OPEN,
    supported: bool = True,
    structural_status: str = "SUPPORTED",
) -> InstrumentResolution:
    return InstrumentResolution(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        internal_symbol_full="TEST",
        display_name="Test Instrument",
        instrument_type="Stocks",
        market_status=market_status,
        is_currently_tradable=market_status is MarketStatus.OPEN,
        is_buy_enabled=True,
        is_hidden_from_client=False,
        is_delisted=False,
        is_active_in_platform=True,
        current_rate=Decimal("10"),
        resolved=True,
        structurally_supported=supported,
        structural_status=structural_status,
        verified=supported,
        as_of=_now(),
    )


def _quote(
    *, market_status: MarketStatus = MarketStatus.OPEN, as_of: datetime | None = None
) -> MarketQuote:
    return MarketQuote(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        price=Decimal("10"),
        bid=Decimal("9.99"),
        ask=Decimal("10.01"),
        as_of=as_of or _now(),
        currency=Currency.USD,
        source="test",
        market_status=market_status,
    )


def _demo_snapshot(
    *,
    cash: Decimal = Decimal("1000"),
    total: Decimal = Decimal("1000"),
    target_exposure: Decimal = Decimal("0"),
) -> DemoPortfolioSnapshot:
    positions: tuple[DemoPortfolioPosition, ...] = ()
    if target_exposure > 0:
        positions = (
            DemoPortfolioPosition(
                instrument_id=TEST_INSTRUMENT_ID,
                asset_currency=Currency.USD,
                side=TradeSide.BUY,
                units=target_exposure / Decimal("10"),
                current_exposure=target_exposure,
                initial_exposure=target_exposure,
                unrealized_pnl_account_currency=Decimal("0"),
                unrealized_pnl_asset_currency=Decimal("0"),
                leverage=Decimal("1"),
                average_open_rate=Decimal("10"),
            ),
        )
    return DemoPortfolioSnapshot(
        context=BrokerAccountContext(
            stable_user_id="stable-user-0001",
            account_id=222,
            kind=AccountKind.DEMO,
        ),
        as_of=_now(),
        currency=Currency.USD,
        cash=cash,
        total_value=total,
        account_balance=cash,
        positions=positions,
        position_ids=tuple(str(position.instrument_id) for position in positions),
    )


def _eligibility(
    *,
    minimum: Decimal = Decimal("5"),
    allow_open: bool = True,
    max_units: Decimal | None = None,
    quantity_types: tuple[str, ...] = ("amount",),
    settlement_type: SettlementType = SettlementType.REAL,
    leverage: int = 1,
) -> DemoEligibility:
    return DemoEligibility(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        currency=Currency.USD,
        minimum_position=minimum,
        allow_open=allow_open,
        allow_close=None,
        max_units_per_order=max_units,
        allowed_order_quantity_types=quantity_types,
        settlement_type=settlement_type,
        leverage=leverage,
        verified=True,
    )


def _news(now: datetime) -> NewsItem:
    return NewsItem(
        news_id="news-1",
        source="test",
        timestamp=now,
        headline="Positive test evidence",
        summary="synthetic evidence for deterministic tests",
        asset_relevance=("TEST",),
        sentiment=Decimal("0.8"),
        importance=Decimal("0.8"),
        confidence=Decimal("0.9"),
    )


def _proposal(
    *,
    idempotency_key: str = "fixed-idempotency-key",
    amount: Decimal = Decimal("5"),
    confidence: Decimal = Decimal("0.80"),
    created_at: datetime | None = None,
) -> TradeProposal:
    now = created_at or _now()
    return TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000501"),
        idempotency_key=idempotency_key,
        created_at=now,
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        side=TradeSide.BUY,
        intent=TradeIntent.OPEN,
        amount=amount,
        currency=Currency.USD,
        target_weight=Decimal("0.15"),
        current_weight=Decimal("0"),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="test proposal",
        evidence=(
            EvidenceItem(
                source="test",
                timestamp=now,
                summary="test",
                confidence=Decimal("0.90"),
            ),
        ),
        confidence=confidence,
        risk_factors=("market risk",),
        invalidation_conditions=("test invalidation",),
        expected_holding_period=HoldingPeriod.MONTHS,
    )


def _metadata(now: datetime | None = None) -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("5"),
        metadata_as_of=now or _now(),
        source="test",
    )


def _risk_context(proposal: TradeProposal, *, now: datetime | None = None) -> RiskContext:
    at = now or _now()
    return RiskContext(
        evaluated_at=at,
        portfolio=_portfolio(),
        price=_quote(as_of=at).to_price_snapshot(),
        instrument=_metadata(at),
        market_data_available=True,
        news_data_available=True,
        daily_new_trade_count=0,
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.USD,
        cash=Decimal("1000"),
        positions=(),
        reported_total_value=Decimal("1000"),
    )


def _admitted_trade(
    proposal: TradeProposal, switch: KillSwitch, now: datetime
) -> tuple[RiskEnforcedExecutionGate, AuthorizedTrade]:
    risk_manager = RiskManager(
        RiskPolicyConfig(),
        switch,
        authorization_key=b"step75-risk-authorization-key-32",
        clock=lambda: now,
    )
    evaluation = risk_manager.evaluate(proposal, _risk_context(proposal, now=now))
    assert evaluation.authorization is not None
    gate = RiskEnforcedExecutionGate(risk_manager)
    return gate, gate.admit(proposal, evaluation.authorization, at=now)


def _stage_a_report(tmp_path: Path) -> FirstDemoPreflightReport:
    return build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "stage-a.sqlite3"),
        clock=_now,
    )


def _confirmation_from(report: FirstDemoPreflightReport) -> DemoExecutionConfirmation:
    assert report.proposal_id is not None
    assert report.proposal_digest is not None
    assert report.risk_policy_digest is not None
    assert report.instrument_id is not None
    assert report.instrument is not None
    assert report.demo_amount is not None
    return DemoExecutionConfirmation(
        proposal_id=report.proposal_id,
        proposal_digest=report.proposal_digest,
        risk_policy_digest=report.risk_policy_digest,
        instrument_id=report.instrument_id,
        symbol=report.instrument,
        amount=report.demo_amount,
        confirmed_at=_now(),
    )


def test_preflight_success_waits_for_human_confirmation(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "preflight.sqlite3")
    client = FakeReadClient()
    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=store,
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_PASS
    assert report.pre_flight_stage_a == PREFLIGHT_PASS
    assert report.ready_for_human_confirmation
    assert report.execution_technically_ready is False
    assert report.execution_arming_performed is False
    assert report.final_authorization_minted is False
    assert report.human_confirmation_required
    assert report.demo_write_performed is False
    assert report.real_write_performed is False
    assert report.kill_switch_status == "ACTIVE"
    assert report.demo_amount == Decimal("5.00")
    assert report.risk_manager_result == RiskDecisionStatus.APPROVED.value
    assert report.minimum_broker_supported_amount == Decimal("5")
    assert report.proposal_id is not None
    assert report.proposal_digest is not None
    assert report.risk_policy_digest is not None
    assert store.demo_submission_keys() == frozenset()
    assert client.calls == [
        "identity",
        "demo_account",
        "real_portfolio_read_only",
        "resolve_instrument",
        "quote",
        "demo_eligibility",
    ]


def test_preflight_rejects_when_api_is_not_enabled(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        ApplicationConfig(kill_switch=False),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.execution_technically_ready is False
    assert report.blocker_code == "API_ENABLEMENT"


def test_preflight_rejects_premature_demo_execution_enablement(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        ApplicationConfig(
            operating_mode=OperatingMode.ETORO_DEMO,
            execution_policy=ExecutionPolicy.CONFIRM,
            etoro_api_enabled=True,
            etoro_demo_execution_enabled=True,
            demo_smoke_test_opt_in=True,
            kill_switch=False,
        ),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "DEMO_EXECUTION_DISABLED"
    assert report.demo_write_performed is False


def test_preflight_rejects_missing_credentials(tmp_path: Path) -> None:
    values = _values()
    values["ETORO_USER_KEY"] = ""

    report = build_first_demo_preflight_report(
        _config(),
        values=values,
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "CREDENTIALS"


def test_transport_failure_prevents_stage_a_readiness(tmp_path: Path) -> None:
    client = FailingReadClient(
        {
            "identity": EtoroApiError(
                "blocked before HTTP",
                endpoint="/api/v1/me",
                category=EtoroHttpFailureKind.NETWORK_TRANSPORT_ERROR,
                transport_detail="SOCKET_CONNECT",
            )
        }
    )

    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight_stage_a == PREFLIGHT_FAIL
    assert report.ready_for_human_confirmation is False
    assert report.blocker_code == "NETWORK_TRANSPORT_ERROR"
    assert report.demo_write_performed is False
    assert report.real_write_performed is False
    assert client.calls == ["identity"]


def test_successful_me_read_permits_stage_a_to_continue(tmp_path: Path) -> None:
    client = FakeReadClient()

    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight_stage_a == PREFLIGHT_PASS
    assert report.ready_for_human_confirmation
    assert client.calls[0] == "identity"
    assert "demo_eligibility" in client.calls
    assert "demo_order_state" not in client.calls


def test_stage_a_requires_real_portfolio_read_only(tmp_path: Path) -> None:
    client = FailingReadClient({"real_portfolio_read_only": RuntimeError("read blocked")})

    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight_stage_a == PREFLIGHT_FAIL
    assert report.ready_for_human_confirmation is False
    assert report.blocker_code == "REAL_PORTFOLIO_READ_ONLY"
    assert client.calls == ["identity", "demo_account", "real_portfolio_read_only"]


def test_preflight_rejects_invalid_runtime_settings(tmp_path: Path) -> None:
    values = _values()
    values["AEGIS_ETORO_READINESS_INSTRUMENT_ID"] = "not-an-int"

    report = build_first_demo_preflight_report(
        _config(),
        values=values,
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "RUNTIME_CONFIGURATION"


def test_preflight_blocks_when_expected_identity_is_not_configured(tmp_path: Path) -> None:
    values = _values()
    values["ETORO_EXPECTED_USERNAME"] = ""
    values["ETORO_EXPECTED_GCID"] = ""

    report = build_first_demo_preflight_report(
        _config(),
        values=values,
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "IDENTITY_GUARD"


def test_preflight_rejects_identity_mismatch_and_activates_kill_switch(
    tmp_path: Path,
) -> None:
    values = _values()
    values["ETORO_EXPECTED_USERNAME"] = "different-user"

    report = build_first_demo_preflight_report(
        _config(),
        values=values,
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "IDENTITY_GUARD"
    assert report.kill_switch_status == "ACTIVE"


def test_preflight_blocks_when_no_symbol_is_configured(tmp_path: Path) -> None:
    values = _values()
    values["AEGIS_ETORO_READINESS_SYMBOL"] = ""

    report = build_first_demo_preflight_report(
        _config(),
        values=values,
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"


def test_preflight_classifies_network_error_without_credentials_in_logs(
    tmp_path: Path,
) -> None:
    error = EtoroApiError(
        "network failed",
        endpoint="/api/v1/me",
        category=EtoroHttpFailureKind.NETWORK_TRANSPORT_ERROR,
    )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FailingReadClient({"identity": error})),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "NETWORK_TRANSPORT_ERROR"
    assert "api-secret" not in json.dumps(report.model_dump(mode="json"))


def test_preflight_classifies_cloudflare_1010_separately(tmp_path: Path) -> None:
    error = EtoroApiError(
        "edge block",
        endpoint="/api/v1/me",
        status=403,
        category=EtoroHttpFailureKind.EDGE_WAF_BLOCK,
        response_headers={"CF-RAY": "sanitized-ray"},
    )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FailingReadClient({"identity": error})),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    auth_check = next(check for check in report.checks if check.name == "authentication")
    assert report.pre_flight == PREFLIGHT_FAIL
    assert auth_check.metadata["category"] == "EDGE_WAF_BLOCK"
    assert auth_check.metadata["cf_ray"] == "sanitized-ray"


def test_preflight_rejects_demo_portfolio_api_failure(tmp_path: Path) -> None:
    error = EtoroApiError(
        "portfolio failed",
        endpoint="/api/v1/trading/info/demo/aggregate-portfolio",
        status=403,
        category=EtoroHttpFailureKind.AUTH_API_PERMISSION_ERROR,
    )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FailingReadClient({"demo_account": error})),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "DEMO_PORTFOLIO"


def test_preflight_rejects_instrument_id_mismatch(tmp_path: Path) -> None:
    values = _values()
    values["AEGIS_ETORO_READINESS_INSTRUMENT_ID"] = str(TEST_INSTRUMENT_ID + 1)

    report = build_first_demo_preflight_report(
        _config(),
        values=values,
        client=cast(EtoroReadClient, FakeReadClient()),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"


def test_preflight_rejects_stale_market_rate(tmp_path: Path) -> None:
    client = FakeReadClient(quote=_quote(as_of=_now() - timedelta(minutes=20)))

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"


def test_preflight_stops_on_market_closed_before_eligibility(tmp_path: Path) -> None:
    client = FakeReadClient(
        resolution=_resolution(market_status=MarketStatus.CLOSED),
        quote=_quote(market_status=MarketStatus.CLOSED),
    )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    market = next(check for check in report.checks if check.name == "market_state")
    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"
    assert market.status == CHECK_MARKET_CLOSED
    assert "demo_eligibility" not in client.calls


def test_preflight_stops_on_ineligible_instrument(tmp_path: Path) -> None:
    client = FakeReadClient(
        resolution=_resolution(supported=False, structural_status="HIDDEN_FROM_CLIENT")
    )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, client),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"
    assert "quote" not in client.calls


def test_preflight_rejects_unknown_minimum_exposure(tmp_path: Path) -> None:
    client = FakeReadClient(eligibility=ValueError("complete Demo eligibility is unavailable"))

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.eligibility_result == "NOT_EVALUATED"
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"


def test_preflight_rejects_ineligible_demo_instrument(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient(eligibility=_eligibility(allow_open=False))),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.eligibility_result == "REJECTED"
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"


@pytest.mark.parametrize(
    "eligibility",
    [
        _eligibility(settlement_type=SettlementType.CFD),
        _eligibility(leverage=2),
        _eligibility(quantity_types=("units",)),
    ],
)
def test_preflight_rejects_broker_eligibility_constraints(
    tmp_path: Path, eligibility: DemoEligibility
) -> None:
    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient(eligibility=eligibility)),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.eligibility_result == "REJECTED"
    assert report.blocker_code == "MARKET_OR_ELIGIBILITY_BLOCK"


def test_preflight_rejects_proposal_below_broker_minimum(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(
            EtoroReadClient,
            FakeReadClient(eligibility=_eligibility(minimum=Decimal("5"))),
        ),
        agent=FixedProposalAgent(_proposal(amount=Decimal("1"))),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "STAGE_A_FEASIBILITY"


def test_preflight_rejects_max_units_limit(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(
            EtoroReadClient,
            FakeReadClient(eligibility=_eligibility(max_units=Decimal("0.1"))),
        ),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    final_check = next(check for check in report.checks if check.name == "stage_a_feasibility")
    assert report.pre_flight == PREFLIGHT_FAIL
    assert final_check.reason == "proposal exceeds maxUnitsPerOrder"


def test_preflight_rejects_daily_trade_limit(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "preflight.sqlite3")
    for index in range(3):
        store.append_track_record(
            TrackRecordKind.ETORO_DEMO,
            {"demo_execution": True, "trade_index": index},
        )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=store,
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert "MAX_DAILY_TRADES_EXCEEDED" in _violations(report)


def test_preflight_rejects_insufficient_demo_cash_directly() -> None:
    proposal = _proposal(amount=Decimal("50"))
    decision = evaluate_demo_preflight(
        proposal=proposal,
        portfolio=_demo_snapshot(cash=Decimal("25"), total=Decimal("1000")),
        eligibility=_eligibility(minimum=Decimal("50")),
        quote=_quote(),
        instrument=_metadata(),
        kill_switch=KillSwitch(active=False, reason="test", clock=_now),
        now=_now(),
        maximum_age_seconds=300,
    )

    assert not decision.allowed
    assert "Demo buying power is insufficient" in decision.reasons


def test_preflight_rejects_cash_reserve_violation(tmp_path: Path) -> None:
    risk = RiskPolicyConfig(
        max_trade_size=Decimal("0.90"),
        max_single_position=Decimal("0.90"),
        min_cash_reserve=Decimal("0.10"),
    )
    report = build_first_demo_preflight_report(
        _config(risk=risk),
        values=_values(),
        client=cast(
            EtoroReadClient,
            FakeReadClient(eligibility=_eligibility(minimum=Decimal("910"))),
        ),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.risk_manager_result == RiskDecisionStatus.REJECTED.value
    assert "MIN_CASH_RESERVE_BREACHED" in _violations(report)


def test_preflight_rejects_position_exposure_violation(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(
            EtoroReadClient,
            FakeReadClient(
                demo=_demo_snapshot(
                    cash=Decimal("770"),
                    total=Decimal("1000"),
                    target_exposure=Decimal("230"),
                ),
                eligibility=_eligibility(minimum=Decimal("30")),
            ),
        ),
        agent=FixedProposalAgent(_proposal(amount=Decimal("30"))),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert "MAX_POSITION_EXPOSURE_EXCEEDED" in _violations(report)


def test_preflight_does_not_force_trade_when_agent_holds(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        agent=HoldAgent(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.aegis_agent_confidence == Decimal("0")
    assert report.proposed_action is None


def test_preflight_rejects_risk_manager_failure(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        agent=FixedProposalAgent(_proposal(amount=Decimal("150"))),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.risk_manager_result == RiskDecisionStatus.REJECTED.value
    assert "MAX_TRADE_SIZE_EXCEEDED" in _violations(report)


def test_expired_and_reused_authorizations_are_rejected() -> None:
    now = _now()
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    risk_manager = RiskManager(
        RiskPolicyConfig(),
        switch,
        authorization_key=b"step75-risk-authorization-key-32",
        clock=lambda: now,
    )
    proposal = _proposal()
    evaluation = risk_manager.evaluate(proposal, _risk_context(proposal, now=now))
    assert evaluation.authorization is not None

    with pytest.raises(RiskAuthorizationError):
        risk_manager.assert_authorized(
            proposal, evaluation.authorization, at=now + timedelta(seconds=121)
        )

    gate = RiskEnforcedExecutionGate(risk_manager)
    gate.admit(proposal, evaluation.authorization, at=now)
    with pytest.raises(AuthorizationAlreadyConsumedError):
        gate.admit(proposal, evaluation.authorization, at=now)


def test_stage_a_can_complete_with_active_kill_switch(tmp_path: Path) -> None:
    report = build_first_demo_preflight_report(
        _config(kill_switch=True),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "preflight.sqlite3"),
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_PASS
    assert report.ready_for_human_confirmation
    assert report.kill_switch_status == "ACTIVE"
    assert report.demo_write_performed is False


def test_risk_preview_can_ignore_kill_switch_only_without_authorization() -> None:
    now = _now()
    switch = KillSwitch(active=True, reason="safe default", clock=lambda: now)
    risk_manager = RiskManager(
        RiskPolicyConfig(),
        switch,
        authorization_key=b"step75-risk-authorization-key-32",
        clock=lambda: now,
    )
    proposal = _proposal()

    with pytest.raises(ValueError):
        risk_manager.evaluate(
            proposal,
            _risk_context(proposal, now=now),
            ignore_kill_switch=True,
        )

    evaluation = risk_manager.evaluate(
        proposal,
        _risk_context(proposal, now=now),
        issue_authorization=False,
        ignore_kill_switch=True,
    )

    assert evaluation.decision.status is RiskDecisionStatus.APPROVED
    assert evaluation.authorization is None
    assert evaluation.authorization_deferred


def test_stage_b_requires_exact_human_confirmation_before_write(tmp_path: Path) -> None:
    stage_a = _stage_a_report(tmp_path)
    confirmation = _confirmation_from(stage_a).model_copy(update={"amount": Decimal("6")})
    switch = KillSwitch(active=True, reason="safe default", clock=_now)
    transport = SequencedTransport([HttpResponse(202, {}, b'{"orderId":"order-1"}')])

    result = arm_and_submit_confirmed_demo_once(
        _config(kill_switch=True),
        confirmation,
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "stage-b.sqlite3"),
        kill_switch=switch,
        http=DisciplinedHttpClient(transport),
        clock=_now,
    )

    assert result.final_state == "NOT_EXECUTED"
    assert result.demo_submission_attempts == 0
    assert result.demo_write_performed is False
    assert transport.calls == []
    assert switch.state.active


def test_stage_b_revalidates_and_submits_exactly_once(tmp_path: Path) -> None:
    stage_a = _stage_a_report(tmp_path)
    client = FakeReadClient(order_state=ExecutionState.FILLED)
    switch = KillSwitch(active=True, reason="safe default", clock=_now)
    transport = SequencedTransport([HttpResponse(202, {}, b'{"orderId":"order-1"}')])

    result = arm_and_submit_confirmed_demo_once(
        _config(kill_switch=True),
        _confirmation_from(stage_a),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "stage-b.sqlite3"),
        kill_switch=switch,
        http=DisciplinedHttpClient(transport),
        clock=_now,
    )

    assert result.stage_a_pre_flight == PREFLIGHT_PASS
    assert result.ready_for_human_confirmation
    assert result.execution_arming_performed
    assert result.final_authorization_minted
    assert result.demo_submission_attempts == 1
    assert result.demo_write_performed
    assert result.real_write_performed is False
    assert result.final_state == ExecutionState.FILLED.value
    assert len(transport.calls) == 1
    assert "quote" in client.calls
    assert "demo_eligibility" in client.calls
    assert "demo_order_state" in client.calls
    assert switch.state.active


def test_stage_b_revalidates_market_data_before_write(tmp_path: Path) -> None:
    stage_a = _stage_a_report(tmp_path)
    client = FakeReadClient(quote=_quote(as_of=_now() - timedelta(minutes=20)))
    switch = KillSwitch(active=True, reason="safe default", clock=_now)
    transport = SequencedTransport([HttpResponse(202, {}, b'{"orderId":"order-1"}')])

    result = arm_and_submit_confirmed_demo_once(
        _config(kill_switch=True),
        _confirmation_from(stage_a),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "stage-b.sqlite3"),
        kill_switch=switch,
        http=DisciplinedHttpClient(transport),
        clock=_now,
    )

    assert result.final_state == "NOT_EXECUTED"
    assert result.demo_submission_attempts == 0
    assert transport.calls == []
    assert "quote" in client.calls
    assert switch.state.active


def test_stage_b_revalidates_eligibility_before_write(tmp_path: Path) -> None:
    stage_a = _stage_a_report(tmp_path)
    client = FakeReadClient(eligibility=_eligibility(allow_open=False))
    switch = KillSwitch(active=True, reason="safe default", clock=_now)
    transport = SequencedTransport([HttpResponse(202, {}, b'{"orderId":"order-1"}')])

    result = arm_and_submit_confirmed_demo_once(
        _config(kill_switch=True),
        _confirmation_from(stage_a),
        values=_values(),
        client=cast(EtoroReadClient, client),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "stage-b.sqlite3"),
        kill_switch=switch,
        http=DisciplinedHttpClient(transport),
        clock=_now,
    )

    assert result.final_state == "NOT_EXECUTED"
    assert result.demo_submission_attempts == 0
    assert transport.calls == []
    assert "demo_eligibility" in client.calls
    assert switch.state.active


def test_stage_b_requires_kill_switch_to_start_active(tmp_path: Path) -> None:
    stage_a = _stage_a_report(tmp_path)
    switch = KillSwitch(active=False, reason="unsafe test", clock=_now)
    transport = SequencedTransport([HttpResponse(202, {}, b'{"orderId":"order-1"}')])

    result = arm_and_submit_confirmed_demo_once(
        _config(kill_switch=False),
        _confirmation_from(stage_a),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "stage-b.sqlite3"),
        kill_switch=switch,
        http=DisciplinedHttpClient(transport),
        clock=_now,
    )

    assert result.final_state == "NOT_EXECUTED"
    assert result.demo_submission_attempts == 0
    assert transport.calls == []
    assert switch.state.active


def test_one_shot_window_reactivates_kill_switch_after_exception() -> None:
    switch = KillSwitch(active=True, reason="safe default", clock=_now)

    with pytest.raises(RuntimeError):
        with OneShotDemoExecutionWindow(switch, confirmed=True):
            assert not switch.state.active
            raise RuntimeError("boom")

    assert switch.state.active


@pytest.mark.parametrize(
    ("response", "expected_state"),
    [
        (HttpResponse(422, {}, b'{"error":"rejected"}'), ExecutionState.REJECTED.value),
        (TransportError("timeout"), "UNKNOWN_EXECUTION_STATE"),
        (RuntimeError("boom"), "UNKNOWN_EXECUTION_STATE"),
    ],
)
def test_stage_b_returns_kill_switch_active_after_non_success_outcomes(
    tmp_path: Path, response: HttpResponse | BaseException, expected_state: str
) -> None:
    stage_a = _stage_a_report(tmp_path)
    switch = KillSwitch(active=True, reason="safe default", clock=_now)
    transport = SequencedTransport([response])

    result = arm_and_submit_confirmed_demo_once(
        _config(kill_switch=True),
        _confirmation_from(stage_a),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=SqliteRecordStore(tmp_path / "stage-b.sqlite3"),
        kill_switch=switch,
        http=DisciplinedHttpClient(transport),
        clock=_now,
    )

    assert result.final_state == expected_state
    assert result.demo_submission_attempts == 1
    assert len(transport.calls) == 1
    assert switch.state.active


def test_demo_adapter_checks_kill_switch_immediately_before_http(tmp_path: Path) -> None:
    now = _now()
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    proposal = _proposal()
    gate, admitted = _admitted_trade(proposal, switch, now)
    store = ActivatingStore(tmp_path / "demo.sqlite3", switch)
    transport = SequencedTransport([HttpResponse(202, {}, b'{"orderId":"order-1"}')])
    adapter = EtoroDemoAdapter(
        credentials=EtoroCredentials(api_key="api", user_key="user"),
        http=DisciplinedHttpClient(transport),
        gate=gate,
        kill_switch=switch,
        registry=store,
        enabled=True,
        explicit_opt_in=True,
        clock=lambda: now,
    )

    with pytest.raises(DemoExecutionError):
        adapter.submit_demo(
            admitted, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("5"))
        )
    assert transport.calls == []


def test_wrong_environment_is_rejected_by_configuration() -> None:
    with pytest.raises(ConfigLoadError):
        load_config({"AEGIS_ENVIRONMENT": "PRODUCTION"})


def test_lookalike_or_incorrect_demo_route_is_rejected() -> None:
    assert_demo_route(DEMO_ORDER_URL)
    for url in (
        "http://public-api.etoro.com/api/v3/trading/execution/demo/orders",
        "https://public-api.etoro.com/api/v3/trading/execution/orders",
        "https://public-api.etoro.com/api/v3/trading/execution/demo/orders/extra",
    ):
        with pytest.raises(DemoExecutionError):
            assert_demo_route(url)


def test_duplicate_proposal_and_idempotency_key_are_rejected(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "preflight.sqlite3")
    proposal = _proposal(idempotency_key="duplicate-key")
    assert store.reserve_demo_submission("duplicate-key", {"state": "FILLED"})
    store.update_demo_submission(
        "duplicate-key",
        ExecutionState.FILLED.value,
        {"state": "FILLED"},
    )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        agent=FixedProposalAgent(proposal),
        store=store,
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert "DUPLICATE_ORDER" in _violations(report)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            HttpResponse(202, {}, b'{"orderId":"order-1","referenceId":"ref-1"}'),
            ExecutionState.SUBMITTED,
        ),
        (HttpResponse(422, {}, b'{"error":"rejected"}'), ExecutionState.REJECTED),
    ],
)
def test_demo_adapter_normalizes_success_and_broker_rejection(
    tmp_path: Path, response: HttpResponse, expected: ExecutionState
) -> None:
    now = _now()
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    proposal = _proposal()
    gate, admitted = _admitted_trade(proposal, switch, now)
    transport = SequencedTransport([response])
    adapter = EtoroDemoAdapter(
        credentials=EtoroCredentials(api_key="api", user_key="user"),
        http=DisciplinedHttpClient(transport),
        gate=gate,
        kill_switch=switch,
        registry=SqliteRecordStore(tmp_path / "demo.sqlite3"),
        enabled=True,
        explicit_opt_in=True,
        clock=lambda: now,
    )

    result = adapter.submit_demo(
        admitted, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("5"))
    )

    assert result.state is expected
    assert len(transport.calls) == 1


def test_http_timeout_and_ambiguous_post_activate_safe_halt(tmp_path: Path) -> None:
    now = _now()
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    proposal = _proposal()
    gate, admitted = _admitted_trade(proposal, switch, now)
    transport = SequencedTransport([TransportError("timeout")])
    adapter = EtoroDemoAdapter(
        credentials=EtoroCredentials(api_key="api", user_key="user"),
        http=DisciplinedHttpClient(transport),
        gate=gate,
        kill_switch=switch,
        registry=SqliteRecordStore(tmp_path / "demo.sqlite3"),
        enabled=True,
        explicit_opt_in=True,
        clock=lambda: now,
    )

    result = adapter.submit_demo(
        admitted, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("5"))
    )

    assert result.state is ExecutionState.UNKNOWN
    assert switch.state.active
    assert len(transport.calls) == 1


def test_demo_order_status_transport_error_is_classified() -> None:
    client = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"),
        DisciplinedHttpClient(SequencedTransport([TransportError("tls")])),
    )

    with pytest.raises(EtoroApiError) as exc_info:
        client.demo_order_state(_identity(), TEST_INSTRUMENT_ID, "order-1")

    assert exc_info.value.category is EtoroHttpFailureKind.NETWORK_TRANSPORT_ERROR
    assert exc_info.value.endpoint == "/api/v2/trading/info/demo/instrument-breakdown"


def test_reconciliation_success_and_mismatch_update_state(tmp_path: Path) -> None:
    now = _now()
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    store = SqliteRecordStore(tmp_path / "demo.sqlite3")
    submission = BrokerSubmission(
        idempotency_key="idempotency-1",
        request_id="request-1",
        state=ExecutionState.SUBMITTED,
        broker_order_id="order-1",
        proposal_id="proposal-1",
        authorization_id="auth-1",
        submitted_at=now,
    )
    assert store.reserve_demo_submission("idempotency-1", {"broker_order_id": "order-1"})

    assert (
        reconcile_demo_state(submission, ExecutionState.PENDING, switch, store)
        is ExecutionState.PENDING
    )
    with pytest.raises(ReconciliationError):
        reconcile_demo_state(submission, ExecutionState.UNKNOWN, switch, store)
    assert switch.state.active


def test_restart_after_pending_submission_blocks_new_preflight(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "preflight.sqlite3")
    assert store.reserve_demo_submission("pending-key", {"state": "SUBMITTED"})

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=store,
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert report.blocker_code == "RESTART_REPLAY_GUARD"


def test_restart_after_completed_submission_does_not_resubmit_same_key(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "preflight.sqlite3")
    assert store.reserve_demo_submission("completed-key", {"broker_order_id": "order-1"})
    store.update_demo_submission(
        "completed-key",
        ExecutionState.FILLED.value,
        {"broker_order_id": "order-1"},
    )

    report = build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        agent=FixedProposalAgent(_proposal(idempotency_key="completed-key")),
        store=store,
        clock=_now,
    )

    assert report.pre_flight == PREFLIGHT_FAIL
    assert "DUPLICATE_ORDER" in _violations(report)


def test_credentials_are_not_persisted_in_preflight_records(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "preflight.sqlite3")
    build_first_demo_preflight_report(
        _config(),
        values=_values(),
        client=cast(EtoroReadClient, FakeReadClient()),
        news_provider=PositiveNewsProvider(),
        store=store,
        clock=_now,
    )

    payload = json.dumps(store.list("etoro-first-demo-preflight"), sort_keys=True)
    assert "api-secret" not in payload
    assert "user-secret" not in payload
    with pytest.raises(SecretPersistenceError):
        store.append("bad", {"x-api-key": "api-secret"})


def _violations(report: FirstDemoPreflightReport) -> str:
    checks = report.checks
    return ",".join(
        str(check.metadata.get("violations", "")) + str(check.metadata.get("reasons", ""))
        for check in checks
    )
