"""Overfit diagnostics, Monte Carlo research, and strategy qualification."""

import random
from decimal import Decimal

from app.validation.metrics import NOT_ENOUGH_DATA
from app.validation.models import (
    MonteCarloResult,
    OverfitRisk,
    ParameterStability,
    PerformanceSummary,
    QualificationEvidence,
    StrategyQualification,
    StrategyQualificationStatus,
)


class ParameterStabilityAnalyzer:
    def classify(self, metric_values: tuple[Decimal, ...]) -> ParameterStability:
        if len(metric_values) < 2:
            return ParameterStability.MODERATE
        spread = max(metric_values) - min(metric_values)
        if spread >= Decimal("0.25"):
            return ParameterStability.PARAMETER_FRAGILE
        if spread >= Decimal("0.10"):
            return ParameterStability.MODERATE
        return ParameterStability.STABLE


class OverfitRiskAnalyzer:
    def classify(
        self,
        *,
        trade_count: int,
        train_return: Decimal | str,
        oos_return: Decimal | str,
        max_single_instrument_share: Decimal,
        max_single_regime_share: Decimal,
    ) -> OverfitRisk:
        flags = 0
        if trade_count < 10:
            flags += 1
        if isinstance(train_return, Decimal) and isinstance(oos_return, Decimal):
            if train_return - oos_return > Decimal("0.20"):
                flags += 1
        else:
            flags += 1
        if max_single_instrument_share > Decimal("0.70"):
            flags += 1
        if max_single_regime_share > Decimal("0.70"):
            flags += 1
        if flags >= 3:
            return OverfitRisk.OVERFIT_RISK_HIGH
        if flags >= 1:
            return OverfitRisk.OVERFIT_RISK_MEDIUM
        return OverfitRisk.OVERFIT_RISK_LOW


class MonteCarloTradeSequence:
    def run(
        self,
        outcomes: tuple[Decimal, ...],
        *,
        seed: int,
        iterations: int = 200,
        sample_provenance: str = "trade_returns",
    ) -> MonteCarloResult:
        if iterations <= 0:
            raise ValueError("iterations must be positive")
        if len(outcomes) < 5:
            return MonteCarloResult(
                seed=seed,
                iterations=iterations,
                sample_provenance=sample_provenance,
                median_return=NOT_ENOUGH_DATA,
                worst_drawdown=NOT_ENOUGH_DATA,
                longest_loss_streak=0,
                risk_of_large_decline=NOT_ENOUGH_DATA,
            )
        rng = random.Random(seed)
        returns: list[Decimal] = []
        drawdowns: list[Decimal] = []
        longest_streak = 0
        large_declines = 0
        for _ in range(iterations):
            shuffled = list(outcomes)
            rng.shuffle(shuffled)
            equity = Decimal("1")
            peak = equity
            worst_drawdown = Decimal("0")
            current_streak = 0
            for outcome in shuffled:
                equity *= Decimal("1") + outcome
                peak = max(peak, equity)
                worst_drawdown = max(worst_drawdown, (peak - equity) / peak)
                current_streak = current_streak + 1 if outcome < 0 else 0
                longest_streak = max(longest_streak, current_streak)
            returns.append(equity - Decimal("1"))
            drawdowns.append(worst_drawdown)
            if worst_drawdown >= Decimal("0.20"):
                large_declines += 1
        return MonteCarloResult(
            seed=seed,
            iterations=iterations,
            sample_provenance=sample_provenance,
            median_return=_median(tuple(returns)),
            worst_drawdown=max(drawdowns),
            longest_loss_streak=longest_streak,
            risk_of_large_decline=Decimal(large_declines) / Decimal(iterations),
        )


class StrategyQualificationEngine:
    def qualify(
        self,
        *,
        metrics: PerformanceSummary,
        oos_metrics: PerformanceSummary,
        walk_forward_passed: bool,
        score_calibration_passed: bool,
        parameter_stability: ParameterStability,
        overfit_risk: OverfitRisk,
        stressed_cost_passed: bool,
    ) -> StrategyQualification:
        minimum_trade_count_passed = metrics.trade_count >= 10
        positive_expectancy = isinstance(metrics.expectancy, Decimal) and metrics.expectancy > 0
        profit_factor_passed = isinstance(
            metrics.profit_factor, Decimal
        ) and metrics.profit_factor >= Decimal("1.2")
        drawdown_passed = isinstance(
            metrics.maximum_drawdown, Decimal
        ) and metrics.maximum_drawdown <= Decimal("0.25")
        oos_available = oos_metrics.trade_count > 0 and not isinstance(
            oos_metrics.total_return, str
        )
        reasons: list[str] = []
        if not minimum_trade_count_passed:
            reasons.append("minimum trade count not met")
        if not oos_available:
            reasons.append("out-of-sample result unavailable")
        if overfit_risk is OverfitRisk.OVERFIT_RISK_HIGH:
            reasons.append("high overfit risk")
        evidence = QualificationEvidence(
            minimum_trade_count_passed=minimum_trade_count_passed,
            oos_available=oos_available,
            positive_expectancy=positive_expectancy,
            profit_factor_passed=profit_factor_passed,
            drawdown_passed=drawdown_passed,
            walk_forward_passed=walk_forward_passed,
            score_calibration_passed=score_calibration_passed,
            parameter_stability=parameter_stability,
            overfit_risk=overfit_risk,
            stressed_cost_passed=stressed_cost_passed,
            reasons=tuple(reasons),
        )
        status = _qualification_status(evidence)
        return StrategyQualification(
            status=status,
            evidence=evidence,
            shadow_feed_eligible=status
            in {
                StrategyQualificationStatus.SHADOW_ELIGIBLE,
                StrategyQualificationStatus.DEMO_ELIGIBLE,
            },
            demo_consideration_allowed=status is StrategyQualificationStatus.DEMO_ELIGIBLE,
        )


def _qualification_status(evidence: QualificationEvidence) -> StrategyQualificationStatus:
    if not evidence.minimum_trade_count_passed:
        return StrategyQualificationStatus.INSUFFICIENT_DATA
    if evidence.overfit_risk is OverfitRisk.OVERFIT_RISK_HIGH:
        return StrategyQualificationStatus.REJECTED
    if not evidence.positive_expectancy or not evidence.profit_factor_passed:
        return StrategyQualificationStatus.RESEARCH_ONLY
    if not evidence.drawdown_passed:
        return StrategyQualificationStatus.REJECTED
    if evidence.oos_available and evidence.walk_forward_passed and evidence.stressed_cost_passed:
        if (
            evidence.score_calibration_passed
            and evidence.parameter_stability is not ParameterStability.PARAMETER_FRAGILE
        ):
            return StrategyQualificationStatus.DEMO_ELIGIBLE
        return StrategyQualificationStatus.SHADOW_ELIGIBLE
    return StrategyQualificationStatus.PROMISING


def _median(values: tuple[Decimal, ...]) -> Decimal | str:
    if not values:
        return NOT_ENOUGH_DATA
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")
