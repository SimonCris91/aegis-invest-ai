"""Decision-funnel, threshold sensitivity, and research-matrix diagnostics."""

from collections import Counter, defaultdict
from decimal import Decimal

from app.domain.enums import AssetClass, TradeIntent, TradeSide
from app.intelligence.models import (
    AegisDecision,
    RegimeLabel,
    StrategyDirection,
    TimeFrame,
)
from app.intelligence.profiles import (
    BREAKOUT_ID,
    DEFENSIVE_ID,
    MEAN_REVERSION_ID,
    MOMENTUM_ID,
    TREND_FOLLOWING_ID,
    profile_for,
)
from app.validation.metrics import NOT_ENOUGH_DATA, PerformanceMetricCalculator
from app.validation.models import (
    DecisionFunnel,
    EvidenceRequirements,
    PerformanceSummary,
    ReplayDecision,
    ReplayDecisionStatus,
    ResearchMatrixRow,
    SimulatedTradeRecord,
    StrategyValidationResult,
    ThresholdSensitivityPoint,
    ZeroTradeDiagnostic,
)
from app.validation.qualification import StrategyQualificationEngine

_STRATEGY_IDS = (
    TREND_FOLLOWING_ID,
    MOMENTUM_ID,
    BREAKOUT_ID,
    MEAN_REVERSION_ID,
    DEFENSIVE_ID,
    "StrategyEnsemble",
)


def build_decision_funnel(
    decisions: tuple[ReplayDecision, ...],
    trades: tuple[SimulatedTradeRecord, ...],
) -> DecisionFunnel:
    direction_counts = Counter(
        direction for decision in decisions for direction in decision.strategy_directions
    )
    return DecisionFunnel(
        market_observations=len(decisions),
        candidates_analyzed=len(decisions),
        buy_signals=direction_counts[StrategyDirection.BUY]
        + direction_counts[StrategyDirection.STRONG_BUY],
        watch_signals=direction_counts[StrategyDirection.WATCH],
        hold_signals=direction_counts[StrategyDirection.HOLD],
        avoid_signals=direction_counts[StrategyDirection.AVOID],
        reduce_signals=direction_counts[StrategyDirection.REDUCE],
        final_buy_decisions=sum(
            1 for decision in decisions if decision.aegis_decision is AegisDecision.BUY
        ),
        final_hold_decisions=sum(
            1 for decision in decisions if decision.aegis_decision is AegisDecision.HOLD
        ),
        final_reduce_decisions=sum(
            1 for decision in decisions if decision.aegis_decision is AegisDecision.REDUCE
        ),
        final_ignore_decisions=sum(
            1 for decision in decisions if decision.aegis_decision is AegisDecision.IGNORE
        ),
        trade_proposals=sum(1 for decision in decisions if decision.proposal_id is not None),
        risk_rejected=sum(
            1 for decision in decisions if decision.status is ReplayDecisionStatus.RISK_REJECTED
        ),
        simulated_executed=sum(
            1
            for decision in decisions
            if decision.status is ReplayDecisionStatus.SIMULATED_EXECUTED
        ),
        exited_trades=sum(
            1
            for trade in trades
            if trade.side is TradeSide.SELL and trade.action is TradeIntent.CLOSE
        ),
    )


def diagnose_zero_or_low_trades(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[ZeroTradeDiagnostic, ...]:
    if not decisions:
        return (
            ZeroTradeDiagnostic(
                blocker="NO_DECISIONS_REPLAYED",
                count=1,
                share=Decimal("1"),
                candidate_incidence_count=1,
                candidate_incidence_rate=Decimal("1"),
                share_of_all_blocker_events=Decimal("1"),
                root_gate_failure=True,
            ),
        )
    candidate_sets: dict[str, set[int]] = defaultdict(set)
    event_counter: Counter[str] = Counter()
    for decision in decisions:
        if decision.status is ReplayDecisionStatus.SIMULATED_EXECUTED:
            continue
        if decision.risk_reasons:
            for reason in decision.risk_reasons:
                blocker = f"RiskManager:{reason}"
                event_counter[blocker] += 1
                candidate_sets[blocker].add(id(decision))
        root_blockers = _root_gate_blockers(decision)
        if root_blockers:
            for blocker in root_blockers:
                event_counter[blocker] += 1
                candidate_sets[blocker].add(id(decision))
            continue
        if decision.proposal_id is None:
            blocker = "downstream:no trade proposal after upstream gates"
            event_counter[blocker] += 1
            candidate_sets[blocker].add(id(decision))
    total = sum(event_counter.values())
    if total == 0:
        return ()
    return tuple(
        ZeroTradeDiagnostic(
            blocker=blocker,
            count=count,
            share=(Decimal(count) / Decimal(total)).quantize(Decimal("0.0001")),
            candidate_incidence_count=len(candidate_sets[blocker]),
            candidate_incidence_rate=(
                Decimal(len(candidate_sets[blocker])) / Decimal(len(decisions))
            ).quantize(Decimal("0.0001")),
            share_of_all_blocker_events=(Decimal(count) / Decimal(total)).quantize(
                Decimal("0.0001")
            ),
            root_gate_failure=not blocker.startswith("downstream:"),
        )
        for blocker, count in event_counter.most_common()
    )


def build_threshold_sensitivity(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[ThresholdSensitivityPoint, ...]:
    points: list[ThresholdSensitivityPoint] = []
    for score_delta in (Decimal("-5"), Decimal("0"), Decimal("5")):
        for confidence_delta in (Decimal("-0.05"), Decimal("0"), Decimal("0.05")):
            diagnostic = 0
            for decision in decisions:
                profile = profile_for(decision.asset_class)
                score_threshold = max(Decimal("0"), profile.minimum_score_for_buy + score_delta)
                confidence_threshold = max(
                    Decimal("0"),
                    profile.minimum_confidence_for_buy + confidence_delta,
                )
                if (
                    decision.score >= score_threshold
                    and decision.confidence >= confidence_threshold
                    and _not_structurally_blocked(decision)
                ):
                    diagnostic += 1
            points.append(
                ThresholdSensitivityPoint(
                    score_delta=score_delta,
                    confidence_delta=confidence_delta,
                    diagnostic_candidates=diagnostic,
                    trade_proposals=sum(1 for decision in decisions if decision.proposal_id),
                    simulated_executed=sum(
                        1
                        for decision in decisions
                        if decision.status is ReplayDecisionStatus.SIMULATED_EXECUTED
                    ),
                )
            )
    return tuple(points)


def build_evidence_failures(
    result: StrategyValidationResult,
    requirements: EvidenceRequirements,
) -> tuple[str, ...]:
    failures: list[str] = []
    bars_per_instrument = (result.dataset.data_digest,)
    if not bars_per_instrument:
        failures.append("dataset digest unavailable")
    if len(result.decisions) < requirements.minimum_replay_decisions:
        failures.append("minimum replay decisions not met")
    if result.metrics.trade_count < requirements.minimum_simulated_trades:
        failures.append("minimum simulated trades not met")
    oos_split = next(
        (split for split in result.period_splits if split.name.value == "OUT_OF_SAMPLE"),
        None,
    )
    if oos_split is None:
        failures.append("out-of-sample split unavailable")
    else:
        oos_trade_count = sum(
            1
            for trade in result.trades
            if oos_split.start <= trade.timestamp <= oos_split.end
            and trade.action is TradeIntent.CLOSE
        )
        if oos_trade_count < requirements.minimum_oos_trades:
            failures.append("minimum out-of-sample trades not met")
    if len(result.walk_forward_windows) < requirements.minimum_walk_forward_windows:
        failures.append("minimum walk-forward windows not met")
    return tuple(failures)


def build_research_matrix(
    result: StrategyValidationResult,
    *,
    timeframe: TimeFrame,
) -> tuple[ResearchMatrixRow, ...]:
    rows: list[ResearchMatrixRow] = []
    grouped: dict[tuple[str, AssetClass, RegimeLabel], list[ReplayDecision]] = defaultdict(list)
    for decision in result.decisions:
        for strategy_id in _STRATEGY_IDS:
            grouped[(strategy_id, decision.asset_class, decision.regime)].append(decision)
    for (strategy_id, asset_class, regime), decisions in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1].value, item[0][2].value)
    ):
        metrics = _metrics_from_decisions(tuple(decisions))
        oos = metrics
        stressed = _stress_result_label(result)
        qualification = StrategyQualificationEngine().qualify(
            metrics=metrics,
            oos_metrics=oos,
            walk_forward_passed=bool(result.walk_forward_windows),
            score_calibration_passed=result.qualification.evidence.score_calibration_passed,
            parameter_stability=result.parameter_stability,
            overfit_risk=result.overfit_risk,
            stressed_cost_passed=stressed != "FAILED",
        )
        rows.append(
            ResearchMatrixRow(
                strategy=strategy_id,
                asset_class=asset_class,
                timeframe=timeframe,
                regime=regime,
                sample_count=len(decisions),
                trade_count=metrics.trade_count,
                oos_expectancy=metrics.expectancy,
                profit_factor=metrics.profit_factor,
                sharpe_ratio=metrics.sharpe_ratio,
                maximum_drawdown=metrics.maximum_drawdown,
                stress_result=stressed,
                overfit_risk=result.overfit_risk,
                qualification=qualification.status,
                metric_provenance="forward_outcome_research",
            )
        )
    return tuple(rows)


def enrich_validation_result(
    result: StrategyValidationResult,
    *,
    requirements: EvidenceRequirements,
    timeframe: TimeFrame,
) -> StrategyValidationResult:
    failures = build_evidence_failures(result, requirements)
    return result.model_copy(
        update={
            "decision_funnel": build_decision_funnel(result.decisions, result.trades),
            "zero_trade_diagnostics": diagnose_zero_or_low_trades(result.decisions),
            "threshold_sensitivity": build_threshold_sensitivity(result.decisions),
            "research_matrix": build_research_matrix(result, timeframe=timeframe),
            "evidence_requirements": requirements,
            "evidence_passed": not failures,
            "evidence_failures": failures,
        }
    )


def _normalize_blocker(reason: str) -> str:
    lowered = reason.casefold()
    if "opportunity score" in lowered:
        return "OpportunityScore:below threshold"
    if "confidence" in lowered:
        return "Confidence:below threshold"
    if "ensemble" in lowered or "strategy" in lowered:
        return "StrategyEnsemble:defensive or non-buy"
    if "feature quality" in lowered or "data" in lowered:
        return "DataQuality:insufficient conviction"
    if "portfolio fit" in lowered:
        return "PortfolioFit:not constructive"
    if "risk" in lowered:
        return "RiskManager:rejected"
    return f"other:{reason}"


def _root_gate_blockers(decision: ReplayDecision) -> tuple[str, ...]:
    if decision.proposal_gate_trace:
        blockers: list[str] = []
        for gate in decision.proposal_gate_trace:
            if gate.get("passed") is not False:
                continue
            gate_name = gate.get("gate")
            if gate_name == "opportunity_score":
                blockers.append("OpportunityScore:below threshold")
            elif gate_name == "confidence":
                blockers.append("Confidence:below threshold")
            elif gate_name == "data_quality":
                blockers.append("DataQuality:root gate failed")
        return tuple(blockers)
    return tuple(_normalize_blocker(reason) for reason in decision.blocker_reasons)


def _not_structurally_blocked(decision: ReplayDecision) -> bool:
    blockers = " ".join(decision.blocker_reasons).casefold()
    return not any(
        marker in blockers
        for marker in (
            "feature quality",
            "portfolio fit is not constructive",
            "strategy ensemble is defensive",
        )
    )


def _metrics_from_decisions(decisions: tuple[ReplayDecision, ...]) -> PerformanceSummary:
    equity = Decimal("1")
    curve = []
    for decision in decisions:
        if decision.forward_return is not None:
            equity *= Decimal("1") + decision.forward_return
        curve.append((decision.timestamp, equity))
    return PerformanceMetricCalculator().calculate(equity_curve=tuple(curve), trades=())


def _stress_result_label(result: StrategyValidationResult) -> str:
    if not result.stress_results:
        return NOT_ENOUGH_DATA
    if all(item.passed for item in result.stress_results):
        return "PASSED"
    return "FAILED"
