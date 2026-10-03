"""Step 8.0 strategy validation, backtesting, and walk-forward tests."""

import inspect
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

import app.validation.engine
import app.validation.execution
import app.validation.runtime
from app.agent.context import AegisAgentContext
from app.agent.exit_policy import (
    EXIT_POLICY_V1_LEGACY,
    EXIT_POLICY_V2_EXPERIMENT_V2_VERSION,
    EXIT_POLICY_V2_GUARDED,
    EXIT_POLICY_V2_PARAMETER_BUNDLE_VERSION,
    EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE,
    EXIT_POLICY_V2_VERSION,
    ExitPolicy,
    ExitPolicyReasonCode,
    ExitPolicyV2ExperimentManifest,
    ExitPolicyV2Guarded,
    ExitPolicyV2ParameterBundle,
    ExitPolicyV2Parameters,
    ExitPolicyV2ValidationStatus,
    PositionManagementState,
    default_exit_policy_v2_candidate_registry,
    default_exit_policy_v2_experiment_manifest,
    default_exit_policy_v2_preregistered_experiment_v2_manifest,
    default_exit_policy_v2_validation_protocol,
    exit_action_blocks_entry,
    exit_policy_v2_parameter_inventory,
    reduce_amount_not_exceeding_long_position,
    select_exit_policy,
    select_exit_policy_for_historical_validation,
)
from app.agent.models import AegisAgentResult, AegisAnalysis
from app.agent.service import DeterministicAegisAgent
from app.config import load_config
from app.config.models import ApplicationConfig
from app.data.runtime import (
    _exit_policy_v2_multi_asset_qualification,
    build_exit_policy_v2_train_validation_report,
    build_prospective_shadow_validation_readiness_report,
)
from app.domain.enums import (
    AssetClass,
    Currency,
    MarketStatus,
    RecommendedAction,
    RiskDecisionStatus,
    RiskViolationCode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import InstrumentMetadata, MarketQuote
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskDecision, RiskEvaluation, RiskViolation
from app.domain.universe import UniversalInstrument
from app.domain.versions import HISTORICAL_DATA_VERSION
from app.intelligence.models import (
    AegisDecision,
    AegisOpportunityAnalysis,
    MarketBar,
    RegimeLabel,
    ScoreBand,
    StrategyDirection,
    TimeFrame,
)
from app.intelligence.service import AegisOpportunityIntelligenceEngine
from app.main.__main__ import main
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SecretPersistenceError, SqliteRecordStore
from app.validation.calibration import CalibrationAnalyzer
from app.validation.costs import TransactionCostModel
from app.validation.engine import HistoricalValidationEngine
from app.validation.execution import SimulatedExecutionEngine, SimulatedExecutionError
from app.validation.freshness import HistoricalReplayFreshnessPolicy
from app.validation.metrics import NOT_ENOUGH_DATA, PerformanceMetricCalculator
from app.validation.models import (
    HistoricalValidationDataset,
    OverfitRisk,
    ParameterStability,
    PerformanceSummary,
    PeriodSplitName,
    ReplayDecision,
    ReplayDecisionStatus,
    SimulatedPortfolioState,
    SimulatedPosition,
    SlippageScenario,
    StrategyQualificationStatus,
    TransactionCostAssumptions,
    WalkForwardConfig,
)
from app.validation.prospective import (
    EUR200_RESEARCH_BASELINE_VERSION,
    PROSPECTIVE_SHADOW_MANIFEST_VERSION,
    Eur200ResearchBaseline,
    default_prospective_shadow_store,
)
from app.validation.qualification import (
    MonteCarloTradeSequence,
    OverfitRiskAnalyzer,
    ParameterStabilityAnalyzer,
    StrategyQualificationEngine,
)
from app.validation.replay import (
    FutureLeakageError,
    HistoricalReplayClock,
    WalkForwardSplitter,
    build_dataset_metadata,
    split_periods,
)
from app.validation.runtime import build_strategy_validation_report
from app.validation.storage import StrategyValidationStore


def _now() -> datetime:
    return datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _instrument(
    symbol: str = "TST",
    *,
    instrument_id: int = 8001,
    asset_class: AssetClass = AssetClass.EQUITY,
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="fixture",
        broker_instrument_id=str(instrument_id),
        symbol=symbol,
        display_name=f"{symbol} Fixture",
        asset_class=asset_class,
        currency=Currency.USD,
        market_status=MarketStatus.CONTINUOUS_24_7
        if asset_class is AssetClass.CRYPTO
        else MarketStatus.OPEN,
        tradeable=True,
        buy_allowed=True,
        sell_allowed=True,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("5"),
        bid=Decimal("99.90"),
        ask=Decimal("100.10"),
        last_price=Decimal("100"),
        price_timestamp=_now(),
        metadata_timestamp=_now(),
    )


def _bars(
    instrument: UniversalInstrument,
    *,
    count: int = 80,
    start: Decimal = Decimal("70"),
    step: Decimal = Decimal("0.50"),
    end_at: datetime | None = None,
) -> tuple[MarketBar, ...]:
    end = end_at or _now()
    start_at = end - timedelta(days=count - 1)
    bars: list[MarketBar] = []
    for index in range(count):
        close = start + Decimal(index) * step
        open_price = close - Decimal("0.10")
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=start_at + timedelta(days=index),
                timeframe=TimeFrame.ONE_DAY,
                open=open_price,
                high=close + Decimal("0.25"),
                low=open_price - Decimal("0.25"),
                close=close,
                volume=Decimal("100000") + Decimal(index * 100),
                currency=Currency.USD,
                source="step80-fixture",
            )
        )
    return tuple(bars)


def _dataset() -> HistoricalValidationDataset:
    instruments = (
        _instrument("AAA", instrument_id=8001, asset_class=AssetClass.EQUITY),
        _instrument("BBB", instrument_id=8002, asset_class=AssetClass.ETF),
        _instrument("CCC", instrument_id=8003, asset_class=AssetClass.CRYPTO),
    )
    bars_by_instrument = {
        instruments[0].key: _bars(instruments[0], start=Decimal("50"), step=Decimal("0.60")),
        instruments[1].key: _bars(instruments[1], start=Decimal("90"), step=Decimal("0.10")),
        instruments[2].key: _bars(instruments[2], start=Decimal("30"), step=Decimal("0.30")),
    }
    metadata = build_dataset_metadata(
        provider="fixture",
        instruments=instruments,
        bars_by_instrument=bars_by_instrument,
        timeframes=(TimeFrame.ONE_DAY,),
        created_at=_now(),
        mapping_version=HISTORICAL_DATA_VERSION,
    )
    return HistoricalValidationDataset(metadata=metadata, bars_by_instrument=bars_by_instrument)


def _quote(price: Decimal = Decimal("100"), *, currency: Currency = Currency.USD) -> MarketQuote:
    return MarketQuote(
        instrument_id=8001,
        symbol="TST",
        price=price,
        as_of=_now(),
        currency=currency,
        source="test",
        previous_close=price - Decimal("1"),
        bid=price - Decimal("0.10"),
        ask=price + Decimal("0.10"),
        market_status=MarketStatus.OPEN,
    )


def _proposal(
    *,
    side: TradeSide = TradeSide.BUY,
    intent: TradeIntent = TradeIntent.OPEN,
    amount: Decimal = Decimal("100"),
) -> TradeProposal:
    return TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000008001"),
        idempotency_key="step80-proposal-0001",
        created_at=_now(),
        instrument_id=8001,
        symbol="TST",
        asset_class=AssetClass.EQUITY,
        side=side,
        intent=intent,
        amount=amount,
        currency=Currency.USD,
        target_weight=Decimal("0.10"),
        current_weight=Decimal("0"),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="test",
        evidence=(),
        confidence=Decimal("0.80"),
        risk_factors=("market risk",),
        invalidation_conditions=("test invalidation",),
        expected_holding_period="MONTHS",
    )


def _dynamic_proposal(
    context: AegisAgentContext,
    *,
    side: TradeSide,
    intent: TradeIntent,
    amount: Decimal,
) -> TradeProposal:
    quote = context.quotes[0]
    instrument = context.instruments[0]
    return TradeProposal(
        proposal_id=UUID(int=quote.instrument_id * 1_000_000 + int(quote.as_of.timestamp())),
        idempotency_key=f"step80-{quote.instrument_id}-{intent.value}-{quote.as_of.isoformat()}",
        created_at=quote.as_of,
        instrument_id=quote.instrument_id,
        symbol=quote.symbol,
        asset_class=instrument.asset_class,
        side=side,
        intent=intent,
        amount=amount,
        currency=quote.currency,
        target_weight=Decimal("0.10"),
        current_weight=context.portfolio.weight_for(quote.instrument_id),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="test lifecycle wiring",
        evidence=(),
        confidence=Decimal("0.90"),
        risk_factors=("market risk",),
        invalidation_conditions=("test invalidation",),
        expected_holding_period="DAYS",
    )


def _risk(status: RiskDecisionStatus) -> RiskEvaluation:
    violations = (
        ()
        if status is RiskDecisionStatus.APPROVED
        else (
            RiskViolation(
                code=RiskViolationCode.MIN_CASH_RESERVE_BREACHED,
                message="test rejection",
            ),
        )
    )
    return RiskEvaluation(
        decision=RiskDecision(
            decision_id=UUID("00000000-0000-0000-0000-000000008002"),
            proposal_id=UUID("00000000-0000-0000-0000-000000008001"),
            status=status,
            evaluated_at=_now(),
            violations=violations,
            metrics={},
        ),
        authorization_deferred=status is RiskDecisionStatus.APPROVED,
    )


def _portfolio(
    *,
    cash: Decimal = Decimal("1000"),
    positions: tuple[SimulatedPosition, ...] = (),
) -> SimulatedPortfolioState:
    return SimulatedPortfolioState(
        as_of=_now(),
        currency=Currency.USD,
        cash=cash,
        positions=positions,
        peak_value=Decimal("1000"),
    )


def _portfolio_snapshot(
    *,
    cash: Decimal = Decimal("900"),
    market_price: Decimal = Decimal("102"),
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.USD,
        cash=cash,
        positions=(
            Position(
                position_id="held-aaa",
                instrument_id=8001,
                symbol="AAA",
                settlement_type=SettlementType.REAL,
                units=Decimal("1"),
                average_entry_price=Decimal("100"),
                market_price=market_price,
            ),
        ),
        reported_total_value=cash + market_price,
        peak_value=cash + market_price,
    )


def _exit_v2_params(**updates: object) -> ExitPolicyV2Parameters:
    values: dict[str, object] = {
        "parameter_bundle_id": "synthetic-test-only-not-calibrated",
        "frozen": True,
        "capital_reduce_drawdown_pct": Decimal("0.08"),
        "capital_close_drawdown_pct": Decimal("0.15"),
        "thesis_reduce_confidence_drop": Decimal("0.10"),
        "thesis_close_confidence_drop": Decimal("0.25"),
        "thesis_reduce_score_drop": Decimal("10"),
        "thesis_close_score_drop": Decimal("25"),
        "trailing_min_mfe_pct": Decimal("0.10"),
        "trailing_reduce_drawdown_pct": Decimal("0.05"),
        "trailing_close_drawdown_pct": Decimal("0.12"),
        "defensive_persistence_reduce_count": 2,
        "defensive_persistence_close_count": 4,
        "concentration_reduce_weight": Decimal("0.30"),
        "stagnation_bars": 8,
        "stagnation_abs_return_pct": Decimal("0.01"),
        "cooldown_bars_after_reduce": 2,
        "cooldown_bars_after_close": 4,
    }
    values.update(updates)
    return ExitPolicyV2Parameters.model_validate(values)


def _position_state(**updates: object) -> PositionManagementState:
    values: dict[str, object] = {
        "instrument_id": 8001,
        "symbol": "AAA",
        "entry_timestamp": _now() - timedelta(days=10),
        "entry_price": Decimal("100"),
        "cost_basis": Decimal("100"),
        "entry_confidence": Decimal("0.80"),
        "entry_opportunity_score": Decimal("82"),
        "entry_regime": RegimeLabel.UPTREND,
        "current_timestamp": _now(),
        "current_price": Decimal("102"),
        "current_confidence": Decimal("0.78"),
        "current_opportunity_score": Decimal("80"),
        "current_regime": RegimeLabel.UPTREND,
        "position_market_value": Decimal("102"),
        "position_weight": Decimal("0.10"),
        "bars_held": 3,
        "mfe": Decimal("0.05"),
        "mae": Decimal("-0.02"),
        "post_entry_peak_price": Decimal("105"),
        "drawdown_from_post_entry_peak": Decimal("0.0286"),
        "defensive_signal_persistence": 0,
    }
    values.update(updates)
    return PositionManagementState.model_validate(values)


def _exit_analysis() -> AegisOpportunityAnalysis:
    instrument = _instrument("AAA", instrument_id=8001)
    bars = _bars(instrument)
    clock = HistoricalReplayClock(bars[-1].timestamp)
    candidate = app.validation.engine._candidate_at(
        instrument=instrument,
        bars=bars,
        portfolio=_portfolio(
            positions=(
                SimulatedPosition(
                    instrument_id=8001,
                    symbol="AAA",
                    asset_class=AssetClass.EQUITY,
                    units=Decimal("1"),
                    average_entry_price=Decimal("100"),
                    market_price=Decimal("102"),
                ),
            )
        ),
        clock=clock,
    )
    return AegisOpportunityIntelligenceEngine().analyze_candidate(
        candidate=candidate,
        portfolio=PortfolioSnapshot(
            as_of=_now(),
            currency=Currency.USD,
            cash=Decimal("900"),
            positions=(
                Position(
                    position_id="held-aaa",
                    instrument_id=8001,
                    symbol="AAA",
                    settlement_type=SettlementType.REAL,
                    units=Decimal("1"),
                    average_entry_price=Decimal("100"),
                    market_price=Decimal("102"),
                ),
            ),
            reported_total_value=Decimal("1002"),
            peak_value=Decimal("1002"),
        ),
        bars_by_timeframe={TimeFrame.ONE_DAY: bars},
        as_of=_now(),
    )


def _decision(
    score: Decimal,
    confidence: Decimal,
    forward_return: Decimal,
    *,
    symbol: str = "AAA",
    regime: RegimeLabel = RegimeLabel.UPTREND,
) -> ReplayDecision:
    return ReplayDecision(
        timestamp=_now(),
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        score=score,
        score_band=ScoreBand.VERY_STRONG if score >= 80 else ScoreBand.INTERESTING,
        confidence=confidence,
        regime=regime,
        aegis_decision="BUY",
        status=ReplayDecisionStatus.SIMULATED_EXECUTED,
        forward_return=forward_return,
    )


def _metrics(
    *,
    trade_count: int = 12,
    total_return: Decimal = Decimal("0.12"),
    expectancy: Decimal = Decimal("1"),
    profit_factor: Decimal = Decimal("1.5"),
    drawdown: Decimal = Decimal("0.10"),
) -> PerformanceSummary:
    return PerformanceSummary(
        total_return=total_return,
        annualized_return=total_return,
        win_rate=Decimal("0.60"),
        loss_rate=Decimal("0.40"),
        profit_factor=profit_factor,
        expectancy=expectancy,
        average_win=Decimal("2"),
        average_loss=Decimal("1"),
        payoff_ratio=Decimal("2"),
        volatility=Decimal("0.12"),
        sharpe_ratio=Decimal("1"),
        sortino_ratio=Decimal("1.2"),
        maximum_drawdown=drawdown,
        calmar_ratio=Decimal("1"),
        recovery_factor=Decimal("1"),
        mfe=Decimal("0.05"),
        mae=Decimal("-0.02"),
        turnover=Decimal("1"),
        exposure=Decimal("1"),
        trade_count=trade_count,
        average_holding_period=NOT_ENOUGH_DATA,
    )


def test_historical_replay_clock_blocks_future_leakage_and_filters_records() -> None:
    instrument = _instrument()
    bars = _bars(instrument, count=3, end_at=_now() + timedelta(days=1))
    clock = HistoricalReplayClock(_now())

    visible = clock.visible_bars(bars)

    assert len(visible) == 2
    with pytest.raises(FutureLeakageError):
        clock.assert_visible(_now() + timedelta(seconds=1), "future candle")


def test_dataset_metadata_digest_and_splits_are_versioned_and_reproducible() -> None:
    dataset = _dataset()
    clone = _dataset()
    splits = split_periods(
        tuple(bar.timestamp for bar in dataset.bars_by_instrument["fixture:8001"])
    )

    assert dataset.metadata.dataset_id == clone.metadata.dataset_id
    assert dataset.metadata.data_digest == clone.metadata.data_digest
    assert dataset.metadata.mapping_version == HISTORICAL_DATA_VERSION
    assert [split.name for split in splits] == [
        PeriodSplitName.TRAIN,
        PeriodSplitName.VALIDATION,
        PeriodSplitName.OUT_OF_SAMPLE,
    ]
    assert splits[0].end < splits[1].start < splits[2].start


def test_walk_forward_windows_roll_without_touching_oos_tuning() -> None:
    timestamps = tuple(_now() + timedelta(days=index) for index in range(80))
    windows = WalkForwardSplitter().windows(
        timestamps,
        WalkForwardConfig(
            training_length=30,
            validation_length=10,
            step_size=10,
            minimum_observations=40,
        ),
    )

    assert len(windows) == 5
    assert windows[0].train_end < windows[0].validation_start
    assert windows[1].train_start == timestamps[10]


def test_transaction_cost_model_uses_spread_slippage_fee_and_unknown_material_costs() -> None:
    base = TransactionCostModel(
        TransactionCostAssumptions(
            scenario=SlippageScenario.BASE,
            slippage_rate=Decimal("0.001"),
            broker_fee_rate=Decimal("0.001"),
        )
    ).estimate(quote=_quote(), gross_value=Decimal("100"), portfolio_currency=Currency.USD)
    severe = TransactionCostModel(
        TransactionCostAssumptions(scenario=SlippageScenario.SEVERE)
    ).estimate(quote=_quote(), gross_value=Decimal("100"), portfolio_currency=Currency.USD)
    fx_unknown = TransactionCostModel().estimate(
        quote=_quote(currency=Currency.EUR),
        gross_value=Decimal("100"),
        portfolio_currency=Currency.USD,
        holding_days=1,
    )

    assert base.total_cost > Decimal("0")
    assert severe.slippage_cost > base.slippage_cost
    assert not fx_unknown.complete
    assert fx_unknown.unknown_material_costs == (
        "FX_CONVERSION_COST",
        "OVERNIGHT_OR_FINANCING_COST",
    )


def test_simulated_execution_opens_reduces_and_blocks_impossible_negative_holdings() -> None:
    engine = SimulatedExecutionEngine(TransactionCostModel())
    opened, buy_record = engine.execute(
        portfolio=_portfolio(),
        proposal=_proposal(),
        quote=_quote(),
        risk=_risk(RiskDecisionStatus.APPROVED),
    )
    position = opened.positions[0]
    close_proposal = _proposal(
        side=TradeSide.SELL, intent=TradeIntent.CLOSE, amount=position.market_value
    )

    closed, sell_record = engine.execute(
        portfolio=opened,
        proposal=close_proposal,
        quote=_quote(price=Decimal("110")),
        risk=_risk(RiskDecisionStatus.APPROVED),
    )
    with pytest.raises(SimulatedExecutionError):
        engine.execute(
            portfolio=closed,
            proposal=_proposal(
                side=TradeSide.SELL, intent=TradeIntent.REDUCE, amount=Decimal("10")
            ),
            quote=_quote(),
            risk=_risk(RiskDecisionStatus.APPROVED),
        )

    assert buy_record.status is ReplayDecisionStatus.SIMULATED_EXECUTED
    assert sell_record.realized_pnl > 0
    assert closed.positions == ()


def test_risk_rejected_trade_remains_rejected_without_portfolio_mutation() -> None:
    engine = SimulatedExecutionEngine()
    portfolio, record = engine.execute(
        portfolio=_portfolio(),
        proposal=_proposal(),
        quote=_quote(),
        risk=_risk(RiskDecisionStatus.REJECTED),
    )

    assert portfolio.positions == ()
    assert record.status is ReplayDecisionStatus.RISK_REJECTED
    assert record.risk_status is RiskDecisionStatus.REJECTED


def test_performance_metrics_cover_return_drawdown_profit_factor_and_ratios() -> None:
    curve = (
        (_now(), Decimal("100")),
        (_now() + timedelta(days=1), Decimal("110")),
        (_now() + timedelta(days=2), Decimal("105")),
        (_now() + timedelta(days=3), Decimal("120")),
    )
    engine = SimulatedExecutionEngine()
    _, win = engine.execute(
        portfolio=_portfolio(),
        proposal=_proposal(),
        quote=_quote(),
        risk=_risk(RiskDecisionStatus.APPROVED),
    )
    win = win.model_copy(update={"realized_pnl": Decimal("10")})
    loss = win.model_copy(update={"realized_pnl": Decimal("-4")})

    metrics = PerformanceMetricCalculator().calculate(equity_curve=curve, trades=(win, loss))

    assert metrics.total_return == Decimal("0.2")
    assert isinstance(metrics.maximum_drawdown, Decimal)
    assert metrics.maximum_drawdown > Decimal("0")
    assert metrics.profit_factor == Decimal("2.5")
    assert metrics.expectancy == Decimal("3")
    assert metrics.trade_count == 2


def test_score_and_confidence_calibration_are_independent() -> None:
    decisions = (
        _decision(Decimal("85"), Decimal("0.30"), Decimal("0.04")),
        _decision(Decimal("65"), Decimal("0.90"), Decimal("0.00")),
        _decision(Decimal("85"), Decimal("0.90"), Decimal("-0.02")),
    )
    analyzer = CalibrationAnalyzer()
    score_buckets = analyzer.by_score(decisions)
    confidence_buckets = analyzer.by_confidence(decisions)

    assert next(item for item in score_buckets if item.label == "80-89").sample_size == 2
    assert next(item for item in confidence_buckets if item.label == "0.8-1.0").sample_size == 2
    assert analyzer.score_80_outperforms_60s(score_buckets)


def test_overfit_parameter_stability_and_monte_carlo_are_deterministic() -> None:
    stability = ParameterStabilityAnalyzer().classify(
        (Decimal("0.10"), Decimal("0.11"), Decimal("0.12"))
    )
    fragile = ParameterStabilityAnalyzer().classify(
        (Decimal("0.50"), Decimal("-0.10"), Decimal("0.02"))
    )
    overfit = OverfitRiskAnalyzer().classify(
        trade_count=2,
        train_return=Decimal("0.50"),
        oos_return=Decimal("-0.05"),
        max_single_instrument_share=Decimal("0.80"),
        max_single_regime_share=Decimal("0.90"),
    )
    mc_a = MonteCarloTradeSequence().run(
        (Decimal("0.02"), Decimal("-0.01"), Decimal("0.03"), Decimal("-0.02"), Decimal("0.01")),
        seed=42,
        iterations=25,
    )
    mc_b = MonteCarloTradeSequence().run(
        (Decimal("0.02"), Decimal("-0.01"), Decimal("0.03"), Decimal("-0.02"), Decimal("0.01")),
        seed=42,
        iterations=25,
    )

    assert stability is ParameterStability.STABLE
    assert fragile is ParameterStability.PARAMETER_FRAGILE
    assert overfit is OverfitRisk.OVERFIT_RISK_HIGH
    assert mc_a == mc_b


def test_strategy_qualification_allows_demo_eligible_but_never_real_eligible() -> None:
    qualification = StrategyQualificationEngine().qualify(
        metrics=_metrics(),
        oos_metrics=_metrics(trade_count=3),
        walk_forward_passed=True,
        score_calibration_passed=True,
        parameter_stability=ParameterStability.STABLE,
        overfit_risk=OverfitRisk.OVERFIT_RISK_LOW,
        stressed_cost_passed=True,
    )
    rejected = StrategyQualificationEngine().qualify(
        metrics=_metrics(trade_count=20),
        oos_metrics=_metrics(trade_count=5),
        walk_forward_passed=True,
        score_calibration_passed=True,
        parameter_stability=ParameterStability.STABLE,
        overfit_risk=OverfitRisk.OVERFIT_RISK_HIGH,
        stressed_cost_passed=True,
    )

    assert qualification.status is StrategyQualificationStatus.DEMO_ELIGIBLE
    assert qualification.demo_consideration_allowed
    assert rejected.status is StrategyQualificationStatus.REJECTED
    assert "REAL_ELIGIBLE" not in {status.value for status in StrategyQualificationStatus}


def test_historical_validation_engine_reuses_production_components_and_zero_writes() -> None:
    result = HistoricalValidationEngine().run(dataset=_dataset(), replay_stride=10)

    assert result.dataset.data_digest == _dataset().metadata.data_digest
    assert result.period_splits[2].name is PeriodSplitName.OUT_OF_SAMPLE
    assert result.walk_forward_windows
    assert result.decisions
    assert result.score_calibration
    assert result.confidence_calibration
    assert "StrategyEnsemble" in result.strategy_performance
    assert result.asset_class_performance
    assert result.regime_performance
    assert result.broker_write is False
    assert result.broker_write_calls == 0
    assert result.real_execution_available is False
    assert result.qualification.status in set(StrategyQualificationStatus)


def test_historical_engine_records_future_records_ignored_without_lookahead() -> None:
    dataset = _dataset()
    result = HistoricalValidationEngine().run(dataset=dataset, replay_stride=20)

    assert any(decision.future_records_ignored > 0 for decision in result.decisions)
    assert all(decision.timestamp <= dataset.metadata.end for decision in result.decisions)


def test_validation_store_is_restart_safe_and_rejects_secret_shaped_payloads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "strategy-validation.sqlite3"
    result = HistoricalValidationEngine().run(dataset=_dataset(), replay_stride=20)
    store = StrategyValidationStore(SqliteRecordStore(path))
    record_id = store.record_result(result)
    restarted = StrategyValidationStore(SqliteRecordStore(path))

    assert record_id == 1
    assert restarted.list_results()[0]["run_id"] == result.run_id
    with pytest.raises(SecretPersistenceError):
        SqliteRecordStore(path).append("strategy-validation", {"api_key": "never"})


def test_validate_strategies_runtime_and_cli_are_offline_fixture_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    payload = build_strategy_validation_report(ApplicationConfig(), clock=_now)

    assert payload["status"] == "RESEARCH_COMPLETE"
    assert payload["broker_write"] is False
    assert payload["broker_write_calls"] == 0
    assert payload["real_execution_available"] is False

    assert main(("validate-strategies", "--offline-fixture"), values={}) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["status"] == "RESEARCH_COMPLETE"
    assert cli_payload["broker_write"] is False


def test_validate_strategies_blocks_if_demo_execution_is_enabled() -> None:
    payload = build_strategy_validation_report(
        ApplicationConfig(
            operating_mode="ETORO_DEMO",
            etoro_api_enabled=True,
            etoro_demo_execution_enabled=True,
        ),
        clock=_now,
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "DEMO_EXECUTION_ENABLED"
    assert payload["broker_write"] is False


def test_validation_modules_cannot_access_broker_write_or_credentials() -> None:
    for module in (app.validation.engine, app.validation.execution, app.validation.runtime):
        source = inspect.getsource(module)
        assert "app.brokers.etoro" not in source
        assert "submit_demo" not in source
        assert "post_once" not in source
        assert "market-open-orders" not in source
        assert "ETORO_API_KEY" not in source
        assert "ETORO_USER_KEY" not in source


def test_agent_propagates_reduce_and_close_for_existing_long_position() -> None:
    instrument = _instrument("AAA", instrument_id=8001)
    bars = _bars(instrument, count=80, start=Decimal("100"), step=Decimal("0.20"))
    clock = HistoricalReplayClock(bars[-1].timestamp)
    simulated_position = SimulatedPosition(
        instrument_id=8001,
        symbol="AAA",
        asset_class=AssetClass.EQUITY,
        units=Decimal("2"),
        average_entry_price=Decimal("100"),
        market_price=Decimal("116"),
    )
    portfolio = PortfolioSnapshot(
        as_of=bars[-1].timestamp,
        currency=Currency.USD,
        cash=Decimal("800"),
        positions=(
            Position(
                position_id="held-aaa",
                instrument_id=8001,
                symbol="AAA",
                settlement_type=SettlementType.REAL,
                units=simulated_position.units,
                average_entry_price=simulated_position.average_entry_price,
                market_price=simulated_position.market_price,
            ),
        ),
        reported_total_value=Decimal("1032"),
        peak_value=Decimal("1032"),
    )
    candidate = app.validation.engine._candidate_at(
        instrument=instrument,
        bars=bars,
        portfolio=SimulatedPortfolioState(
            as_of=bars[-1].timestamp,
            currency=Currency.USD,
            cash=Decimal("800"),
            positions=(simulated_position,),
            peak_value=Decimal("1032"),
        ),
        clock=clock,
    )
    analysis = AegisOpportunityIntelligenceEngine().analyze_candidate(
        candidate=candidate,
        portfolio=portfolio,
        bars_by_timeframe={TimeFrame.ONE_DAY: bars},
        as_of=bars[-1].timestamp,
    )
    close_analysis = analysis.model_copy(
        update={
            "decision": AegisDecision.REDUCE,
            "ensemble": analysis.ensemble.model_copy(
                update={"direction": StrategyDirection.REDUCE, "strength": Decimal("0.60")}
            ),
        }
    )
    metadata = InstrumentMetadata(
        instrument_id=8001,
        symbol="AAA",
        asset_class=AssetClass.EQUITY,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("5"),
        metadata_as_of=bars[-1].timestamp,
        source="test",
    )
    base_context = {
        "portfolio": portfolio,
        "quotes": (candidate.quote,),
        "news": (),
        "instruments": (metadata,),
        "candidates": (candidate,),
        "analysis_timestamp": bars[-1].timestamp,
        "strategy": ApplicationConfig().strategy,
        "minimum_trade_amount": Decimal("5"),
    }

    close = DeterministicAegisAgent().analyze(
        AegisAgentContext(**base_context, intelligence_reports=(close_analysis,))
    )
    partial = DeterministicAegisAgent().analyze(
        AegisAgentContext(
            **base_context,
            intelligence_reports=(
                close_analysis.model_copy(
                    update={
                        "ensemble": close_analysis.ensemble.model_copy(
                            update={"strength": Decimal("0.30")}
                        )
                    }
                ),
            ),
        )
    )

    assert close.proposal is not None
    assert close.proposal.side is TradeSide.SELL
    assert close.proposal.intent is TradeIntent.CLOSE
    assert close.analysis.recommended_action is RecommendedAction.CLOSE
    assert partial.proposal is not None
    assert partial.proposal.side is TradeSide.SELL
    assert partial.proposal.intent is TradeIntent.REDUCE
    assert partial.proposal.amount < simulated_position.market_value


def test_exit_policy_v2_updates_causal_position_state_without_future_data() -> None:
    state = _position_state(
        current_timestamp=_now() - timedelta(days=2), current_price=Decimal("100")
    )

    updated = state.update_from_observation(
        timestamp=_now() - timedelta(days=1),
        price=Decimal("112"),
        confidence=Decimal("0.70"),
        opportunity_score=Decimal("74"),
        regime=RegimeLabel.UPTREND,
        defensive_signal_active=True,
    ).update_from_observation(
        timestamp=_now(),
        price=Decimal("98"),
        confidence=Decimal("0.65"),
        opportunity_score=Decimal("70"),
        regime=RegimeLabel.TRANSITION,
        defensive_signal_active=True,
    )

    assert updated.bars_held == state.bars_held + 2
    assert updated.mfe == Decimal("0.12")
    assert updated.mae == Decimal("-0.02")
    assert updated.post_entry_peak_price == Decimal("112")
    assert updated.drawdown_from_post_entry_peak > Decimal("0.12")
    assert updated.confidence_deterioration == Decimal("0.15")
    assert updated.opportunity_score_deterioration == Decimal("12")
    assert updated.defensive_signal_persistence == 2
    with pytest.raises(ValueError, match="move forward"):
        updated.update_from_observation(
            timestamp=_now(),
            price=Decimal("99"),
            confidence=Decimal("0.60"),
            opportunity_score=Decimal("65"),
            regime=RegimeLabel.DOWNTREND,
            defensive_signal_active=True,
        )


def test_exit_policy_v2_is_opt_in_and_v1_legacy_remains_default() -> None:
    assert ApplicationConfig().strategy.exit_policy_profile == EXIT_POLICY_V1_LEGACY
    assert isinstance(select_exit_policy(EXIT_POLICY_V1_LEGACY), ExitPolicy)
    assert isinstance(select_exit_policy(EXIT_POLICY_V2_GUARDED), ExitPolicyV2Guarded)
    assert (
        load_config(
            {"AEGIS_EXIT_POLICY_PROFILE": " EXITPOLICY_V2_GUARDED "}
        ).strategy.exit_policy_profile
        == EXIT_POLICY_V2_GUARDED
    )
    with pytest.raises(Exception, match="exit policy profile"):
        load_config({"AEGIS_EXIT_POLICY_PROFILE": "EXITPOLICY_V2_AUTO"})


def test_exit_policy_v2_parameter_governance_inventory_and_protocol_are_frozen() -> None:
    inventory = exit_policy_v2_parameter_inventory()
    names = {item.name for item in inventory}
    protocol = default_exit_policy_v2_validation_protocol()

    assert names == {
        "capital_reduce_drawdown_pct",
        "capital_close_drawdown_pct",
        "thesis_reduce_confidence_drop",
        "thesis_close_confidence_drop",
        "thesis_reduce_score_drop",
        "thesis_close_score_drop",
        "regime_deterioration_reduce_enabled",
        "regime_invalidation_close_enabled",
        "trailing_min_mfe_pct",
        "trailing_reduce_drawdown_pct",
        "trailing_close_drawdown_pct",
        "defensive_persistence_reduce_count",
        "defensive_persistence_close_count",
        "concentration_reduce_weight",
        "stagnation_bars",
        "stagnation_abs_return_pct",
        "cooldown_bars_after_reduce",
        "cooldown_bars_after_close",
    }
    assert all(item.mandatory for item in inventory)
    assert any(
        item.equivalent_existing_value == "RiskPolicy.max_single_position" for item in inventory
    )
    assert protocol.candidate_parameter_grid_declared_before_replay is True
    assert any(partition.research_exposed for partition in protocol.research_exposed_observations)
    assert all(
        partition.end <= datetime(2024, 1, 1, tzinfo=UTC)
        for partition in protocol.proposed_partitions
    )


def test_exit_policy_v2_parameter_bundle_fingerprint_blocks_mutation() -> None:
    params = _exit_v2_params()
    bundle = ExitPolicyV2ParameterBundle.create(
        created_at=_now(),
        creation_rationale="synthetic governance fixture; not selected from replay outcomes",
        parameter_provenance=("predeclared unit-test fixture",),
        asset_class_scope=(AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO),
        validation_status=ExitPolicyV2ValidationStatus.FROZEN_FOR_VALIDATION,
        parameters=params,
    )
    changed_params = params.model_copy(update={"capital_reduce_drawdown_pct": Decimal("0.09")})
    changed_bundle = ExitPolicyV2ParameterBundle.create(
        created_at=_now(),
        creation_rationale="synthetic governance fixture; not selected from replay outcomes",
        parameter_provenance=("predeclared unit-test fixture",),
        asset_class_scope=(AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO),
        validation_status=ExitPolicyV2ValidationStatus.FROZEN_FOR_VALIDATION,
        parameters=changed_params,
    )
    tampered_payload = bundle.model_dump(mode="python")
    tampered_payload["parameters"] = changed_params

    assert bundle.version == EXIT_POLICY_V2_PARAMETER_BUNDLE_VERSION
    assert bundle.frozen is True
    assert bundle.fingerprint == bundle.expected_fingerprint()
    assert changed_bundle.fingerprint != bundle.fingerprint
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        ExitPolicyV2ParameterBundle.model_validate(tampered_payload)
    with pytest.raises(ValueError, match="must be frozen"):
        ExitPolicyV2ParameterBundle.model_validate(
            {**bundle.model_dump(mode="python"), "frozen": False}
        )


def test_exit_policy_v2_experiment_manifest_freezes_partitions_and_registry() -> None:
    registry = default_exit_policy_v2_candidate_registry(created_at=_now())
    manifest = default_exit_policy_v2_experiment_manifest(created_at=_now())
    holdouts = tuple(
        partition for partition in manifest.dataset_partitions if partition.role == "HOLDOUT"
    )
    research_exposed = tuple(partition for partition in manifest.research_exposed_ranges)
    tampered_payload = manifest.model_dump(mode="python")
    tampered_payload["objective_rules"] = ("profit-only invalid mutation",)

    assert registry.frozen is True
    assert len(registry.candidate_bundles) == 3
    assert registry.fingerprint == registry.expected_fingerprint()
    assert manifest.frozen is True
    assert manifest.candidate_registry_fingerprint == registry.fingerprint
    assert manifest.manifest_sha256 == manifest.expected_manifest_sha256()
    assert all(
        holdout.end < research.start for holdout in holdouts for research in research_exposed
    )
    assert any(
        partition.asset_class is AssetClass.CRYPTO
        and partition.role == "TRAIN"
        and set(partition.insufficient_history_symbols) == {"ADA", "AVAX", "DOT", "XRP"}
        for partition in manifest.dataset_partitions
    )
    with pytest.raises(ValueError, match="manifest fingerprint mismatch"):
        ExitPolicyV2ExperimentManifest.model_validate(tampered_payload)
    with pytest.raises(ValueError, match="must be frozen"):
        ExitPolicyV2ExperimentManifest.model_validate(
            {**manifest.model_dump(mode="python"), "frozen": False}
        )


def test_exit_policy_v2_experiment_manifest_v2_freezes_profiles_and_safety() -> None:
    manifest = default_exit_policy_v2_preregistered_experiment_v2_manifest(created_at=_now())
    tampered_payload = manifest.model_dump(mode="python")
    tampered_payload["confidence_profile"] = "V1_LEGACY"

    assert manifest.experiment_version == EXIT_POLICY_V2_EXPERIMENT_V2_VERSION
    assert manifest.confidence_profile == EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE
    assert manifest.exit_policy_profile == EXIT_POLICY_V2_GUARDED
    assert "broker_write_calls must remain 0" in manifest.safety_invariants
    assert manifest.manifest_sha256 == manifest.expected_manifest_sha256()
    with pytest.raises(ValueError, match="V2_B_GUARDED confidence|fingerprint mismatch"):
        ExitPolicyV2ExperimentManifest.model_validate(tampered_payload)


def test_exit_policy_v2_historical_validation_requires_frozen_governance() -> None:
    registry = default_exit_policy_v2_candidate_registry(created_at=_now())
    manifest = default_exit_policy_v2_experiment_manifest(created_at=_now())
    bundle = registry.candidate_bundles[0]

    assert isinstance(
        select_exit_policy_for_historical_validation(EXIT_POLICY_V1_LEGACY), ExitPolicy
    )
    with pytest.raises(ValueError, match="requires a frozen parameter bundle"):
        select_exit_policy_for_historical_validation(EXIT_POLICY_V2_GUARDED)
    with pytest.raises(ValueError, match="requires a frozen candidate registry"):
        select_exit_policy_for_historical_validation(
            EXIT_POLICY_V2_GUARDED,
            v2_parameter_bundle=bundle,
        )
    with pytest.raises(ValueError, match="requires a frozen experiment manifest"):
        select_exit_policy_for_historical_validation(
            EXIT_POLICY_V2_GUARDED,
            v2_parameter_bundle=bundle,
            v2_candidate_registry=registry,
        )
    assert isinstance(
        select_exit_policy_for_historical_validation(
            EXIT_POLICY_V2_GUARDED,
            v2_parameter_bundle=bundle,
            v2_candidate_registry=registry,
            v2_experiment_manifest=manifest,
        ),
        ExitPolicyV2Guarded,
    )
    unregistered = ExitPolicyV2ParameterBundle.create(
        created_at=_now(),
        creation_rationale="unregistered fixture",
        parameter_provenance=("not in manifest",),
        asset_class_scope=(AssetClass.EQUITY,),
        validation_status=ExitPolicyV2ValidationStatus.FROZEN_FOR_VALIDATION,
        parameters=_exit_v2_params(parameter_bundle_id="unregistered"),
    )
    with pytest.raises(ValueError, match="not registered"):
        select_exit_policy_for_historical_validation(
            EXIT_POLICY_V2_GUARDED,
            v2_parameter_bundle=unregistered,
            v2_candidate_registry=registry,
            v2_experiment_manifest=manifest,
        )


def test_exit_policy_v2_train_validation_runner_requires_manifest_confidence_profile() -> None:
    payload = build_exit_policy_v2_train_validation_report(
        load_config({"ETORO_DEMO_EXECUTION_ENABLED": "false"})
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "EXITPOLICY_V2_MANIFEST_CONFIDENCE_PROFILE_MISMATCH"
    assert payload["broker_write_calls"] == 0


def test_frozen_balanced_candidate_runs_synthetic_train_validation() -> None:
    registry = default_exit_policy_v2_candidate_registry(created_at=_now())
    manifest = default_exit_policy_v2_preregistered_experiment_v2_manifest(created_at=_now())
    balanced = next(
        bundle
        for bundle in registry.candidate_bundles
        if bundle.parameters.parameter_bundle_id == "exitpolicy-v2-balanced-guarded-candidate"
    )
    config = ApplicationConfig().model_copy(
        update={
            "strategy": ApplicationConfig().strategy.model_copy(
                update={
                    "confidence_profile": "V2_B_GUARDED",
                    "exit_policy_profile": EXIT_POLICY_V2_GUARDED,
                }
            )
        }
    )

    select_exit_policy_for_historical_validation(
        config.strategy.exit_policy_profile,
        v2_parameter_bundle=balanced,
        v2_candidate_registry=registry,
        v2_experiment_manifest=manifest,
    )
    result = HistoricalValidationEngine(
        risk_policy=config.risk,
        strategy_config=config.strategy,
        agent=DeterministicAegisAgent(exit_policy=ExitPolicyV2Guarded(parameter_bundle=balanced)),
    ).run(dataset=_dataset(), initial_cash=Decimal("200"), replay_stride=5)

    assert result.broker_write_calls == 0
    assert result.lifecycle is not None
    assert result.lifecycle.initial_cash == Decimal("200")
    assert len(result.decisions) > 0


def test_exit_policy_v2_multi_asset_qualification_requires_same_candidate_per_class() -> None:
    def row(asset_class: str, candidate_id: str, *, completed_trades: int = 3) -> dict[str, object]:
        return {
            "asset_class": asset_class,
            "candidate_bundle_id": candidate_id,
            "status": "EXECUTED_TRAIN_VALIDATION_ONLY",
            "completed_trades": completed_trades,
            "close": completed_trades,
            "max_drawdown": "0.10",
            "cooldown_reentry_blocks": 0,
            "data_quality_exclusions": 0,
            "realized_pnl": "1",
        }

    required_classes = ("CRYPTO", "EQUITY", "ETF")
    equity_only = _exit_policy_v2_multi_asset_qualification(
        [row("EQUITY", "balanced"), row("ETF", "balanced", completed_trades=0)],
        required_asset_classes=required_classes,
    )
    assert equity_only["status"] == "CROSS_ASSET_VALIDATION_INSUFFICIENT"
    assert equity_only["asset_classes_without_a_qualifying_candidate"] == (
        "CRYPTO",
        "ETF",
    )

    all_classes = _exit_policy_v2_multi_asset_qualification(
        [row(asset_class, "balanced") for asset_class in required_classes],
        required_asset_classes=required_classes,
    )
    assert all_classes["status"] == "CROSS_ASSET_VALIDATION_QUALIFIED"
    assert all_classes["cross_asset_qualified_candidate_bundle_ids"] == ("balanced",)


def test_exit_policy_v2_requires_frozen_parameter_bundle_before_execution() -> None:
    analysis = _exit_analysis()
    unresolved_analysis = analysis.model_copy(
        update={
            "candidate": analysis.candidate.model_copy(
                update={
                    "instrument": analysis.candidate.instrument.model_copy(
                        update={"broker_instrument_id": "unresolved"}
                    )
                }
            )
        }
    )
    unresolved = ExitPolicyV2Guarded(_exit_v2_params()).evaluate(
        analysis=unresolved_analysis,
        portfolio=_portfolio_snapshot(),
        position_state=_position_state(),
    )
    decision = ExitPolicyV2Guarded().evaluate(
        analysis=analysis,
        portfolio=PortfolioSnapshot(as_of=_now(), currency=Currency.USD, cash=Decimal("1000")),
        position_state=_position_state(),
    )

    assert unresolved.reason_code == ExitPolicyReasonCode.INSUFFICIENT_POSITION_STATE_HOLD.value
    assert decision.action is RecommendedAction.HOLD
    assert decision.reason_code == ExitPolicyReasonCode.INSUFFICIENT_POSITION_STATE_HOLD.value
    decision = ExitPolicyV2Guarded().evaluate(
        analysis=analysis,
        portfolio=_portfolio_snapshot(),
        position_state=_position_state(),
    )
    assert decision.reason_code == ExitPolicyReasonCode.PARAMETER_BUNDLE_REQUIRED.value
    with pytest.raises(ValueError, match="unsupported exit policy profile"):
        select_exit_policy("EXITPOLICY_V2_AUTO")
    with pytest.raises(ValueError, match="capital close threshold"):
        _exit_v2_params(capital_close_drawdown_pct=Decimal("0.04"))
    with pytest.raises(ValueError, match="thesis close confidence"):
        _exit_v2_params(thesis_close_confidence_drop=Decimal("0.05"))
    with pytest.raises(ValueError, match="thesis close score"):
        _exit_v2_params(thesis_close_score_drop=Decimal("5"))
    with pytest.raises(ValueError, match="trailing close drawdown"):
        _exit_v2_params(trailing_close_drawdown_pct=Decimal("0.04"))
    with pytest.raises(ValueError, match="defensive close persistence"):
        _exit_v2_params(defensive_persistence_close_count=1)


def test_exit_policy_v2_branches_and_close_precedence_with_synthetic_thresholds() -> None:
    portfolio = _portfolio_snapshot(market_price=Decimal("100"))
    policy = ExitPolicyV2Guarded(_exit_v2_params())
    analysis = _exit_analysis()

    hold = policy.evaluate(analysis=analysis, portfolio=portfolio, position_state=_position_state())
    reduce = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(current_price=Decimal("91")),
    )
    close = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(current_price=Decimal("80")),
    )
    thesis = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(current_confidence=Decimal("0.60")),
    )
    thesis_close = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(current_opportunity_score=Decimal("50")),
    )
    trailing = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(
            mfe=Decimal("0.20"),
            current_price=Decimal("105"),
            post_entry_peak_price=Decimal("120"),
            drawdown_from_post_entry_peak=Decimal("0.125"),
        ),
    )
    trailing_reduce = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(
            mfe=Decimal("0.20"),
            current_price=Decimal("114"),
            post_entry_peak_price=Decimal("120"),
            drawdown_from_post_entry_peak=Decimal("0.05"),
        ),
    )

    assert hold.reason_code == ExitPolicyReasonCode.NO_EXIT_TRIGGER_HOLD.value
    assert reduce.action is RecommendedAction.REDUCE
    assert reduce.reason_code == ExitPolicyReasonCode.CAPITAL_PROTECTION_REDUCE.value
    assert close.action is RecommendedAction.CLOSE
    assert close.reason_code == ExitPolicyReasonCode.CAPITAL_PROTECTION_CLOSE.value
    assert thesis.action is RecommendedAction.REDUCE
    assert thesis.reason_code == ExitPolicyReasonCode.THESIS_DETERIORATION_REDUCE.value
    assert thesis_close.action is RecommendedAction.CLOSE
    assert thesis_close.reason_code == ExitPolicyReasonCode.THESIS_INVALIDATION_CLOSE.value
    assert trailing.action is RecommendedAction.CLOSE
    assert trailing.reason_code == ExitPolicyReasonCode.TRAILING_PROFIT_CLOSE.value
    assert trailing_reduce.action is RecommendedAction.REDUCE
    assert trailing_reduce.reason_code == ExitPolicyReasonCode.TRAILING_PROFIT_REDUCE.value
    assert close.policy_version == EXIT_POLICY_V2_VERSION


def test_exit_policy_v2_defensive_regime_concentration_stagnation_and_no_short_creation() -> None:
    portfolio = _portfolio_snapshot(market_price=Decimal("100"))
    policy = ExitPolicyV2Guarded(_exit_v2_params())
    analysis = _exit_analysis()

    defensive = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(defensive_signal_persistence=2),
    )
    regime = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(
            entry_regime=RegimeLabel.STRONG_UPTREND,
            current_regime=RegimeLabel.DOWNTREND,
        ),
    )
    regime_reduce = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(current_regime=RegimeLabel.TRANSITION),
    )
    defensive_close = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(defensive_signal_persistence=4),
    )
    concentration = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(position_weight=Decimal("0.35")),
    )
    stagnation = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(bars_held=9, current_price=Decimal("100.50")),
    )
    cooldown = policy.evaluate(
        analysis=analysis,
        portfolio=portfolio,
        position_state=_position_state(cooldown_bars_remaining=1),
    )

    assert defensive.reason_code == ExitPolicyReasonCode.PERSISTENT_DEFENSIVE_SIGNALS_REDUCE.value
    assert regime.reason_code == ExitPolicyReasonCode.REGIME_INVALIDATION_CLOSE.value
    assert regime_reduce.reason_code == ExitPolicyReasonCode.REGIME_DETERIORATION_REDUCE.value
    assert (
        defensive_close.reason_code == ExitPolicyReasonCode.PERSISTENT_DEFENSIVE_SIGNALS_CLOSE.value
    )
    assert concentration.reason_code == ExitPolicyReasonCode.POSITION_CONCENTRATION_REDUCE.value
    assert stagnation.reason_code == ExitPolicyReasonCode.STAGNATION_REDUCE.value
    assert cooldown.reason_code == ExitPolicyReasonCode.COOLDOWN_BLOCKS_REENTRY.value
    assert reduce_amount_not_exceeding_long_position(Decimal("500"), Decimal("100")) == Decimal(
        "100.00"
    )
    assert exit_action_blocks_entry(RecommendedAction.CLOSE) is True
    assert exit_action_blocks_entry(RecommendedAction.REDUCE) is True
    assert exit_action_blocks_entry(RecommendedAction.HOLD) is False


def test_agent_v2_cooldown_blocks_same_symbol_increase() -> None:
    analysis = _exit_analysis().model_copy(update={"decision": AegisDecision.BUY})
    quote = analysis.candidate.quote
    assert quote is not None
    metadata = InstrumentMetadata(
        instrument_id=8001,
        symbol="AAA",
        asset_class=AssetClass.EQUITY,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("5"),
        metadata_as_of=_now(),
        source="test",
    )

    result = DeterministicAegisAgent(exit_policy=ExitPolicyV2Guarded(_exit_v2_params())).analyze(
        AegisAgentContext(
            portfolio=_portfolio_snapshot(),
            quotes=(quote,),
            news=(),
            instruments=(metadata,),
            candidates=(analysis.candidate,),
            intelligence_reports=(analysis,),
            position_states=(_position_state(cooldown_bars_remaining=1),),
            analysis_timestamp=_now(),
            strategy=ApplicationConfig().strategy,
            minimum_trade_amount=Decimal("5"),
        )
    )

    assert result.proposal is None
    assert result.analysis.recommended_action is RecommendedAction.HOLD
    assert "cooldown is active" in result.analysis.rationale


def test_agent_v2_cooldown_blocks_same_symbol_open_after_close() -> None:
    analysis = _exit_analysis().model_copy(update={"decision": AegisDecision.BUY})
    quote = analysis.candidate.quote
    assert quote is not None
    metadata = InstrumentMetadata(
        instrument_id=8001,
        symbol="AAA",
        asset_class=AssetClass.EQUITY,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("5"),
        metadata_as_of=_now(),
        source="test",
    )
    empty_portfolio = PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.USD,
        cash=Decimal("1000"),
        positions=(),
        reported_total_value=Decimal("1000"),
        peak_value=Decimal("1000"),
    )

    result = DeterministicAegisAgent(exit_policy=ExitPolicyV2Guarded(_exit_v2_params())).analyze(
        AegisAgentContext(
            portfolio=empty_portfolio,
            quotes=(quote,),
            news=(),
            instruments=(metadata,),
            candidates=(analysis.candidate,),
            intelligence_reports=(analysis,),
            position_states=(
                _position_state(position_market_value=Decimal("0"), cooldown_bars_remaining=2),
            ),
            analysis_timestamp=_now(),
            strategy=ApplicationConfig().strategy,
            minimum_trade_amount=Decimal("5"),
        )
    )

    assert result.proposal is None
    assert result.analysis.recommended_action is RecommendedAction.HOLD
    assert "cooldown is active" in result.analysis.rationale


def test_agent_v2_exit_precedes_same_timestamp_increase() -> None:
    analysis = _exit_analysis().model_copy(update={"decision": AegisDecision.BUY})
    quote = analysis.candidate.quote
    assert quote is not None
    metadata = InstrumentMetadata(
        instrument_id=8001,
        symbol="AAA",
        asset_class=AssetClass.EQUITY,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("5"),
        metadata_as_of=_now(),
        source="test",
    )

    result = DeterministicAegisAgent(exit_policy=ExitPolicyV2Guarded(_exit_v2_params())).analyze(
        AegisAgentContext(
            portfolio=_portfolio_snapshot(),
            quotes=(quote,),
            news=(),
            instruments=(metadata,),
            candidates=(analysis.candidate,),
            intelligence_reports=(analysis,),
            position_states=(_position_state(current_price=Decimal("80")),),
            analysis_timestamp=_now(),
            strategy=ApplicationConfig().strategy,
            minimum_trade_amount=Decimal("5"),
        )
    )

    assert result.proposal is not None
    assert result.proposal.intent is TradeIntent.CLOSE
    assert result.proposal.side is TradeSide.SELL
    assert result.proposal.amount <= _portfolio_snapshot().market_value_for(8001)


def test_agent_intelligence_path_rejects_non_positive_trade_amount() -> None:
    base_analysis = _exit_analysis().model_copy(update={"decision": AegisDecision.BUY})
    analysis = base_analysis.model_copy(
        update={
            "candidate": base_analysis.candidate.model_copy(
                update={
                    "instrument": base_analysis.candidate.instrument.model_copy(
                        update={"minimum_order_value": None}
                    )
                }
            )
        }
    )
    quote = analysis.candidate.quote
    assert quote is not None
    metadata = InstrumentMetadata(
        instrument_id=8001,
        symbol="AAA",
        asset_class=AssetClass.EQUITY,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=None,
        metadata_as_of=_now(),
        source="test",
    )

    tiny_portfolio = PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.USD,
        cash=Decimal("0.01"),
        positions=(),
        reported_total_value=Decimal("0.01"),
        peak_value=Decimal("0.01"),
    )

    result = DeterministicAegisAgent(exit_policy=ExitPolicyV2Guarded(_exit_v2_params())).analyze(
        AegisAgentContext(
            portfolio=tiny_portfolio,
            quotes=(quote,),
            news=(),
            instruments=(metadata,),
            candidates=(analysis.candidate,),
            intelligence_reports=(analysis,),
            analysis_timestamp=_now(),
            strategy=ApplicationConfig().strategy,
            minimum_trade_amount=None,
        )
    )

    assert result.proposal is None
    assert result.analysis.recommended_action is RecommendedAction.HOLD
    assert "trade size is not positive" in result.analysis.rationale


def test_lifecycle_summary_distinguishes_entries_reductions_and_completed_closes() -> None:
    engine = SimulatedExecutionEngine(TransactionCostModel())
    opened, buy = engine.execute(
        portfolio=_portfolio(),
        proposal=_proposal(),
        quote=_quote(),
        risk=_risk(RiskDecisionStatus.APPROVED),
    )
    reduced, reduce_record = engine.execute(
        portfolio=opened,
        proposal=_proposal(side=TradeSide.SELL, intent=TradeIntent.REDUCE, amount=Decimal("50")),
        quote=_quote(price=Decimal("105")),
        risk=_risk(RiskDecisionStatus.APPROVED),
    )
    closed, close_record = engine.execute(
        portfolio=reduced,
        proposal=_proposal(
            side=TradeSide.SELL,
            intent=TradeIntent.CLOSE,
            amount=reduced.positions[0].market_value,
        ),
        quote=_quote(price=Decimal("110")),
        risk=_risk(RiskDecisionStatus.APPROVED),
    )

    lifecycle = app.validation.engine._lifecycle_summary(
        initial_cash=Decimal("1000"),
        final_portfolio=closed,
        trades=(buy, reduce_record, close_record),
    )
    metrics = PerformanceMetricCalculator().calculate(
        equity_curve=((_now(), Decimal("1000")), (_now() + timedelta(days=1), closed.total_value)),
        trades=(buy, reduce_record, close_record),
    )

    assert lifecycle.entry_count == 1
    assert lifecycle.reduction_count == 1
    assert lifecycle.close_count == 1
    assert lifecycle.completed_trade_count == 1
    assert lifecycle.open_position_count == 0
    assert lifecycle.realized_pnl > 0
    assert metrics.trade_count == 2


def test_open_positions_are_not_completed_trade_statistics() -> None:
    engine = SimulatedExecutionEngine(TransactionCostModel())
    opened, buy = engine.execute(
        portfolio=_portfolio(),
        proposal=_proposal(),
        quote=_quote(),
        risk=_risk(RiskDecisionStatus.APPROVED),
    )

    lifecycle = app.validation.engine._lifecycle_summary(
        initial_cash=Decimal("1000"),
        final_portfolio=opened,
        trades=(buy,),
    )
    metrics = PerformanceMetricCalculator().calculate(
        equity_curve=((_now(), Decimal("1000")), (_now() + timedelta(days=1), opened.total_value)),
        trades=(buy,),
    )

    assert lifecycle.entry_count == 1
    assert lifecycle.completed_trade_count == 0
    assert lifecycle.open_position_count == 1
    assert metrics.trade_count == 0
    assert metrics.expectancy == NOT_ENOUGH_DATA


class EntryRankingExcludesHeldAfterFirstPass:
    def __init__(self) -> None:
        self.calls = 0

    def rank(self, candidates: tuple[Any, ...], *, top_n: int) -> tuple[Any, ...]:
        self.calls += 1
        if self.calls == 1:
            return tuple(
                candidate for candidate in candidates if candidate.instrument.symbol == "AAA"
            )
        return tuple(candidate for candidate in candidates if candidate.instrument.symbol != "AAA")[
            :top_n
        ]


class OpenThenCloseHeldAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, Decimal]] = []

    def analyze(self, context: AegisAgentContext) -> AegisAgentResult:
        quote = context.quotes[0]
        current_value = context.portfolio.market_value_for(quote.instrument_id)
        self.calls.append((quote.symbol, context.analysis_timestamp, current_value))
        if quote.symbol == "AAA" and current_value > 0:
            action = RecommendedAction.CLOSE
            proposal = _dynamic_proposal(
                context,
                side=TradeSide.SELL,
                intent=TradeIntent.CLOSE,
                amount=current_value,
            )
        elif quote.symbol == "AAA":
            action = RecommendedAction.OPEN
            proposal = _dynamic_proposal(
                context,
                side=TradeSide.BUY,
                intent=TradeIntent.OPEN,
                amount=Decimal("50"),
            )
        else:
            action = RecommendedAction.HOLD
            proposal = None
        return AegisAgentResult(
            analysis=AegisAnalysis(
                timestamp=context.analysis_timestamp,
                market_assessment="test",
                portfolio_assessment="test",
                opportunity_summary="test",
                risk_summary="test",
                confidence=Decimal("0.90"),
                supporting_factors=("test",),
                risk_factors=("test",),
                recommended_action=action,
                rationale="test lifecycle wiring",
                symbol=quote.symbol if action is not RecommendedAction.HOLD else None,
            ),
            proposal=proposal,
        )


def test_open_position_is_evaluated_even_when_absent_from_entry_ranking() -> None:
    instruments = (
        _instrument("AAA", instrument_id=8001),
        _instrument("BBB", instrument_id=8002),
        _instrument("CCC", instrument_id=8003),
        _instrument("DDD", instrument_id=8004),
    )
    bars_by_instrument = {
        instrument.key: _bars(instrument, count=90, start=Decimal("100"), step=Decimal("0.30"))
        for instrument in instruments
    }
    dataset = HistoricalValidationDataset(
        metadata=build_dataset_metadata(
            provider="fixture",
            instruments=instruments,
            bars_by_instrument=bars_by_instrument,
            timeframes=(TimeFrame.ONE_DAY,),
            created_at=_now(),
            mapping_version=HISTORICAL_DATA_VERSION,
        ),
        bars_by_instrument=bars_by_instrument,
    )
    agent = OpenThenCloseHeldAgent()
    result = HistoricalValidationEngine(
        agent=cast(Any, agent),
        ranking_engine=cast(Any, EntryRankingExcludesHeldAfterFirstPass()),
    ).run(dataset=dataset, replay_stride=10)

    aaa_calls = [call for call in agent.calls if call[0] == "AAA"]
    assert result.lifecycle is not None
    assert len(aaa_calls) == 2
    assert aaa_calls[0][2] == Decimal("0")
    assert aaa_calls[1][2] > 0
    assert result.lifecycle.entry_count == 1
    assert result.lifecycle.close_count == 1
    assert result.lifecycle.completed_trade_count == 1
    assert result.lifecycle.open_position_count == 0
    assert result.broker_write_calls == 0
    assert any(
        "evaluation_path:POSITION_MANAGEMENT_PATH" in decision.blocker_reasons
        for decision in result.decisions
        if decision.symbol == "AAA"
    )


def test_position_management_path_does_not_duplicate_same_position_bar() -> None:
    agent = OpenThenCloseHeldAgent()
    result = HistoricalValidationEngine(
        agent=cast(Any, agent),
        ranking_engine=cast(Any, EntryRankingExcludesHeldAfterFirstPass()),
    ).run(dataset=_dataset(), replay_stride=10)
    per_symbol_timestamp = Counter(
        (decision.symbol, decision.timestamp)
        for decision in result.decisions
        if "evaluation_path:POSITION_MANAGEMENT_PATH" in decision.blocker_reasons
    )

    assert all(count == 1 for count in per_symbol_timestamp.values())
    assert result.broker_write_calls == 0


def test_historical_daily_freshness_differs_by_asset_class_and_live_rule_remains() -> None:
    policy = HistoricalReplayFreshnessPolicy()
    friday = datetime(2026, 8, 28, 21, 0, tzinfo=UTC)
    monday = friday + timedelta(days=3)

    assert (
        policy.risk_reference_time(
            asset_class=AssetClass.EQUITY,
            timeframe=TimeFrame.ONE_DAY,
            price_timestamp=friday,
            replay_timestamp=monday,
        )
        == friday
    )
    assert (
        policy.risk_reference_time(
            asset_class=AssetClass.CRYPTO,
            timeframe=TimeFrame.ONE_DAY,
            price_timestamp=friday,
            replay_timestamp=monday,
        )
        == monday
    )

    stale_quote = _quote().model_copy(update={"as_of": _now() - timedelta(seconds=301)})
    risk_manager = RiskManager(ApplicationConfig().risk, KillSwitch(active=False))
    stale = risk_manager.evaluate(
        _proposal(),
        app.validation.engine._risk_context(
            portfolio=_portfolio(),
            proposal=_proposal(),
            quote=stale_quote,
            instrument=_instrument(),
            at=_now(),
            timeframe=TimeFrame.ONE_HOUR,
        ),
    )

    assert stale.decision.status is RiskDecisionStatus.REJECTED
    assert RiskViolationCode.STALE_PRICE in {item.code for item in stale.decision.violations}


def test_historical_validation_exposes_lifecycle_equity_curve_and_zero_broker_writes() -> None:
    result = HistoricalValidationEngine().run(dataset=_dataset(), replay_stride=10)

    assert result.lifecycle is not None
    assert result.final_portfolio is not None
    assert result.equity_curve_detail
    assert result.lifecycle.execution_count == sum(
        1 for trade in result.trades if trade.status is ReplayDecisionStatus.SIMULATED_EXECUTED
    )
    assert result.metrics.trade_count == sum(
        1 for trade in result.trades if trade.realized_pnl != 0
    )
    assert result.broker_write_calls == 0
    assert not result.real_execution_available


def test_eur200_research_baseline_is_frozen_and_reconciled() -> None:
    baseline = Eur200ResearchBaseline.create()

    assert baseline.version == EUR200_RESEARCH_BASELINE_VERSION
    assert baseline.initial_equity == Decimal("200")
    assert baseline.final_research_equity == Decimal("224.5301270773")
    assert baseline.classification == "RESEARCH_EXPOSED"
    assert baseline.accounting_status == "RECONCILED"
    assert baseline.frozen is True
    assert baseline.fingerprint == baseline.expected_fingerprint()

    tampered = baseline.model_dump(mode="python")
    tampered["final_research_equity"] = Decimal("225")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        Eur200ResearchBaseline.model_validate(tampered)


def test_prospective_shadow_readiness_persists_independent_sanitized_account(
    tmp_path: Path,
) -> None:
    store = default_prospective_shadow_store(tmp_path / "prospective-shadow.sqlite3")
    config = load_config(
        {
            "AEGIS_CONFIDENCE_PROFILE": "V2_B_GUARDED",
            "AEGIS_EXIT_POLICY_PROFILE": "EXITPOLICY_V2_GUARDED",
            "ETORO_DEMO_EXECUTION_ENABLED": "false",
        }
    )

    report = build_prospective_shadow_validation_readiness_report(
        config,
        store=store,
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert report["status"] == "PROSPECTIVE_SHADOW_VALIDATION_READY"
    assert report["broker_write_calls"] == 0
    assert report["demo_execution_enabled"] is False
    assert report["real_execution_available"] is False
    assert report["prospective_account"] == {
        "starting_equity": "200",
        "positions_carried_from_research": 0,
        "realized_pnl_carried_from_research": "0",
        "unrealized_pnl_carried_from_research": "0",
    }
    manifest = cast(dict[str, Any], report["prospective_manifest"])
    assert manifest["version"] == PROSPECTIVE_SHADOW_MANIFEST_VERSION
    assert manifest["confidence_profile"] == "V2_B_GUARDED"
    assert manifest["exit_policy_profile"] == EXIT_POLICY_V2_GUARDED
    assert manifest["frozen_candidate_id"] == "exitpolicy-v2-balanced-guarded-candidate"
    assert len(store.baselines()) == 1
    assert len(store.manifests()) == 1
    assert len(store.decisions()) == 1
    serialized = json.dumps(report, sort_keys=True)
    assert "api_key" not in serialized.lower()
    assert "user_key" not in serialized.lower()
    assert "authorization" not in serialized.lower()


def test_prospective_shadow_readiness_fails_closed_without_explicit_v2_profiles(
    tmp_path: Path,
) -> None:
    store = default_prospective_shadow_store(tmp_path / "prospective-shadow.sqlite3")
    report = build_prospective_shadow_validation_readiness_report(
        ApplicationConfig(),
        store=store,
        clock=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert report["status"] == "BLOCKED"
    assert report["category"] == "PROSPECTIVE_SHADOW_CONFIDENCE_PROFILE_MISMATCH"
    assert report["broker_write_calls"] == 0
    assert len(store.baselines()) == 0
    assert len(store.manifests()) == 0
    assert len(store.decisions()) == 0


def test_prospective_shadow_store_rejects_secret_shaped_payload(tmp_path: Path) -> None:
    store = default_prospective_shadow_store(tmp_path / "prospective-shadow.sqlite3")

    with pytest.raises(SecretPersistenceError):
        store.record_decision({"event_type": "bad", "api_key": "redacted"})
