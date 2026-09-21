from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.demo import DEMO_ORDER_URL
from app.brokers.etoro.demo_pilot import (
    EtoroDemoSubmissionPackage,
    RiskCheckedEtoroDemoSubmissionGateway,
)
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse, TransportError
from app.brokers.models import PreflightDecision
from app.config.models import ApplicationConfig
from app.domain.enums import (
    AssetClass,
    BrokerExecutionMode,
    Currency,
    ExecutionPolicy,
    HoldingPeriod,
    MarketStatus,
    OperatingMode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import AuthorizedCapitalEnvelope, RiskContext
from app.domain.universe import UniversalInstrument
from app.execution.gate import RiskEnforcedExecutionGate
from app.intelligence.models import AegisDecision, FeatureQuality, MarketBar, TimeFrame
from app.orchestration.active_intelligence import (
    ActiveIntelligenceCycleRecord,
    CycleChangeClassification,
    DataHealthState,
)
from app.orchestration.active_runtime import AegisEtoroAutomaticDemoRuntime
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.scanner.active import ActiveScannerBucket, ActiveScannerCandidate, ActiveScannerResult
from app.storage.sqlite import SqliteRecordStore


class FakeCycleProducer:
    def __init__(
        self,
        *,
        cycle: ActiveIntelligenceCycleRecord | None,
        scanner_result: ActiveScannerResult | None,
    ) -> None:
        self._cycle = cycle
        self.last_scanner_result = scanner_result
        self.calls = 0

    def run_if_new_bar_cycle(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: dict[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal = Decimal("200"),
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ActiveIntelligenceCycleRecord | None:
        self.calls += 1
        return self._cycle

    def run_cycle(self, **_: object) -> ActiveIntelligenceCycleRecord:
        raise AssertionError("legacy run_cycle must not be called by automatic Demo runtime")


class ScopedCycleProducer(FakeCycleProducer):
    """Test double that exposes the production asset-class scope argument."""

    def __init__(
        self,
        *,
        cycle: ActiveIntelligenceCycleRecord | None,
        scanner_result: ActiveScannerResult | None,
    ) -> None:
        super().__init__(cycle=cycle, scanner_result=scanner_result)
        self.scopes: list[frozenset[AssetClass] | None] = []

    def run_if_new_bar_cycle(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: dict[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal = Decimal("200"),
        asset_classes: frozenset[AssetClass] | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ActiveIntelligenceCycleRecord | None:
        self.scopes.append(asset_classes)
        return self._cycle


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


def test_no_cycle_never_reaches_demo_write_path(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=None, scanner_result=None)
    runtime = _runtime(tmp_path, producer=producer, values=_armed_values(), gateway=None)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "NO_CYCLE"
    assert producer.calls == 1
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0
    assert result["execution_admission_gate_reached"] is False


def test_runtime_requests_one_global_scan_instead_of_crypto_first_scope(
    tmp_path: Path,
) -> None:
    producer = ScopedCycleProducer(cycle=None, scanner_result=None)
    runtime = _runtime(tmp_path, producer=producer, values={}, gateway=None, enabled=False)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "NO_CYCLE"
    assert producer.scopes == [None]


def test_accepted_cycle_zero_top_does_not_invoke_demo(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result(()))
    runtime = _runtime(tmp_path, producer=producer, values=_armed_values(), gateway=None)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "NO_TOP_OPPORTUNITY"
    assert result["demo_broker_write_calls"] == 0


def test_accepted_top_disabled_pilot_submits_zero(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(tmp_path, producer=producer, values={}, gateway=None, enabled=False)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "DEMO_PILOT_DISABLED"
    assert result["eligible_count"] == 1
    assert result["demo_broker_write_calls"] == 0


def test_read_only_execution_mode_blocks_demo_before_packaging(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=None,
        execution_mode=BrokerExecutionMode.READ_ONLY,
        package_provider=lambda *_: (_ for _ in ()).throw(
            AssertionError("READ_ONLY must not package or submit")
        ),
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "EXECUTION_MODE_READ_ONLY"
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_advisory_policy_blocks_autonomous_demo_before_packaging(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=None,
        execution_policy=ExecutionPolicy.ADVISORY,
        package_provider=lambda *_: (_ for _ in ()).throw(
            AssertionError("ADVISORY must not package or submit")
        ),
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "EXECUTION_POLICY_NOT_AUTONOMOUS"
    assert result["top_opportunity_count"] == 1
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_kill_switch_blocks_autonomous_demo_before_packaging(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=None,
        kill_switch=True,
        package_provider=lambda *_: (_ for _ in ()).throw(
            AssertionError("kill switch must block packaging and submission")
        ),
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "KILL_SWITCH_ACTIVE"
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_accepted_top_invalid_notional_submits_zero(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values={"AEGIS_ETORO_DEMO_PILOT_NOTIONAL_EUR": "bad"},
        gateway=None,
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["blockers"] == ("MISSING_PILOT_NOTIONAL",)
    assert result["demo_broker_write_calls"] == 0


def test_accepted_top_without_verified_execution_package_fails_closed(
    tmp_path: Path,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    transport = StubEtoroTransport()
    gateway = _gateway(store, transport)
    runtime = _runtime(tmp_path, producer=producer, values=_armed_values(), gateway=gateway)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "BLOCKED"
    assert result["blockers"] == ("VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE",)
    assert transport.calls == []
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_accepted_top_all_gates_pass_posts_exactly_once(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    transport = StubEtoroTransport()
    risk_manager = RiskManager(
        ApplicationConfig().risk,
        KillSwitch(active=False, reason="test", clock=_now),
        authorization_key=b"runtime-demo-risk-authorization-key!",
        clock=_now,
    )
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=KillSwitch(active=False, reason="test", clock=_now),
        registry=store,
    )
    package = _package(_intent_key(_cycle().cycle_id))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=gateway,
        store=store,
        package_provider=lambda cycle, result: {"TEST": package},
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "SUBMITTED"
    assert result["submitted_count"] == 1
    assert result["demo_broker_write_calls"] == 1
    assert result["broker_write_calls_real"] == 0
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][1] == DEMO_ORDER_URL
    assert producer.calls == 1
    assert result["execution_admission_gate_reached"] is True


def test_restart_duplicate_blocks_second_demo_post(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    cycle = _cycle()
    first_transport = StubEtoroTransport()
    second_transport = StubEtoroTransport()
    package = _package(_intent_key(cycle.cycle_id))
    first = _runtime_with_real_gateway(
        tmp_path,
        store,
        cycle,
        first_transport,
        package_provider=lambda cycle, result: {"TEST": package},
    )
    second = _runtime_with_real_gateway(
        tmp_path,
        store,
        cycle,
        second_transport,
        package_provider=lambda cycle, result: {"TEST": package},
    )

    first_result = first.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )
    second_result = second.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert first_result["status"] == "SUBMITTED"
    assert second_result["blockers"] == ("DUPLICATE_CYCLE_CANDIDATE",)
    assert len(first_transport.calls) == 1
    assert second_transport.calls == []


def test_alpaca_only_mapping_fails_closed_without_demo_post(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    transport = StubEtoroTransport()
    gateway = _gateway(SqliteRecordStore(tmp_path / "store.sqlite3"), transport)
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=gateway,
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(broker_instrument_id="ALPACA_ONLY:TEST"),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "BLOCKED"
    assert result["blockers"] == ("AMBIGUOUS_INSTRUMENT_MAPPING",)
    assert transport.calls == []
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def _runtime_with_real_gateway(
    tmp_path: Path,
    store: SqliteRecordStore,
    cycle: ActiveIntelligenceCycleRecord,
    transport: StubEtoroTransport,
    package_provider=None,
) -> AegisEtoroAutomaticDemoRuntime:
    producer = FakeCycleProducer(cycle=cycle, scanner_result=_scanner_result((_top(),)))
    gateway = _gateway(store, transport)
    return _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=gateway,
        store=store,
        package_provider=package_provider,
    )


def _gateway(
    store: SqliteRecordStore,
    transport: StubEtoroTransport,
) -> RiskCheckedEtoroDemoSubmissionGateway:
    risk_manager = RiskManager(
        ApplicationConfig().risk,
        KillSwitch(active=False, reason="test", clock=_now),
        authorization_key=b"runtime-demo-risk-authorization-key!",
        clock=_now,
    )
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=KillSwitch(active=False, reason="test", clock=_now),
        registry=store,
    )
    return gateway


def _runtime(
    tmp_path: Path,
    *,
    producer: FakeCycleProducer,
    values: dict[str, str],
    gateway,
    enabled: bool = True,
    store: SqliteRecordStore | None = None,
    package_provider=None,
    execution_mode: BrokerExecutionMode | None = None,
    execution_policy: ExecutionPolicy | None = None,
    kill_switch: bool = False,
) -> AegisEtoroAutomaticDemoRuntime:
    config = ApplicationConfig(
        operating_mode=OperatingMode.ETORO_DEMO,
        broker_execution_mode=(
            execution_mode
            or (BrokerExecutionMode.DEMO_EXECUTION if enabled else BrokerExecutionMode.READ_ONLY)
        ),
        authorized_capital_eur=Decimal("50"),
        etoro_api_enabled=True,
        etoro_demo_execution_enabled=enabled,
        etoro_demo_automatic_pilot_enabled=enabled,
        execution_policy=(
            execution_policy
            or (ExecutionPolicy.AUTONOMOUS if enabled else ExecutionPolicy.ADVISORY)
        ),
        kill_switch=kill_switch,
    )
    return AegisEtoroAutomaticDemoRuntime(
        config=config,
        values=values,
        orchestrator=producer,
        registry=store or SqliteRecordStore(tmp_path / "store.sqlite3"),
        gateway=gateway,
        package_provider=package_provider,
    )


def _armed_values() -> dict[str, str]:
    return {"AEGIS_ETORO_DEMO_PILOT_NOTIONAL_EUR": "10"}


def _package(idempotency_key: str) -> EtoroDemoSubmissionPackage:
    proposal = TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000901"),
        idempotency_key=idempotency_key,
        created_at=_now(),
        instrument_id=1001,
        symbol="TEST",
        asset_class=AssetClass.EQUITY,
        side=TradeSide.BUY,
        intent=TradeIntent.OPEN,
        amount=Decimal("10"),
        currency=Currency.EUR,
        target_weight=Decimal("0.05"),
        current_weight=Decimal("0"),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="accepted TOP_OPPORTUNITY runtime pilot",
        evidence=(
            EvidenceItem(
                source="fixture",
                timestamp=_now(),
                summary="causal",
                confidence=Decimal("0.9"),
            ),
        ),
        confidence=Decimal("0.80"),
        risk_factors=(),
        invalidation_conditions=("runtime test invalidation",),
        expected_holding_period=HoldingPeriod.DAYS,
    )
    return EtoroDemoSubmissionPackage(
        proposal=proposal,
        risk_context=RiskContext(
            evaluated_at=_now(),
            portfolio=_portfolio(),
            price=None,
            instrument=None,
            market_data_available=False,
            news_data_available=True,
            daily_new_trade_count=0,
            recent_idempotency_keys=frozenset(),
        ).model_copy(
            update={
                "price": _price(),
                "instrument": _instrument_metadata(),
                "market_data_available": True,
                "capital_envelope": AuthorizedCapitalEnvelope(
                    authorized_capital_eur=Decimal("50"), managed_exposure_eur=Decimal("0")
                ),
            }
        ),
        preflight=PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")),
    )


def _price():
    from app.domain.market import PriceSnapshot

    return PriceSnapshot(
        instrument_id=1001,
        symbol="TEST",
        price=Decimal("100"),
        as_of=_now(),
        source="fixture",
    )


def _instrument_metadata():
    from app.domain.market import InstrumentMetadata

    return InstrumentMetadata(
        instrument_id=1001,
        symbol="TEST",
        asset_class=AssetClass.EQUITY,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("1"),
        metadata_as_of=_now(),
        source="fixture",
    )


def _intent_key(cycle_id: str) -> str:
    return f"etoro-demo-pilot:{cycle_id}:TEST:OPEN"


def _now() -> datetime:
    return datetime(2026, 8, 31, 10, tzinfo=UTC)


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.EUR,
        cash=Decimal("200"),
        reported_total_value=Decimal("200"),
        peak_value=Decimal("200"),
    )


def _instrument(*, broker_instrument_id: str = "1001") -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id=broker_instrument_id,
        symbol="TEST",
        display_name="TEST",
        asset_class=AssetClass.EQUITY,
        currency=Currency.EUR,
        exchange="TEST",
        market_status=MarketStatus.OPEN,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=_now(),
    )


def _bars() -> tuple[MarketBar, ...]:
    return (
        MarketBar(
            instrument=_instrument(),
            timestamp=_now(),
            timeframe=TimeFrame.ONE_HOUR,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
            currency=Currency.EUR,
            source="fixture",
            data_quality=FeatureQuality.GOOD,
        ),
    )


def _top() -> ActiveScannerCandidate:
    return ActiveScannerCandidate(
        symbol="TEST",
        full_asset_name="TEST",
        asset_class=AssetClass.EQUITY,
        timestamp=_now(),
        timeframe=TimeFrame.ONE_HOUR,
        current_market_state=MarketStatus.OPEN,
        opportunity_score=Decimal("75"),
        confidence=Decimal("0.8"),
        regime=("UPTREND",),
        decision=AegisDecision.BUY,
        bucket=ActiveScannerBucket.TOP_OPPORTUNITIES,
        data_quality_state=FeatureQuality.GOOD,
        current_position_state="NO_POSITION",
        freshness="FRESH",
        provider_provenance=("fixture",),
        affordable_fractionally=True,
        proposed_capital_allocation=Decimal("10"),
        remaining_simulated_cash=Decimal("190"),
        existing_exposure=Decimal("0"),
        diversification_concentration_impact="NEUTRAL",
        rank=1,
    )


def _scanner_result(candidates: tuple[ActiveScannerCandidate, ...]) -> ActiveScannerResult:
    return ActiveScannerResult(
        as_of=_now(),
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
        candidates=candidates,
        top_opportunities=_candidates_for_bucket(
            candidates,
            ActiveScannerBucket.TOP_OPPORTUNITIES,
        ),
        watchlist=_candidates_for_bucket(candidates, ActiveScannerBucket.WATCHLIST),
        no_trade=_candidates_for_bucket(candidates, ActiveScannerBucket.NO_TRADE),
        rejected=_candidates_for_bucket(candidates, ActiveScannerBucket.REJECTED),
        duplicate_decisions_prevented=0,
        existing_positions_monitored=0,
    )


def _candidates_for_bucket(
    candidates: tuple[ActiveScannerCandidate, ...],
    bucket: ActiveScannerBucket,
) -> tuple[ActiveScannerCandidate, ...]:
    return tuple(candidate for candidate in candidates if candidate.bucket is bucket)


def _cycle() -> ActiveIntelligenceCycleRecord:
    return ActiveIntelligenceCycleRecord(
        cycle_id="cycle-" + "b" * 64,
        scheduled_at=_now(),
        started_at=_now(),
        completed_at=_now(),
        market_data_timestamp=_now(),
        news_cutoff_timestamp=_now(),
        symbols_evaluated=("TEST",),
        positions_monitored=0,
        fresh_news_events=0,
        duplicate_events_ignored=0,
        material_events=0,
        global_risk_context={},
        top_opportunities=("TEST",),
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
        scan_cycle_timestamp=_now(),
    )
