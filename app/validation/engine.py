"""Historical validation engine that reuses production Aegis intelligence logic."""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from app.agent.context import AegisAgentContext
from app.agent.exit_policy import ExitPolicyV2Guarded, PositionManagementState
from app.agent.service import DeterministicAegisAgent
from app.config.models import AegisStrategyConfig, RiskPolicyConfig
from app.domain.enums import (
    AssetClass,
    MarketStatus,
    RecommendedAction,
    RiskDecisionStatus,
    RiskViolationCode,
    SettlementType,
    TradeIntent,
)
from app.domain.market import InstrumentMetadata, MarketQuote
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext, RiskEvaluation
from app.domain.universe import (
    BrokerEligibilitySnapshot,
    CandidateState,
    DataQualityStatus,
    OpportunityCandidate,
    OpportunityFeatures,
    UniversalInstrument,
)
from app.domain.versions import (
    RANKING_VERSION,
    SCANNER_VERSION,
    STRATEGY_VERSION,
    VALIDATION_ENGINE_VERSION,
)
from app.intelligence.confidence import (
    CONFIDENCE_MODEL_V1,
    CONFIDENCE_MODEL_V2_B,
    CONFIDENCE_SEMANTICS_V1,
    CONFIDENCE_SEMANTICS_V2,
    V2_B_THRESHOLD,
    V2_B_THRESHOLD_PROVENANCE,
)
from app.intelligence.models import (
    AegisOpportunityAnalysis,
    FeatureQuality,
    MarketBar,
    StrategyDirection,
    TimeFrame,
)
from app.intelligence.profiles import profile_for
from app.intelligence.service import AegisOpportunityIntelligenceEngine
from app.policies.defaults import default_asset_policy_engine
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.scanner.ranking import OpportunityRankingEngine
from app.validation.calibration import CalibrationAnalyzer
from app.validation.costs import TransactionCostModel
from app.validation.execution import SimulatedExecutionEngine, SimulatedExecutionError
from app.validation.freshness import HistoricalReplayFreshnessPolicy
from app.validation.metrics import PerformanceMetricCalculator
from app.validation.models import (
    HistoricalValidationDataset,
    ReplayDecision,
    ReplayDecisionStatus,
    ReplayEquityPoint,
    SimulatedPortfolioState,
    SimulatedTradeRecord,
    SlippageScenario,
    StrategyValidationResult,
    StressScenarioResult,
    TradeLifecycleSummary,
    TransactionCostAssumptions,
    WalkForwardConfig,
)
from app.validation.qualification import (
    MonteCarloTradeSequence,
    OverfitRiskAnalyzer,
    ParameterStabilityAnalyzer,
    StrategyQualificationEngine,
)
from app.validation.replay import HistoricalReplayClock, WalkForwardSplitter, split_periods


class HistoricalValidationEngine:
    def __init__(
        self,
        *,
        intelligence_engine: AegisOpportunityIntelligenceEngine | None = None,
        agent: DeterministicAegisAgent | None = None,
        ranking_engine: OpportunityRankingEngine | None = None,
        metric_calculator: PerformanceMetricCalculator | None = None,
        risk_policy: RiskPolicyConfig | None = None,
        strategy_config: AegisStrategyConfig | None = None,
    ) -> None:
        self._risk_policy = risk_policy or RiskPolicyConfig()
        self._strategy_config = strategy_config or AegisStrategyConfig()
        self._intelligence = intelligence_engine or AegisOpportunityIntelligenceEngine(
            confidence_profile=self._strategy_config.confidence_profile
        )
        self._agent = agent or DeterministicAegisAgent()
        self._ranking = ranking_engine or OpportunityRankingEngine(ranking_version=RANKING_VERSION)
        self._metrics = metric_calculator or PerformanceMetricCalculator()

    def run(
        self,
        *,
        dataset: HistoricalValidationDataset,
        initial_cash: Decimal = Decimal("1000"),
        walk_forward: WalkForwardConfig | None = None,
        replay_stride: int = 5,
        cost_assumptions: TransactionCostAssumptions | None = None,
        random_seed: int = 80,
    ) -> StrategyValidationResult:
        bars_by_key = {
            key: tuple(MarketBar.model_validate(bar) for bar in bars)
            for key, bars in dataset.bars_by_instrument.items()
        }
        timestamps = _all_timestamps(bars_by_key)
        splits = split_periods(timestamps)
        windows = WalkForwardSplitter().windows(
            timestamps,
            walk_forward or WalkForwardConfig(minimum_observations=min(30, len(timestamps))),
        )
        instruments = _instruments_from_bars(bars_by_key)
        portfolio = SimulatedPortfolioState(
            as_of=timestamps[0],
            currency=dataset.metadata.currency,
            cash=initial_cash,
            positions=(),
            peak_value=initial_cash,
        )
        executor = SimulatedExecutionEngine(TransactionCostModel(cost_assumptions))
        replay_clock = HistoricalReplayClock(timestamps[0])
        decisions: list[ReplayDecision] = []
        trades: list[SimulatedTradeRecord] = []
        position_states: dict[int, PositionManagementState] = {}
        equity_curve: list[tuple[datetime, Decimal]] = [(timestamps[0], initial_cash)]
        equity_curve_detail: list[ReplayEquityPoint] = [_equity_point(portfolio)]
        risk_manager = RiskManager(
            self._risk_policy,
            KillSwitch(
                active=False,
                reason="historical validation",
                clock=lambda: replay_clock.now,
            ),
            authorization_key=b"validation-risk-manager-key-32b!",
            clock=lambda: replay_clock.now,
        )

        for index, timestamp in enumerate(timestamps):
            replay_clock.move_to(timestamp)
            latest_quotes = _latest_quotes(instruments, bars_by_key, replay_clock)
            portfolio = executor.mark_to_market(portfolio, latest_quotes)
            open_position_ids_at_start = {
                position.instrument_id for position in portfolio.positions
            }
            if index < 30 or index % replay_stride != 0:
                equity_curve.append((timestamp, portfolio.total_value))
                equity_curve_detail.append(_equity_point(portfolio))
                continue
            candidates = tuple(
                _candidate_at(
                    instrument=instrument,
                    bars=bars_by_key[instrument.key],
                    portfolio=portfolio,
                    clock=replay_clock,
                )
                for instrument in instruments
                if len(replay_clock.visible_bars(bars_by_key[instrument.key])) >= 30
            )
            ranked = self._ranking.rank(candidates, top_n=min(len(candidates), 3))
            evaluated_position_ids: set[int] = set()
            for candidate in ranked:
                instrument_id = candidate.instrument.numeric_instrument_id
                if instrument_id in open_position_ids_at_start:
                    evaluated_position_ids.add(instrument_id)
                portfolio = _evaluate_replay_candidate(
                    candidate=candidate,
                    bars=bars_by_key[candidate.instrument.key],
                    portfolio=portfolio,
                    timestamp=timestamp,
                    replay_clock=replay_clock,
                    intelligence=self._intelligence,
                    agent=self._agent,
                    risk_manager=risk_manager,
                    risk_policy=self._risk_policy,
                    executor=executor,
                    strategy_config=self._strategy_config,
                    timeframe=dataset.metadata.timeframes[0],
                    decisions=decisions,
                    trades=trades,
                    position_states=position_states,
                    evaluation_path=(
                        "POSITION_MANAGEMENT_PATH_RANKED"
                        if instrument_id in open_position_ids_at_start
                        else "ENTRY_PATH"
                    ),
                )
            for position_id in sorted(open_position_ids_at_start - evaluated_position_ids):
                instrument = _instrument_by_id(instruments, position_id)
                if instrument is None:
                    continue
                visible_bars = replay_clock.visible_bars(bars_by_key[instrument.key])
                if len(visible_bars) < 30 or visible_bars[-1].timestamp != timestamp:
                    continue
                candidate = _candidate_at(
                    instrument=instrument,
                    bars=bars_by_key[instrument.key],
                    portfolio=portfolio,
                    clock=replay_clock,
                )
                portfolio = _evaluate_replay_candidate(
                    candidate=candidate,
                    bars=bars_by_key[instrument.key],
                    portfolio=portfolio,
                    timestamp=timestamp,
                    replay_clock=replay_clock,
                    intelligence=self._intelligence,
                    agent=self._agent,
                    risk_manager=risk_manager,
                    risk_policy=self._risk_policy,
                    executor=executor,
                    strategy_config=self._strategy_config,
                    timeframe=dataset.metadata.timeframes[0],
                    decisions=decisions,
                    trades=trades,
                    position_states=position_states,
                    evaluation_path="POSITION_MANAGEMENT_PATH",
                )
            equity_curve.append((timestamp, portfolio.total_value))
            equity_curve_detail.append(_equity_point(portfolio))

        lifecycle = _lifecycle_summary(
            initial_cash=initial_cash,
            final_portfolio=portfolio,
            trades=tuple(trades),
        )
        metrics = self._metrics.calculate(equity_curve=tuple(equity_curve), trades=tuple(trades))
        score_calibration = CalibrationAnalyzer().by_score(tuple(decisions))
        confidence_calibration = CalibrationAnalyzer().by_confidence(tuple(decisions))
        stress_results = _stress_results(tuple(equity_curve), tuple(trades))
        parameter_stability = ParameterStabilityAnalyzer().classify(
            tuple(
                item.metrics.total_return
                for item in stress_results
                if isinstance(item.metrics.total_return, Decimal)
            )
        )
        overfit = OverfitRiskAnalyzer().classify(
            trade_count=lifecycle.completed_trade_count,
            train_return=_segment_return(equity_curve, splits[0].start, splits[0].end),
            oos_return=_segment_return(equity_curve, splits[2].start, splits[2].end),
            max_single_instrument_share=_max_share(decisions, lambda item: item.symbol),
            max_single_regime_share=_max_share(decisions, lambda item: item.regime.value),
        )
        qualification = StrategyQualificationEngine().qualify(
            metrics=metrics,
            oos_metrics=self._metrics.calculate(
                equity_curve=tuple(
                    item for item in equity_curve if splits[2].start <= item[0] <= splits[2].end
                ),
                trades=tuple(
                    trade for trade in trades if splits[2].start <= trade.timestamp <= splits[2].end
                ),
            ),
            walk_forward_passed=bool(windows),
            score_calibration_passed=CalibrationAnalyzer().score_80_outperforms_60s(
                score_calibration
            ),
            parameter_stability=parameter_stability,
            overfit_risk=overfit,
            stressed_cost_passed=all(
                item.passed for item in stress_results if item.scenario != "SEVERE"
            ),
        )
        outcomes = tuple(
            decision.forward_return for decision in decisions if decision.forward_return is not None
        )
        return StrategyValidationResult(
            run_id=_run_id(dataset.metadata.data_digest, random_seed=random_seed),
            dataset=dataset.metadata,
            period_splits=splits,
            walk_forward_windows=windows,
            decisions=tuple(decisions),
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            equity_curve_detail=tuple(equity_curve_detail),
            final_portfolio=portfolio,
            lifecycle=lifecycle,
            metrics=metrics,
            benchmark_metrics=_benchmarks(bars_by_key, tuple(equity_curve), self._metrics),
            score_calibration=score_calibration,
            confidence_calibration=confidence_calibration,
            strategy_performance=_strategy_performance(decisions, self._metrics),
            asset_class_performance=_asset_class_performance(decisions, self._metrics),
            regime_performance=_regime_performance(decisions, self._metrics),
            stress_results=stress_results,
            parameter_stability=parameter_stability,
            overfit_risk=overfit,
            monte_carlo=MonteCarloTradeSequence().run(
                outcomes,
                seed=random_seed,
                iterations=200,
                sample_provenance="forward_outcome_research",
            ),
            qualification=qualification,
            broker_write=False,
            broker_write_calls=0,
            real_execution_available=False,
        )


def _execute_or_reject(
    executor: SimulatedExecutionEngine,
    portfolio: SimulatedPortfolioState,
    proposal: TradeProposal,
    quote: MarketQuote,
    risk: RiskEvaluation,
) -> tuple[SimulatedPortfolioState, SimulatedTradeRecord]:
    try:
        return executor.execute(portfolio=portfolio, proposal=proposal, quote=quote, risk=risk)
    except SimulatedExecutionError:
        return portfolio, SimulatedTradeRecord(
            timestamp=quote.as_of,
            instrument_id=proposal.instrument_id,
            symbol=proposal.symbol,
            asset_class=proposal.asset_class,
            side=proposal.side,
            action=proposal.intent,
            quantity=Decimal("0"),
            gross_value=Decimal("0"),
            costs=TransactionCostModel().estimate(
                quote=quote,
                gross_value=Decimal("0"),
                portfolio_currency=portfolio.currency,
            ),
            realized_pnl=Decimal("0"),
            status=ReplayDecisionStatus.RISK_REJECTED,
            proposal_id=str(proposal.proposal_id),
            risk_status=RiskDecisionStatus.REJECTED,
        )


def _updated_position_state(
    *,
    state: PositionManagementState,
    analysis: AegisOpportunityAnalysis,
    quote: MarketQuote,
    timestamp: datetime,
) -> PositionManagementState:
    if timestamp <= state.current_timestamp:
        return state
    return state.update_from_observation(
        timestamp=timestamp,
        price=quote.price,
        confidence=analysis.opportunity_score.confidence,
        opportunity_score=analysis.opportunity_score.overall_score,
        regime=analysis.regime.trend,
        defensive_signal_active=_defensive_signal_active(analysis),
    )


def _apply_position_state_after_trade(
    *,
    position_states: dict[int, PositionManagementState],
    portfolio: SimulatedPortfolioState,
    proposal: TradeProposal,
    quote: MarketQuote,
    analysis: AegisOpportunityAnalysis,
    agent: DeterministicAegisAgent,
) -> None:
    position = next(
        (item for item in portfolio.positions if item.instrument_id == proposal.instrument_id),
        None,
    )
    if proposal.intent is TradeIntent.OPEN:
        if position is None:
            return
        position_states[proposal.instrument_id] = PositionManagementState(
            instrument_id=proposal.instrument_id,
            symbol=proposal.symbol,
            entry_timestamp=quote.as_of,
            entry_price=quote.price,
            cost_basis=position.average_entry_price,
            entry_confidence=analysis.opportunity_score.confidence,
            entry_opportunity_score=analysis.opportunity_score.overall_score,
            entry_regime=analysis.regime.trend,
            current_timestamp=quote.as_of,
            current_price=quote.price,
            current_confidence=analysis.opportunity_score.confidence,
            current_opportunity_score=analysis.opportunity_score.overall_score,
            current_regime=analysis.regime.trend,
            position_market_value=position.market_value,
            position_weight=(
                position.market_value / portfolio.total_value
                if portfolio.total_value > 0
                else Decimal("0")
            ),
            bars_held=0,
            mfe=Decimal("0"),
            mae=Decimal("0"),
            post_entry_peak_price=quote.price,
            drawdown_from_post_entry_peak=Decimal("0"),
            defensive_signal_persistence=0,
        )
        return
    if proposal.intent is TradeIntent.INCREASE and position is not None:
        state = position_states.get(proposal.instrument_id)
        if state is not None:
            position_states[proposal.instrument_id] = state.model_copy(
                update={
                    "cost_basis": position.average_entry_price,
                    "position_market_value": position.market_value,
                    "position_weight": (
                        position.market_value / portfolio.total_value
                        if portfolio.total_value > 0
                        else Decimal("0")
                    ),
                }
            )
        return
    if proposal.intent not in {TradeIntent.REDUCE, TradeIntent.CLOSE}:
        return
    state = position_states.get(proposal.instrument_id)
    if state is None:
        return
    cooldown = _cooldown_bars_after_exit(agent, proposal.intent)
    position_market_value = position.market_value if position is not None else Decimal("0")
    position_states[proposal.instrument_id] = state.model_copy(
        update={
            "position_market_value": position_market_value,
            "position_weight": (
                position_market_value / portfolio.total_value
                if portfolio.total_value > 0
                else Decimal("0")
            ),
            "last_reduce_timestamp": quote.as_of
            if proposal.intent is TradeIntent.REDUCE
            else state.last_reduce_timestamp,
            "last_close_timestamp": quote.as_of
            if proposal.intent is TradeIntent.CLOSE
            else state.last_close_timestamp,
            "last_exit_reason": proposal.reason,
            "cooldown_bars_remaining": cooldown,
        }
    )


def _cooldown_bars_after_exit(agent: DeterministicAegisAgent, intent: TradeIntent) -> int:
    exit_policy = getattr(agent, "_exit_policy", None)
    if not isinstance(exit_policy, ExitPolicyV2Guarded):
        return 0
    return exit_policy.cooldown_bars_for_exit(RecommendedAction(intent.value))


def _defensive_signal_active(analysis: AegisOpportunityAnalysis) -> bool:
    return any(
        signal.direction in {StrategyDirection.REDUCE, StrategyDirection.AVOID}
        for signal in analysis.strategy_signals
    )


def _evaluate_replay_candidate(
    *,
    candidate: OpportunityCandidate,
    bars: tuple[MarketBar, ...],
    portfolio: SimulatedPortfolioState,
    timestamp: datetime,
    replay_clock: HistoricalReplayClock,
    intelligence: AegisOpportunityIntelligenceEngine,
    agent: DeterministicAegisAgent,
    risk_manager: RiskManager,
    risk_policy: RiskPolicyConfig,
    executor: SimulatedExecutionEngine,
    strategy_config: AegisStrategyConfig,
    timeframe: TimeFrame,
    decisions: list[ReplayDecision],
    trades: list[SimulatedTradeRecord],
    position_states: dict[int, PositionManagementState],
    evaluation_path: str,
) -> SimulatedPortfolioState:
    visible_bars = replay_clock.visible_bars(bars)
    analysis = intelligence.analyze_candidate(
        candidate=candidate,
        portfolio=_domain_portfolio(portfolio),
        bars_by_timeframe={TimeFrame.ONE_DAY: visible_bars},
        as_of=timestamp,
    )
    instrument_id = candidate.instrument.numeric_instrument_id
    position_state = (
        _updated_position_state(
            state=position_states[instrument_id],
            analysis=analysis,
            quote=candidate.quote,
            timestamp=timestamp,
        )
        if instrument_id is not None
        and instrument_id in position_states
        and candidate.quote is not None
        else None
    )
    if instrument_id is not None and position_state is not None:
        position_states[instrument_id] = position_state
    result = agent.analyze(
        AegisAgentContext(
            portfolio=_domain_portfolio(portfolio),
            quotes=(candidate.quote,) if candidate.quote is not None else (),
            news=(),
            instruments=(_metadata(candidate.instrument, timestamp),),
            candidates=(candidate,),
            intelligence_reports=(analysis,),
            position_states=(position_state,) if position_state is not None else (),
            analysis_timestamp=timestamp,
            strategy=strategy_config,
            minimum_trade_amount=candidate.instrument.minimum_order_value,
        )
    )
    future_records_ignored = len(bars) - len(visible_bars)
    status = ReplayDecisionStatus.HOLD
    risk_reasons: tuple[str, ...] = ()
    blocker_reasons = list(analysis.reasons)
    blocker_reasons.append(f"evaluation_path:{evaluation_path}")
    proposal_id = None
    risk_evaluation: RiskEvaluation | None = None
    if result.proposal is not None and candidate.quote is not None:
        risk_context = _risk_context(
            portfolio=portfolio,
            proposal=result.proposal,
            quote=candidate.quote,
            instrument=candidate.instrument,
            at=timestamp,
            timeframe=timeframe,
        )
        risk = risk_manager.evaluate(
            result.proposal,
            risk_context,
        )
        risk_evaluation = risk
        proposal_id = str(result.proposal.proposal_id)
        if risk.decision.status is RiskDecisionStatus.APPROVED:
            portfolio, trade = _execute_or_reject(
                executor, portfolio, result.proposal, candidate.quote, risk
            )
            trades.append(trade)
            if trade.status is ReplayDecisionStatus.SIMULATED_EXECUTED:
                _apply_position_state_after_trade(
                    position_states=position_states,
                    portfolio=portfolio,
                    proposal=result.proposal,
                    quote=candidate.quote,
                    analysis=analysis,
                    agent=agent,
                )
            status = trade.status
            if trade.status is ReplayDecisionStatus.RISK_REJECTED:
                risk_reasons = ("SIMULATED_EXECUTION_REJECTED",)
        else:
            status = ReplayDecisionStatus.RISK_REJECTED
            risk_reasons = tuple(v.code.value for v in risk.decision.violations)
            blocker_reasons.extend(f"RiskManager:{reason}" for reason in risk_reasons)
    else:
        blocker_reasons.extend(result.analysis.risk_factors)
    decisions.append(
        ReplayDecision(
            timestamp=timestamp,
            symbol=candidate.instrument.symbol,
            asset_class=candidate.asset_class,
            score=analysis.opportunity_score.overall_score,
            score_band=analysis.opportunity_score.band,
            confidence=analysis.opportunity_score.confidence,
            regime=analysis.regime.trend,
            aegis_decision=analysis.decision,
            status=status,
            ensemble_direction=analysis.ensemble.direction,
            strategy_directions=tuple(signal.direction for signal in analysis.strategy_signals),
            strategy_signal_counts=_strategy_signal_counts(
                tuple(signal.direction for signal in analysis.strategy_signals)
            ),
            forward_return=_forward_return(
                bars,
                timestamp=timestamp,
                horizon=5,
            ),
            proposal_id=proposal_id,
            proposal_side=(result.proposal.side if result.proposal is not None else None),
            proposal_intent=(result.proposal.intent if result.proposal is not None else None),
            risk_reasons=risk_reasons,
            blocker_reasons=tuple(blocker_reasons),
            proposal_gate_trace=_proposal_gate_trace(
                analysis=analysis,
                proposal=result.proposal,
                profile_minimum_score=_profile_minimum_score(candidate.asset_class),
                profile_minimum_confidence=(
                    analysis.opportunity_score.confidence_threshold
                    or _profile_minimum_confidence(candidate.asset_class)
                ),
                visible_bar_count=len(visible_bars),
                requested_bar_count=len(bars),
                risk_evaluation=risk_evaluation,
                risk_policy=risk_policy,
                quote=candidate.quote,
                reference_time=(
                    risk_evaluation.decision.evaluated_at
                    if risk_evaluation is not None
                    else timestamp
                ),
                asset_class=candidate.asset_class,
            ),
            confidence_decomposition=_confidence_decomposition(analysis),
            future_records_ignored=future_records_ignored,
        )
    )
    return portfolio


def _instrument_by_id(
    instruments: tuple[UniversalInstrument, ...],
    instrument_id: int,
) -> UniversalInstrument | None:
    return next(
        (
            instrument
            for instrument in instruments
            if instrument.numeric_instrument_id == instrument_id
        ),
        None,
    )


def _all_timestamps(bars_by_key: dict[str, tuple[MarketBar, ...]]) -> tuple[datetime, ...]:
    return tuple(sorted({bar.timestamp for bars in bars_by_key.values() for bar in bars}))


def _instruments_from_bars(
    bars_by_key: dict[str, tuple[MarketBar, ...]],
) -> tuple[UniversalInstrument, ...]:
    return tuple(bars[0].instrument for _, bars in sorted(bars_by_key.items()) if bars)


def _latest_quotes(
    instruments: tuple[UniversalInstrument, ...],
    bars_by_key: dict[str, tuple[MarketBar, ...]],
    clock: HistoricalReplayClock,
) -> dict[int, MarketQuote]:
    quotes: dict[int, MarketQuote] = {}
    for instrument in instruments:
        visible = clock.visible_bars(bars_by_key[instrument.key])
        if not visible:
            continue
        quote = _quote_from_bar(instrument, visible[-1])
        quotes[quote.instrument_id] = quote
    return quotes


def _candidate_at(
    *,
    instrument: UniversalInstrument,
    bars: tuple[MarketBar, ...],
    portfolio: SimulatedPortfolioState,
    clock: HistoricalReplayClock,
) -> OpportunityCandidate:
    visible = clock.visible_bars(bars)
    latest = visible[-1]
    previous = visible[-2] if len(visible) > 1 else latest
    quote = _quote_from_bar(instrument, latest, previous=previous)
    spread = (quote.ask - quote.bid) if quote.ask is not None and quote.bid is not None else None
    spread_pct = spread / quote.price if spread is not None else None
    current_weight = Decimal("0")
    if portfolio.total_value > 0:
        current_weight = (
            next(
                (
                    position.market_value
                    for position in portfolio.positions
                    if position.instrument_id == quote.instrument_id
                ),
                Decimal("0"),
            )
            / portfolio.total_value
        )
    return OpportunityCandidate(
        candidate_id=f"validation-{instrument.symbol}-{latest.timestamp.isoformat()}",
        broker=instrument.broker,
        instrument=instrument,
        asset_class=instrument.asset_class,
        market_status=MarketStatus.CONTINUOUS_24_7
        if instrument.asset_class.value == "CRYPTO"
        else MarketStatus.OPEN,
        quote=quote,
        broker_eligibility=_eligibility(instrument, latest.timestamp),
        policy_allowed=True,
        policy_version="asset-policy-v1",
        candidate_state=CandidateState.OPEN_AND_ALLOWED,
        data_quality=DataQualityStatus.GOOD,
        candidate_score=Decimal("50"),
        opportunity_factors=("historical replay",),
        risk_factors=("historical simulation risk",),
        features=OpportunityFeatures(
            mid_price=quote.price,
            spread=spread,
            spread_percentage=spread_pct,
            short_term_momentum=(latest.close - previous.close) / previous.close,
            current_portfolio_weight=current_weight,
        ),
        confidence=Decimal("0.70"),
        scanner_version=SCANNER_VERSION,
        ranking_version=RANKING_VERSION,
        timestamp=latest.timestamp,
    )


def _quote_from_bar(
    instrument: UniversalInstrument, bar: MarketBar, *, previous: MarketBar | None = None
) -> MarketQuote:
    instrument_id = instrument.numeric_instrument_id
    if instrument_id is None:
        raise ValueError("historical validation requires numeric instrument IDs")
    half_spread = bar.close * Decimal("0.001")
    return MarketQuote(
        instrument_id=instrument_id,
        symbol=instrument.symbol,
        price=bar.close,
        as_of=bar.timestamp,
        currency=bar.currency,
        source="historical-replay",
        previous_close=previous.close if previous is not None else None,
        bid=bar.close - half_spread,
        ask=bar.close + half_spread,
        market_status=MarketStatus.CONTINUOUS_24_7
        if instrument.asset_class.value == "CRYPTO"
        else MarketStatus.OPEN,
    )


def _eligibility(instrument: UniversalInstrument, at: datetime) -> BrokerEligibilitySnapshot:
    return BrokerEligibilitySnapshot(
        broker=instrument.broker,
        broker_instrument_id=instrument.broker_instrument_id,
        symbol=instrument.symbol,
        checked_at=at,
        currency=instrument.currency,
        verified=True,
        allow_open=True,
        allow_close=True,
        minimum_order_value=instrument.minimum_order_value or Decimal("5"),
        settlement_type=SettlementType.REAL,
        leverage_configs=(1,),
    )


def _metadata(instrument: UniversalInstrument, at: datetime) -> InstrumentMetadata:
    instrument_id = instrument.numeric_instrument_id
    if instrument_id is None:
        raise ValueError("historical validation requires numeric instrument IDs")
    return InstrumentMetadata(
        instrument_id=instrument_id,
        symbol=instrument.symbol,
        asset_class=instrument.asset_class,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=instrument.minimum_order_value,
        metadata_as_of=at,
        source="historical-validation",
    )


def _domain_portfolio(portfolio: SimulatedPortfolioState) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=portfolio.as_of,
        currency=portfolio.currency,
        cash=portfolio.cash,
        positions=tuple(
            Position(
                position_id=f"validation-{position.instrument_id}",
                instrument_id=position.instrument_id,
                symbol=position.symbol,
                settlement_type=SettlementType.REAL,
                units=position.units,
                average_entry_price=position.average_entry_price,
                market_price=position.market_price,
            )
            for position in portfolio.positions
        ),
        reported_total_value=portfolio.total_value,
        peak_value=portfolio.peak_value,
    )


def _risk_context(
    *,
    portfolio: SimulatedPortfolioState,
    proposal: TradeProposal,
    quote: MarketQuote,
    instrument: UniversalInstrument,
    at: datetime,
    timeframe: TimeFrame,
) -> RiskContext:
    risk_reference_time = HistoricalReplayFreshnessPolicy().risk_reference_time(
        asset_class=instrument.asset_class,
        timeframe=timeframe,
        price_timestamp=quote.as_of,
        replay_timestamp=at,
    )
    return RiskContext(
        evaluated_at=risk_reference_time,
        portfolio=_domain_portfolio(portfolio),
        price=quote.to_price_snapshot(),
        instrument=_metadata(instrument, risk_reference_time),
        market_data_available=True,
        news_data_available=True,
        daily_new_trade_count=0,
        api_state_consistent=True,
        recent_idempotency_keys=frozenset(),
    )


def _equity_point(portfolio: SimulatedPortfolioState) -> ReplayEquityPoint:
    total = portfolio.total_value
    drawdown = Decimal("0")
    if portfolio.peak_value > 0 and total < portfolio.peak_value:
        drawdown = (portfolio.peak_value - total) / portfolio.peak_value
    gross_exposure = portfolio.positions_value / total if total > 0 else Decimal("0")
    return ReplayEquityPoint(
        timestamp=portfolio.as_of,
        cash=portfolio.cash,
        position_market_value=portfolio.positions_value,
        realized_pnl=portfolio.realized_pnl,
        unrealized_pnl=portfolio.unrealized_pnl,
        total_equity=total,
        drawdown=drawdown,
        gross_exposure=gross_exposure,
    )


def _lifecycle_summary(
    *,
    initial_cash: Decimal,
    final_portfolio: SimulatedPortfolioState,
    trades: tuple[SimulatedTradeRecord, ...],
) -> TradeLifecycleSummary:
    executed = tuple(
        trade for trade in trades if trade.status is ReplayDecisionStatus.SIMULATED_EXECUTED
    )
    entries = tuple(trade for trade in executed if trade.action is TradeIntent.OPEN)
    increases = tuple(trade for trade in executed if trade.action is TradeIntent.INCREASE)
    reductions = tuple(trade for trade in executed if trade.action is TradeIntent.REDUCE)
    closes = tuple(trade for trade in executed if trade.action is TradeIntent.CLOSE)
    ending_equity = final_portfolio.total_value
    realized_pnl = final_portfolio.realized_pnl
    unrealized_pnl = final_portfolio.unrealized_pnl
    return TradeLifecycleSummary(
        initial_cash=initial_cash,
        ending_cash=final_portfolio.cash,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        ending_equity=ending_equity,
        realized_return=realized_pnl / initial_cash if initial_cash > 0 else "NOT_ENOUGH_DATA",
        mark_to_market_return=unrealized_pnl / initial_cash
        if initial_cash > 0
        else "NOT_ENOUGH_DATA",
        total_equity_return=ending_equity / initial_cash - Decimal("1")
        if initial_cash > 0
        else "NOT_ENOUGH_DATA",
        entry_count=len(entries),
        increase_count=len(increases),
        reduction_count=len(reductions),
        close_count=len(closes),
        execution_count=len(executed),
        completed_trade_count=len(closes),
        open_position_count=len(final_portfolio.positions),
        open_position_symbols=tuple(
            sorted(position.symbol for position in final_portfolio.positions)
        ),
    )


def _forward_return(
    bars: tuple[MarketBar, ...], *, timestamp: datetime, horizon: int
) -> Decimal | None:
    ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
    index = next((idx for idx, bar in enumerate(ordered) if bar.timestamp == timestamp), None)
    if index is None or index + horizon >= len(ordered):
        return None
    return ordered[index + horizon].close / ordered[index].close - Decimal("1")


def _strategy_signal_counts(directions: tuple[StrategyDirection, ...]) -> dict[str, int]:
    counts = Counter(direction.value for direction in directions)
    return dict(sorted(counts.items()))


def _profile_minimum_score(asset_class: AssetClass) -> Decimal:
    return profile_for(asset_class).minimum_score_for_buy


def _profile_minimum_confidence(asset_class: AssetClass) -> Decimal:
    return profile_for(asset_class).minimum_confidence_for_buy


def _proposal_gate_trace(
    *,
    analysis: AegisOpportunityAnalysis,
    proposal: TradeProposal | None,
    profile_minimum_score: Decimal,
    profile_minimum_confidence: Decimal,
    visible_bar_count: int,
    requested_bar_count: int,
    risk_evaluation: RiskEvaluation | None,
    risk_policy: RiskPolicyConfig,
    quote: MarketQuote | None,
    reference_time: datetime,
    asset_class: AssetClass,
) -> tuple[dict[str, object], ...]:
    feature_quality = analysis.features.quality
    confidence = analysis.opportunity_score.confidence
    score = analysis.opportunity_score.overall_score
    final_action = analysis.decision.value
    data_quality_reason = (
        "provider-limited or missing optional features"
        if feature_quality.value == "PARTIAL"
        else feature_quality.value
    )
    gates: list[dict[str, object]] = [
        {
            "gate": "opportunity_score",
            "actual": str(score),
            "threshold": str(profile_minimum_score),
            "operator": ">=",
            "passed": score >= profile_minimum_score,
        },
        {
            "gate": "confidence",
            "actual": str(confidence),
            "threshold": str(profile_minimum_confidence),
            "operator": ">=",
            "passed": confidence >= profile_minimum_confidence,
            "scale": "0..1",
            "confidence_model_version": analysis.opportunity_score.confidence_model_version,
            "confidence_semantics_version": (
                analysis.opportunity_score.confidence_semantics_version
            ),
            "threshold_provenance": analysis.opportunity_score.confidence_threshold_provenance,
        },
        {
            "gate": "data_quality",
            "actual": feature_quality.value,
            "threshold": "GOOD preferred; PARTIAL allowed for score but limits conviction",
            "operator": "diagnostic",
            "passed": feature_quality.value not in {"DATA_INSUFFICIENT", "STALE", "CONFLICTING"},
            "reason": data_quality_reason,
            "visible_bar_count": visible_bar_count,
            "requested_bar_count": requested_bar_count,
        },
        {
            "gate": "final_opportunity_action",
            "actual": final_action,
            "threshold": "BUY",
            "operator": "==",
            "passed": final_action == "BUY",
        },
        {
            "gate": "trade_proposal",
            "actual": proposal is not None,
            "threshold": True,
            "operator": "==",
            "passed": proposal is not None,
        },
    ]
    if proposal is not None and risk_evaluation is not None:
        gates.extend(
            _risk_manager_gate_trace(
                proposal=proposal,
                risk_evaluation=risk_evaluation,
                risk_policy=risk_policy,
                quote=quote,
                reference_time=reference_time,
                asset_class=asset_class,
            )
        )
    return tuple(gates)


def _risk_manager_gate_trace(
    *,
    proposal: TradeProposal,
    risk_evaluation: RiskEvaluation,
    risk_policy: RiskPolicyConfig,
    quote: MarketQuote | None,
    reference_time: datetime,
    asset_class: AssetClass,
) -> tuple[dict[str, object], ...]:
    asset_policy = default_asset_policy_engine().policy_for(asset_class)
    legacy_minimum_confidence = max(risk_policy.minimum_confidence, asset_policy.minimum_confidence)
    v2_b_metadata_valid = (
        proposal.confidence_model_version == CONFIDENCE_MODEL_V2_B
        and proposal.confidence_semantics_version == CONFIDENCE_SEMANTICS_V2
        and proposal.confidence_threshold == V2_B_THRESHOLD
        and proposal.confidence_threshold_provenance == V2_B_THRESHOLD_PROVENANCE
    )
    v1_metadata_valid = (
        proposal.confidence_model_version == CONFIDENCE_MODEL_V1
        and proposal.confidence_semantics_version == CONFIDENCE_SEMANTICS_V1
    )
    risk_minimum_confidence = V2_B_THRESHOLD if v2_b_metadata_valid else legacy_minimum_confidence
    rejection_codes = tuple(
        violation.code.value for violation in risk_evaluation.decision.violations
    )
    price_timestamp = quote.as_of if quote is not None else None
    price_age_seconds = (
        str((reference_time - price_timestamp).total_seconds())
        if price_timestamp is not None
        else None
    )
    stale_price_failed = RiskViolationCode.STALE_PRICE.value in rejection_codes
    confidence_failed = RiskViolationCode.CONFIDENCE_BELOW_MINIMUM.value in rejection_codes
    return (
        {
            "gate": "risk_manager_confidence",
            "actual": str(proposal.confidence),
            "threshold": str(risk_minimum_confidence),
            "operator": ">=",
            "passed": not confidence_failed,
            "scale": "0..1",
            "field_read": "TradeProposal.confidence",
            "risk_manager_source": "app/risk/manager.py:RiskManager.evaluate",
            "threshold_sources": (
                "app/config/models.py:RiskPolicyConfig.minimum_confidence",
                "app/policies/defaults.py:conservative_asset_policies",
            ),
            "threshold_provenance": (
                V2_B_THRESHOLD_PROVENANCE if v2_b_metadata_valid else "SAFETY_DEFAULT"
            ),
            "confidence_model_version": proposal.confidence_model_version,
            "confidence_semantics_version": proposal.confidence_semantics_version,
            "confidence_threshold_provenance": proposal.confidence_threshold_provenance,
            "confidence_semantic_valid": v2_b_metadata_valid or v1_metadata_valid,
            "duplicate_numeric_floor_removed_for_v2_b": v2_b_metadata_valid,
            "risk_policy_minimum_confidence": str(risk_policy.minimum_confidence),
            "asset_policy_minimum_confidence": str(asset_policy.minimum_confidence),
            "asset_policy_version": "asset-policy-v1",
            "risk_policy_profile_version": "RiskPolicyConfig",
            "rejection_codes": rejection_codes,
        },
        {
            "gate": "risk_manager_stale_price",
            "actual": price_age_seconds,
            "threshold": str(risk_policy.max_price_age_seconds),
            "operator": f"0 <= age_seconds <= {risk_policy.max_price_age_seconds}",
            "passed": not stale_price_failed,
            "price_timestamp": price_timestamp.isoformat() if price_timestamp is not None else None,
            "reference_timestamp": reference_time.isoformat(),
            "reference_time_source": "HistoricalReplayClock.current_time",
            "risk_context_field": "RiskContext.evaluated_at",
            "risk_manager_source": "app/risk/manager.py:RiskManager.evaluate",
            "rejection_codes": rejection_codes,
        },
        {
            "gate": "risk_manager_authorization",
            "actual": risk_evaluation.decision.status.value,
            "threshold": RiskDecisionStatus.APPROVED.value,
            "operator": "==",
            "passed": risk_evaluation.decision.status is RiskDecisionStatus.APPROVED,
            "authorization_issued": risk_evaluation.authorization is not None,
            "authorization_deferred": risk_evaluation.authorization_deferred,
            "rejection_codes": rejection_codes,
        },
    )


def _confidence_decomposition(analysis: AegisOpportunityAnalysis) -> dict[str, object]:
    signals = analysis.strategy_signals
    base_strategy_confidence = (
        sum((signal.confidence for signal in signals), Decimal("0")) / Decimal(len(signals))
        if signals
        else Decimal("0")
    )
    agreement_multiplier = Decimal("0.50") + analysis.ensemble.agreement / Decimal("2")
    quality_multiplier = _ensemble_quality_multiplier(analysis.features.quality)
    regime_multiplier = max(analysis.regime.confidence, Decimal("0.20"))
    strong_disagreement_multiplier = (
        Decimal("0.70") if _analysis_has_strong_disagreement(analysis) else Decimal("1")
    )
    pre_clamp_ensemble_confidence = (
        base_strategy_confidence
        * agreement_multiplier
        * quality_multiplier
        * regime_multiplier
        * strong_disagreement_multiplier
    )
    data_quality_component = analysis.opportunity_score.data_quality_score / Decimal("100")
    pre_round_final_confidence = (
        analysis.ensemble.confidence + analysis.regime.confidence + data_quality_component
    ) / Decimal("3")
    return {
        "strategy_confidences": tuple(
            {
                "strategy_id": signal.strategy_id,
                "direction": signal.direction.value,
                "strength": str(signal.strength),
                "confidence": str(signal.confidence),
                "data_quality": signal.data_quality.value,
            }
            for signal in signals
        ),
        "mean_base_strategy_confidence": str(base_strategy_confidence.quantize(Decimal("0.0001"))),
        "agreement_ratio": str(analysis.ensemble.agreement),
        "agreement_multiplier": str(agreement_multiplier),
        "feature_quality_state": analysis.features.quality.value,
        "feature_quality_multiplier": str(quality_multiplier),
        "feature_quality_counts": dict(
            sorted(
                Counter(
                    feature.quality.value
                    for feature_set in analysis.features.feature_sets
                    for feature in feature_set.features
                ).items()
            )
        ),
        "insufficient_feature_names": tuple(
            feature.name.value
            for feature_set in analysis.features.feature_sets
            for feature in feature_set.features
            if feature.quality is FeatureQuality.DATA_INSUFFICIENT
        ),
        "regime_confidence": str(analysis.regime.confidence),
        "regime_multiplier": str(regime_multiplier),
        "strong_disagreement_multiplier": str(strong_disagreement_multiplier),
        "portfolio_fit_status": analysis.portfolio_fit.status.value,
        "portfolio_fit_score": str(analysis.portfolio_fit.score),
        "news_status": analysis.news_signal.status.value,
        "news_confidence": str(analysis.news_signal.confidence),
        "liquidity_score": str(analysis.opportunity_score.liquidity_score),
        "execution_readiness_score": (
            str(analysis.opportunity_score.execution_readiness_score)
            if analysis.opportunity_score.execution_readiness_score is not None
            else None
        ),
        "data_quality_score": str(analysis.opportunity_score.data_quality_score),
        "confidence_model_version": analysis.opportunity_score.confidence_model_version,
        "confidence_semantics_version": analysis.opportunity_score.confidence_semantics_version,
        "confidence_threshold_provenance": (
            analysis.opportunity_score.confidence_threshold_provenance
        ),
        "calibration_dataset_id": analysis.opportunity_score.calibration_dataset_id,
        "calibration_version": analysis.opportunity_score.calibration_version,
        "asset_evidence_warning": analysis.opportunity_score.asset_evidence_warning,
        "ensemble_confidence_before_clamp": str(
            pre_clamp_ensemble_confidence.quantize(Decimal("0.0001"))
        ),
        "ensemble_confidence_after_rounding": str(analysis.ensemble.confidence),
        "legacy_v1_opportunity_confidence_formula": (
            "(ensemble_confidence + regime_confidence + data_quality_score/100) / 3"
        ),
        "opportunity_confidence_formula": (
            "clamp(0.30*directional_consistency + 0.25*mean_base_strategy_confidence "
            "+ 0.20*agreement_ratio + 0.15*regime_confidence "
            "+ 0.10*required_feature_sufficiency)"
        ),
        "opportunity_confidence_before_rounding": str(
            pre_round_final_confidence.quantize(Decimal("0.0001"))
        ),
        "normalized_final_confidence": str(analysis.opportunity_score.confidence),
        "clamps_floors_caps": (
            "regime multiplier floor = 0.20",
            "ensemble confidence clamped to 0..1 and rounded to 0.01",
            "final confidence capped at 1 and rounded to 0.01",
        ),
    }


def _ensemble_quality_multiplier(quality: FeatureQuality) -> Decimal:
    return {
        FeatureQuality.GOOD: Decimal("1"),
        FeatureQuality.PARTIAL: Decimal("0.70"),
        FeatureQuality.DATA_INSUFFICIENT: Decimal("0.25"),
        FeatureQuality.UNKNOWN: Decimal("0.20"),
        FeatureQuality.STALE: Decimal("0.20"),
        FeatureQuality.CONFLICTING: Decimal("0.30"),
    }[quality]


def _analysis_has_strong_disagreement(analysis: AegisOpportunityAnalysis) -> bool:
    positive = any(
        signal.direction in {StrategyDirection.STRONG_BUY, StrategyDirection.BUY}
        for signal in analysis.strategy_signals
    )
    negative = any(
        signal.direction in {StrategyDirection.REDUCE, StrategyDirection.AVOID}
        for signal in analysis.strategy_signals
    )
    return positive and negative


def _segment_return(
    equity_curve: list[tuple[datetime, Decimal]], start: datetime, end: datetime
) -> Decimal | str:
    segment = tuple(item for item in equity_curve if start <= item[0] <= end)
    if len(segment) < 2 or segment[0][1] <= 0:
        return "NOT_ENOUGH_DATA"
    return segment[-1][1] / segment[0][1] - Decimal("1")


def _max_share(decisions: list[ReplayDecision], key: Callable[[ReplayDecision], str]) -> Decimal:
    if not decisions or not callable(key):
        return Decimal("0")
    counts = Counter(str(key(item)) for item in decisions)
    return Decimal(max(counts.values())) / Decimal(len(decisions))


def _stress_results(
    equity_curve: tuple[tuple[datetime, Decimal], ...],
    trades: tuple[SimulatedTradeRecord, ...],
) -> tuple[StressScenarioResult, ...]:
    calculator = PerformanceMetricCalculator()
    results: list[StressScenarioResult] = []
    for scenario, haircut in (
        (SlippageScenario.STRESSED, Decimal("0.98")),
        (SlippageScenario.SEVERE, Decimal("0.95")),
    ):
        stressed_curve = tuple((timestamp, value * haircut) for timestamp, value in equity_curve)
        metrics = calculator.calculate(equity_curve=stressed_curve, trades=trades)
        passed = not isinstance(
            metrics.maximum_drawdown, Decimal
        ) or metrics.maximum_drawdown <= Decimal("0.35")
        results.append(
            StressScenarioResult(
                scenario=scenario.value,
                metrics=metrics,
                passed=passed,
                reasons=() if passed else ("stress drawdown exceeded threshold",),
            )
        )
    return tuple(results)


def _benchmarks(
    bars_by_key: dict[str, tuple[MarketBar, ...]],
    equity_curve: tuple[tuple[datetime, Decimal], ...],
    calculator: PerformanceMetricCalculator,
) -> dict[str, object]:
    if not equity_curve:
        return {}
    start_value = equity_curve[0][1]
    curves: dict[str, tuple[tuple[datetime, Decimal], ...]] = {
        "cash": tuple((timestamp, start_value) for timestamp, _ in equity_curve),
    }
    first_bars = next(iter(bars_by_key.values()), ())
    if len(first_bars) >= 2:
        first_price = first_bars[0].close
        curves["buy_and_hold"] = tuple(
            (bar.timestamp, start_value * (bar.close / first_price)) for bar in first_bars
        )
    if bars_by_key:
        curves["equal_weight"] = _equal_weight_curve(bars_by_key, start_value)
    return {
        name: calculator.calculate(equity_curve=curve, trades=()) for name, curve in curves.items()
    }


def _equal_weight_curve(
    bars_by_key: dict[str, tuple[MarketBar, ...]], start_value: Decimal
) -> tuple[tuple[datetime, Decimal], ...]:
    common = sorted(
        set.intersection(*(set(bar.timestamp for bar in bars) for bars in bars_by_key.values()))
    )
    curve: list[tuple[datetime, Decimal]] = []
    for timestamp in common:
        relatives = []
        for bars in bars_by_key.values():
            first = bars[0].close
            current = next(bar.close for bar in bars if bar.timestamp == timestamp)
            relatives.append(current / first)
        curve.append(
            (timestamp, start_value * sum(relatives, Decimal("0")) / Decimal(len(relatives)))
        )
    return tuple(curve)


def _strategy_performance(
    decisions: list[ReplayDecision], calculator: PerformanceMetricCalculator
) -> dict[str, object]:
    strategies = (
        "TrendFollowingStrategy",
        "MomentumStrategy",
        "BreakoutStrategy",
        "MeanReversionStrategy",
        "DefensiveStrategy",
        "StrategyEnsemble",
    )
    return {
        strategy: _metrics_from_decisions(tuple(decisions), calculator) for strategy in strategies
    }


def _asset_class_performance(
    decisions: list[ReplayDecision], calculator: PerformanceMetricCalculator
) -> dict[str, object]:
    grouped: dict[str, list[ReplayDecision]] = defaultdict(list)
    for decision in decisions:
        grouped[decision.asset_class.value].append(decision)
    return {
        key: _metrics_from_decisions(tuple(value), calculator) for key, value in grouped.items()
    }


def _regime_performance(
    decisions: list[ReplayDecision], calculator: PerformanceMetricCalculator
) -> dict[str, object]:
    grouped: dict[str, list[ReplayDecision]] = defaultdict(list)
    for decision in decisions:
        grouped[decision.regime.value].append(decision)
    return {
        key: _metrics_from_decisions(tuple(value), calculator) for key, value in grouped.items()
    }


def _metrics_from_decisions(
    decisions: tuple[ReplayDecision, ...], calculator: PerformanceMetricCalculator
) -> object:
    equity = Decimal("1")
    curve: list[tuple[datetime, Decimal]] = []
    for decision in decisions:
        if decision.forward_return is not None:
            equity *= Decimal("1") + decision.forward_return
        curve.append((decision.timestamp, equity))
    return calculator.calculate(equity_curve=tuple(curve), trades=())


def _run_id(dataset_digest: str, *, random_seed: int) -> str:
    payload = json.dumps(
        {
            "dataset_digest": dataset_digest,
            "strategy_version": STRATEGY_VERSION,
            "validation_version": VALIDATION_ENGINE_VERSION,
            "random_seed": random_seed,
            "created_at": datetime.now(UTC).date().isoformat(),
        },
        sort_keys=True,
    )
    return "validation-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
