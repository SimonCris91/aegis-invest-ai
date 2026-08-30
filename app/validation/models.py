"""Typed research models for historical validation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass, Currency, RiskDecisionStatus, TradeIntent, TradeSide
from app.intelligence.models import (
    AegisDecision,
    MarketBar,
    RegimeLabel,
    ScoreBand,
    StrategyDirection,
    TimeFrame,
)


class PeriodSplitName(StrEnum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"


class SlippageScenario(StrEnum):
    IDEAL = "IDEAL"
    BASE = "BASE"
    STRESSED = "STRESSED"
    SEVERE = "SEVERE"


class ReplayDecisionStatus(StrEnum):
    HOLD = "HOLD"
    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    SIMULATED_EXECUTED = "SIMULATED_EXECUTED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


class StrategyQualificationStatus(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PROMISING = "PROMISING"
    SHADOW_ELIGIBLE = "SHADOW_ELIGIBLE"
    DEMO_ELIGIBLE = "DEMO_ELIGIBLE"
    REJECTED = "REJECTED"


class OverfitRisk(StrEnum):
    OVERFIT_RISK_LOW = "OVERFIT_RISK_LOW"
    OVERFIT_RISK_MEDIUM = "OVERFIT_RISK_MEDIUM"
    OVERFIT_RISK_HIGH = "OVERFIT_RISK_HIGH"


class ParameterStability(StrEnum):
    STABLE = "STABLE"
    MODERATE = "MODERATE"
    PARAMETER_FRAGILE = "PARAMETER_FRAGILE"


class ResearchDatasetMetadata(FrozenDomainModel):
    dataset_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    instruments: tuple[str, ...]
    asset_classes: tuple[AssetClass, ...]
    timeframes: tuple[TimeFrame, ...]
    start: datetime
    end: datetime
    currency: Currency
    data_quality: str = Field(min_length=1)
    mapping_version: str = Field(min_length=1)
    data_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("start", "end", "created_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "dataset metadata timestamp")

    @model_validator(mode="after")
    def end_follows_start(self) -> ResearchDatasetMetadata:
        if self.end <= self.start:
            raise ValueError("dataset end must follow start")
        if not self.instruments or not self.asset_classes or not self.timeframes:
            raise ValueError("dataset metadata requires instruments, asset classes, and timeframes")
        return self


class EvidenceRequirements(FrozenDomainModel):
    minimum_bars_per_instrument: int = Field(default=60, ge=2)
    minimum_historical_span_days: int = Field(default=60, ge=1)
    minimum_replay_decisions: int = Field(default=30, ge=1)
    minimum_simulated_trades: int = Field(default=10, ge=0)
    minimum_oos_trades: int = Field(default=2, ge=0)
    minimum_walk_forward_windows: int = Field(default=3, ge=0)
    requirements_version: str = Field(default="validation-evidence-requirements-v1", min_length=1)


class HistoricalValidationDataset(FrozenDomainModel):
    metadata: ResearchDatasetMetadata
    bars_by_instrument: dict[str, tuple[MarketBar, ...]]


class InstrumentTimeframeCoverage(FrozenDomainModel):
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    timeframe: TimeFrame
    provider: str = Field(min_length=1)
    bar_count: int = Field(ge=0)
    start: datetime | None = None
    end: datetime | None = None
    span_days: int = Field(default=0, ge=0)
    status: str = Field(min_length=1)
    data_quality: str = Field(min_length=1)
    cached: bool = False
    reasons: tuple[str, ...] = ()

    @field_validator("start", "end")
    @classmethod
    def optional_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "coverage timestamp")


class HistoricalValidationDatasetBuild(FrozenDomainModel):
    provider: str = Field(min_length=1)
    universe_rule: str = Field(min_length=1)
    requested_timeframes: tuple[TimeFrame, ...]
    selected_instruments: tuple[str, ...]
    coverage: tuple[InstrumentTimeframeCoverage, ...]
    datasets_by_timeframe: dict[TimeFrame, HistoricalValidationDataset]
    rejected_symbols: tuple[str, ...] = ()
    broker_write: bool = False
    broker_write_calls: int = Field(default=0, ge=0, le=0)
    real_execution_available: bool = False


class PeriodSplit(FrozenDomainModel):
    name: PeriodSplitName
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "period split timestamp")

    @model_validator(mode="after")
    def end_follows_start(self) -> PeriodSplit:
        if self.end <= self.start:
            raise ValueError("period split end must follow start")
        return self


class WalkForwardConfig(FrozenDomainModel):
    training_length: int = Field(default=40, ge=2)
    validation_length: int = Field(default=10, ge=1)
    step_size: int = Field(default=10, ge=1)
    minimum_observations: int = Field(default=30, ge=2)


class WalkForwardWindow(FrozenDomainModel):
    window_index: int = Field(ge=1)
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime

    @field_validator("train_start", "train_end", "validation_start", "validation_end")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "walk-forward window timestamp")


class TransactionCostAssumptions(FrozenDomainModel):
    scenario: SlippageScenario = SlippageScenario.BASE
    spread_assumption: Decimal = Field(default=Decimal("0.002"), ge=0, le=Decimal("0.20"))
    slippage_rate: Decimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.20"))
    broker_fee_rate: Decimal = Field(default=Decimal("0.0005"), ge=0, le=Decimal("0.20"))
    fx_conversion_rate: Decimal | None = Field(default=None, ge=0, le=Decimal("0.20"))
    overnight_rate: Decimal | None = Field(default=None, ge=0, le=Decimal("0.20"))
    provenance: str = Field(default="explicit conservative research assumptions", min_length=1)
    cost_version: str = Field(default="transaction-cost-model-v1", min_length=1)


class TransactionCostEstimate(FrozenDomainModel):
    gross_value: Decimal = Field(ge=0)
    spread_cost: Decimal = Field(ge=0)
    slippage_cost: Decimal = Field(ge=0)
    broker_fee: Decimal = Field(ge=0)
    fx_cost: Decimal = Field(ge=0)
    overnight_cost: Decimal = Field(ge=0)
    total_cost: Decimal = Field(ge=0)
    complete: bool
    unknown_material_costs: tuple[str, ...] = ()
    provenance: str = Field(min_length=1)
    scenario: SlippageScenario


class SimulatedPosition(FrozenDomainModel):
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    units: Decimal = Field(ge=0)
    average_entry_price: Decimal = Field(gt=0)
    market_price: Decimal = Field(gt=0)

    @property
    def market_value(self) -> Decimal:
        return self.units * self.market_price

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.market_price - self.average_entry_price) * self.units


class SimulatedPortfolioState(FrozenDomainModel):
    as_of: datetime
    currency: Currency
    cash: Decimal = Field(ge=0)
    positions: tuple[SimulatedPosition, ...] = ()
    realized_pnl: Decimal = Decimal("0")
    peak_value: Decimal = Field(gt=0)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "simulated portfolio timestamp")

    @property
    def positions_value(self) -> Decimal:
        return sum((position.market_value for position in self.positions), Decimal("0"))

    @property
    def total_value(self) -> Decimal:
        return self.cash + self.positions_value

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((position.unrealized_pnl for position in self.positions), Decimal("0"))


class SimulatedTradeRecord(FrozenDomainModel):
    timestamp: datetime
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    side: TradeSide
    action: TradeIntent
    quantity: Decimal = Field(ge=0)
    gross_value: Decimal = Field(ge=0)
    costs: TransactionCostEstimate
    realized_pnl: Decimal = Decimal("0")
    status: ReplayDecisionStatus
    proposal_id: str | None = Field(default=None, min_length=1)
    risk_status: RiskDecisionStatus | None = None
    mfe: Decimal = Decimal("0")
    mae: Decimal = Decimal("0")

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "simulated trade timestamp")


class ReplayDecision(FrozenDomainModel):
    timestamp: datetime
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    score: Decimal = Field(ge=0, le=100)
    score_band: ScoreBand
    confidence: Decimal = Field(ge=0, le=1)
    regime: RegimeLabel
    aegis_decision: AegisDecision
    status: ReplayDecisionStatus
    ensemble_direction: StrategyDirection | None = None
    strategy_directions: tuple[StrategyDirection, ...] = ()
    strategy_signal_counts: dict[str, int] = Field(default_factory=dict)
    forward_return: Decimal | None = None
    proposal_id: str | None = Field(default=None, min_length=1)
    proposal_side: TradeSide | None = None
    proposal_intent: TradeIntent | None = None
    risk_reasons: tuple[str, ...] = ()
    blocker_reasons: tuple[str, ...] = ()
    proposal_gate_trace: tuple[dict[str, object], ...] = ()
    confidence_decomposition: dict[str, object] = Field(default_factory=dict)
    future_records_ignored: int = Field(default=0, ge=0)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "replay decision timestamp")


class PerformanceSummary(FrozenDomainModel):
    total_return: Decimal | str
    annualized_return: Decimal | str
    win_rate: Decimal | str
    loss_rate: Decimal | str
    profit_factor: Decimal | str
    expectancy: Decimal | str
    average_win: Decimal | str
    average_loss: Decimal | str
    payoff_ratio: Decimal | str
    volatility: Decimal | str
    sharpe_ratio: Decimal | str
    sortino_ratio: Decimal | str
    maximum_drawdown: Decimal | str
    calmar_ratio: Decimal | str
    recovery_factor: Decimal | str
    mfe: Decimal | str
    mae: Decimal | str
    turnover: Decimal | str
    exposure: Decimal | str
    trade_count: int = Field(ge=0)
    average_holding_period: Decimal | str


class ReplayEquityPoint(FrozenDomainModel):
    timestamp: datetime
    cash: Decimal
    position_market_value: Decimal = Field(ge=0)
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_equity: Decimal = Field(ge=0)
    drawdown: Decimal = Field(ge=0)
    gross_exposure: Decimal = Field(ge=0)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "equity curve timestamp")


class TradeLifecycleSummary(FrozenDomainModel):
    initial_cash: Decimal = Field(gt=0)
    ending_cash: Decimal = Field(ge=0)
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    ending_equity: Decimal = Field(ge=0)
    realized_return: Decimal | str
    mark_to_market_return: Decimal | str
    total_equity_return: Decimal | str
    entry_count: int = Field(ge=0)
    increase_count: int = Field(ge=0)
    reduction_count: int = Field(ge=0)
    close_count: int = Field(ge=0)
    execution_count: int = Field(ge=0)
    completed_trade_count: int = Field(ge=0)
    open_position_count: int = Field(ge=0)
    open_position_symbols: tuple[str, ...] = ()
    lifecycle_version: str = Field(default="trade-lifecycle-v1", min_length=1)


class CalibrationBucket(FrozenDomainModel):
    label: str = Field(min_length=1)
    lower: Decimal
    upper: Decimal
    sample_size: int = Field(ge=0)
    mean_return: Decimal | str
    median_return: Decimal | str
    win_rate: Decimal | str
    maximum_drawdown: Decimal | str
    mfe: Decimal | str
    mae: Decimal | str


class DecisionFunnel(FrozenDomainModel):
    market_observations: int = Field(ge=0)
    candidates_analyzed: int = Field(ge=0)
    buy_signals: int = Field(ge=0)
    watch_signals: int = Field(ge=0)
    hold_signals: int = Field(ge=0)
    avoid_signals: int = Field(ge=0)
    reduce_signals: int = Field(ge=0)
    final_buy_decisions: int = Field(default=0, ge=0)
    final_hold_decisions: int = Field(default=0, ge=0)
    final_reduce_decisions: int = Field(default=0, ge=0)
    final_ignore_decisions: int = Field(default=0, ge=0)
    trade_proposals: int = Field(ge=0)
    risk_rejected: int = Field(ge=0)
    simulated_executed: int = Field(ge=0)
    exited_trades: int = Field(ge=0)


class ZeroTradeDiagnostic(FrozenDomainModel):
    blocker: str = Field(min_length=1)
    count: int = Field(ge=0)
    share: Decimal = Field(ge=0, le=1)
    candidate_incidence_count: int = Field(default=0, ge=0)
    candidate_incidence_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    share_of_all_blocker_events: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    root_gate_failure: bool = False


class ThresholdSensitivityPoint(FrozenDomainModel):
    score_delta: Decimal
    confidence_delta: Decimal
    diagnostic_candidates: int = Field(ge=0)
    trade_proposals: int = Field(ge=0)
    simulated_executed: int = Field(ge=0)
    note: str = Field(default="research-only; production defaults unchanged", min_length=1)


class ResearchMatrixRow(FrozenDomainModel):
    strategy: str = Field(min_length=1)
    asset_class: AssetClass
    timeframe: TimeFrame
    regime: RegimeLabel | None = None
    sample_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    oos_expectancy: Decimal | str
    profit_factor: Decimal | str
    sharpe_ratio: Decimal | str
    maximum_drawdown: Decimal | str
    stress_result: str = Field(min_length=1)
    overfit_risk: OverfitRisk
    qualification: StrategyQualificationStatus
    metric_provenance: str = Field(default="trade_performance", min_length=1)


class StressScenarioResult(FrozenDomainModel):
    scenario: str = Field(min_length=1)
    metrics: PerformanceSummary
    passed: bool
    reasons: tuple[str, ...] = ()


class MonteCarloResult(FrozenDomainModel):
    seed: int
    iterations: int = Field(ge=1)
    sample_provenance: str = Field(default="trade_returns", min_length=1)
    median_return: Decimal | str
    worst_drawdown: Decimal | str
    longest_loss_streak: int = Field(ge=0)
    risk_of_large_decline: Decimal | str


class QualificationEvidence(FrozenDomainModel):
    minimum_trade_count_passed: bool
    oos_available: bool
    positive_expectancy: bool
    profit_factor_passed: bool
    drawdown_passed: bool
    walk_forward_passed: bool
    score_calibration_passed: bool
    parameter_stability: ParameterStability
    overfit_risk: OverfitRisk
    stressed_cost_passed: bool
    reasons: tuple[str, ...] = ()


class StrategyQualification(FrozenDomainModel):
    status: StrategyQualificationStatus
    evidence: QualificationEvidence
    shadow_feed_eligible: bool
    demo_consideration_allowed: bool


class StrategyValidationResult(FrozenDomainModel):
    run_id: str = Field(min_length=1)
    dataset: ResearchDatasetMetadata
    period_splits: tuple[PeriodSplit, ...]
    walk_forward_windows: tuple[WalkForwardWindow, ...]
    decisions: tuple[ReplayDecision, ...]
    trades: tuple[SimulatedTradeRecord, ...]
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    equity_curve_detail: tuple[ReplayEquityPoint, ...] = ()
    final_portfolio: SimulatedPortfolioState | None = None
    lifecycle: TradeLifecycleSummary | None = None
    metrics: PerformanceSummary
    benchmark_metrics: dict[str, PerformanceSummary]
    score_calibration: tuple[CalibrationBucket, ...]
    confidence_calibration: tuple[CalibrationBucket, ...]
    strategy_performance: dict[str, PerformanceSummary]
    asset_class_performance: dict[str, PerformanceSummary]
    regime_performance: dict[str, PerformanceSummary]
    decision_funnel: DecisionFunnel | None = None
    zero_trade_diagnostics: tuple[ZeroTradeDiagnostic, ...] = ()
    threshold_sensitivity: tuple[ThresholdSensitivityPoint, ...] = ()
    research_matrix: tuple[ResearchMatrixRow, ...] = ()
    evidence_requirements: EvidenceRequirements | None = None
    evidence_passed: bool = False
    evidence_failures: tuple[str, ...] = ()
    stress_results: tuple[StressScenarioResult, ...]
    parameter_stability: ParameterStability
    overfit_risk: OverfitRisk
    monte_carlo: MonteCarloResult
    qualification: StrategyQualification
    broker_write: bool = False
    broker_write_calls: int = Field(default=0, ge=0, le=0)
    real_execution_available: bool = False
