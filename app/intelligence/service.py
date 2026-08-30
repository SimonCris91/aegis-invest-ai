"""Aegis strategy and opportunity intelligence orchestration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from app.domain.enums import RecommendedAction
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import OpportunityCandidate
from app.domain.versions import STRATEGY_VERSION
from app.intelligence.confidence import ConfidenceModel
from app.intelligence.ensemble import StrategyEnsemble
from app.intelligence.features import MarketFeatureEngine
from app.intelligence.models import (
    AegisDecision,
    AegisOpportunityAnalysis,
    FeatureQuality,
    MarketBar,
    NewsSignal,
    PortfolioFitStatus,
    RiskBudgetContext,
    StrategyDirection,
    StrategyEvaluationContext,
    TimeFrame,
)
from app.intelligence.news import NewsSignalBuilder
from app.intelligence.portfolio_fit import PortfolioFitEngine
from app.intelligence.profiles import (
    asset_strategy_profiles_for_confidence_profile,
    default_asset_strategy_profiles,
)
from app.intelligence.regime import MarketRegimeEngine
from app.intelligence.scoring import OpportunityScoringEngine
from app.intelligence.strategies import (
    BreakoutStrategy,
    DefensiveStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    TrendFollowingStrategy,
)


class AegisOpportunityIntelligenceEngine:
    """Coordinates deterministic analysis only; no broker credentials or execution adapters."""

    def __init__(
        self,
        *,
        feature_engine: MarketFeatureEngine | None = None,
        regime_engine: MarketRegimeEngine | None = None,
        portfolio_fit_engine: PortfolioFitEngine | None = None,
        scoring_engine: OpportunityScoringEngine | None = None,
        ensemble: StrategyEnsemble | None = None,
        confidence_profile: str = "V1_LEGACY",
    ) -> None:
        self._feature_engine = feature_engine or MarketFeatureEngine()
        self._regime_engine = regime_engine or MarketRegimeEngine()
        self._portfolio_fit_engine = portfolio_fit_engine or PortfolioFitEngine()
        self._scoring_engine = scoring_engine or OpportunityScoringEngine()
        self._ensemble = ensemble or StrategyEnsemble(
            (
                TrendFollowingStrategy(),
                MomentumStrategy(),
                BreakoutStrategy(),
                MeanReversionStrategy(),
                DefensiveStrategy(),
            )
        )
        profiles = (
            default_asset_strategy_profiles()
            if confidence_profile == "V1_LEGACY"
            else asset_strategy_profiles_for_confidence_profile(confidence_profile)
        )
        self._profiles = {profile.asset_class: profile for profile in profiles}
        self._news = NewsSignalBuilder()

    def analyze_candidate(
        self,
        *,
        candidate: OpportunityCandidate,
        portfolio: PortfolioSnapshot,
        bars_by_timeframe: dict[TimeFrame, tuple[MarketBar, ...]],
        as_of: datetime,
        required_timeframes: tuple[TimeFrame, ...] = (TimeFrame.ONE_DAY,),
        news_signal: NewsSignal | None = None,
    ) -> AegisOpportunityAnalysis:
        profile = self._profiles[candidate.asset_class]
        features = self._feature_engine.analyze(
            bars_by_timeframe=bars_by_timeframe,
            required_timeframes=required_timeframes,
            as_of=as_of,
        )
        regime = self._regime_engine.assess(features, as_of=as_of)
        proposed_exposure = _analysis_exposure(candidate, portfolio)
        portfolio_fit = self._portfolio_fit_engine.evaluate(
            candidate=candidate,
            portfolio=portfolio,
            proposed_exposure=proposed_exposure,
        )
        risk_budget = RiskBudgetContext(
            portfolio_value=portfolio.total_value,
            available_cash=portfolio.cash,
            current_drawdown=portfolio.drawdown,
            instrument_exposure=candidate.features.current_portfolio_weight,
            daily_new_trades_remaining=3,
            data_quality=candidate.data_quality,
        )
        news_signal = news_signal or self._news.not_configured(candidate.instrument, as_of=as_of)
        evaluation_context = StrategyEvaluationContext(
            candidate=candidate,
            features=features,
            regime=regime,
            portfolio_fit=portfolio_fit,
            news_signal=news_signal,
            risk_budget=risk_budget,
            profile=profile,
        )
        ensemble = self._ensemble.evaluate(evaluation_context, as_of=as_of)
        opportunity_score = self._scoring_engine.score(
            features=features,
            regime=regime,
            ensemble=ensemble,
            portfolio_fit=portfolio_fit,
            as_of=as_of,
            news_signal=news_signal,
            confidence_model=ConfidenceModel(profile.confidence_model_version),
            confidence_threshold=profile.minimum_confidence_for_buy,
            asset_evidence_warning=profile.evidence_warning,
        )
        decision = _decision(
            profile_enabled=profile.enabled,
            ensemble_direction=ensemble.direction,
            score=opportunity_score.overall_score,
            confidence=opportunity_score.confidence,
            minimum_score=profile.minimum_score_for_buy,
            minimum_confidence=profile.minimum_confidence_for_buy,
            portfolio_fit_status=portfolio_fit.status,
            feature_quality=features.quality,
        )
        reasons = _decision_reasons(
            decision=decision,
            ensemble_direction=ensemble.direction,
            score=opportunity_score.overall_score,
            confidence=opportunity_score.confidence,
            profile_minimum_score=profile.minimum_score_for_buy,
            profile_minimum_confidence=profile.minimum_confidence_for_buy,
            feature_quality=features.quality,
            portfolio_fit_status=portfolio_fit.status,
        )
        data_digest = _data_digest(
            {
                "candidate": candidate.candidate_id,
                "symbol": candidate.instrument.symbol,
                "asset_class": candidate.asset_class.value,
                "features": tuple(
                    {
                        "timeframe": feature_set.timeframe.value,
                        "quality": feature_set.quality.value,
                        "features": tuple(
                            {
                                "name": feature.name.value,
                                "value": str(feature.value) if feature.value is not None else None,
                                "quality": feature.quality.value,
                            }
                            for feature in feature_set.features
                        ),
                    }
                    for feature_set in features.feature_sets
                ),
                "regime": regime.model_dump(mode="json"),
                "ensemble": ensemble.model_dump(mode="json"),
                "portfolio_fit": portfolio_fit.model_dump(mode="json"),
            }
        )
        return AegisOpportunityAnalysis(
            candidate=candidate,
            features=features,
            regime=regime,
            strategy_signals=ensemble.signals,
            ensemble=ensemble,
            opportunity_score=opportunity_score,
            portfolio_fit=portfolio_fit,
            news_signal=news_signal,
            risk_budget=risk_budget,
            decision=decision,
            generated_at=as_of,
            strategy_version=STRATEGY_VERSION,
            policy_version=candidate.policy_version,
            data_digest=data_digest,
            reasons=reasons,
        )


def recommended_action_for_analysis(analysis: AegisOpportunityAnalysis) -> RecommendedAction:
    if analysis.decision is AegisDecision.BUY:
        return RecommendedAction.OPEN
    if analysis.decision is AegisDecision.REDUCE:
        return RecommendedAction.REDUCE
    return RecommendedAction.HOLD


def _analysis_exposure(candidate: OpportunityCandidate, portfolio: PortfolioSnapshot) -> Decimal:
    minimum = candidate.instrument.minimum_order_value or Decimal("1")
    policy_sized = (portfolio.total_value * Decimal("0.05")).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    return max(minimum, policy_sized)


def _decision(
    *,
    profile_enabled: bool,
    ensemble_direction: StrategyDirection,
    score: Decimal,
    confidence: Decimal,
    minimum_score: Decimal,
    minimum_confidence: Decimal,
    portfolio_fit_status: PortfolioFitStatus,
    feature_quality: FeatureQuality,
) -> AegisDecision:
    if not profile_enabled:
        return AegisDecision.IGNORE
    if portfolio_fit_status is PortfolioFitStatus.BLOCKED:
        return AegisDecision.HOLD
    if feature_quality in {FeatureQuality.DATA_INSUFFICIENT, FeatureQuality.STALE}:
        return AegisDecision.HOLD
    if ensemble_direction is StrategyDirection.REDUCE:
        return AegisDecision.REDUCE
    if ensemble_direction is StrategyDirection.AVOID:
        return AegisDecision.IGNORE
    if (
        ensemble_direction in {StrategyDirection.STRONG_BUY, StrategyDirection.BUY}
        and score >= minimum_score
        and confidence >= minimum_confidence
    ):
        return AegisDecision.BUY
    return AegisDecision.HOLD


def _decision_reasons(
    *,
    decision: AegisDecision,
    ensemble_direction: StrategyDirection,
    score: Decimal,
    confidence: Decimal,
    profile_minimum_score: Decimal,
    profile_minimum_confidence: Decimal,
    feature_quality: FeatureQuality,
    portfolio_fit_status: PortfolioFitStatus,
) -> tuple[str, ...]:
    if decision is AegisDecision.BUY:
        return ("opportunity score and confidence passed deterministic thresholds",)
    reasons: list[str] = []
    if score < profile_minimum_score:
        reasons.append("opportunity score is below the asset profile threshold")
    if confidence < profile_minimum_confidence:
        reasons.append("confidence is below the asset profile threshold")
    if ensemble_direction in {StrategyDirection.AVOID, StrategyDirection.REDUCE}:
        reasons.append("strategy ensemble is defensive")
    if feature_quality is not FeatureQuality.GOOD:
        reasons.append("feature quality limits conviction")
    if portfolio_fit_status in {PortfolioFitStatus.NEGATIVE, PortfolioFitStatus.BLOCKED}:
        reasons.append("portfolio fit is not constructive")
    return tuple(reasons) or ("no trade is the deterministic result",)


def _data_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
