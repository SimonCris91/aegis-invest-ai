"""Explainable Aegis Opportunity Score."""

from datetime import datetime
from decimal import Decimal

from app.domain.versions import OPPORTUNITY_SCORE_V2_VERSION
from app.intelligence.confidence import (
    ConfidenceModel,
    compute_opportunity_confidence,
    guarded_v2b_calibration_profile,
    legacy_v1_calibration_profile,
)
from app.intelligence.models import (
    AegisOpportunityScore,
    FeatureName,
    FeatureQuality,
    MarketRegimeAssessment,
    MultiTimeframeFeatureSet,
    NewsSignal,
    NewsSignalStatus,
    PortfolioFitAssessment,
    PortfolioFitStatus,
    RegimeLabel,
    ScoreBand,
    StrategyDirection,
    StrategyEnsembleResult,
)


class OpportunityScoringEngine:
    def __init__(
        self,
        *,
        score_version: str = OPPORTUNITY_SCORE_V2_VERSION,
        confidence_model: ConfidenceModel = ConfidenceModel.V1_LEGACY,
    ) -> None:
        self._score_version = score_version
        self._confidence_model = confidence_model

    @property
    def score_version(self) -> str:
        return self._score_version

    @property
    def confidence_model(self) -> ConfidenceModel:
        return self._confidence_model

    def score(
        self,
        *,
        features: MultiTimeframeFeatureSet,
        regime: MarketRegimeAssessment,
        ensemble: StrategyEnsembleResult,
        portfolio_fit: PortfolioFitAssessment,
        as_of: datetime,
        news_signal: NewsSignal | None = None,
        confidence_model: ConfidenceModel | None = None,
        confidence_threshold: Decimal | None = None,
        asset_evidence_warning: str | None = None,
    ) -> AegisOpportunityScore:
        trend_score = _feature_score(features, FeatureName.PRICE_VS_MOVING_AVERAGE)
        momentum_score = _feature_score(features, FeatureName.MEDIUM_TERM_MOMENTUM)
        regime_score = _regime_score(regime)
        liquidity_score = _liquidity_score(features)
        data_quality_score = _quality_score(features.quality)
        portfolio_score = portfolio_fit.score
        news_score = _news_score(news_signal)
        event_risk_penalty = _event_risk_penalty(news_signal)
        risk_adjusted = _risk_adjusted_score(features, regime, portfolio_fit)
        risk_penalty = _risk_penalty(features, regime, portfolio_fit) + event_risk_penalty
        directional_bonus = _directional_bonus(ensemble.direction) * ensemble.confidence
        gross = (
            trend_score * Decimal("0.18")
            + momentum_score * Decimal("0.18")
            + regime_score * Decimal("0.14")
            + risk_adjusted * Decimal("0.16")
            + liquidity_score * Decimal("0.08")
            + portfolio_score * Decimal("0.13")
            + data_quality_score * Decimal("0.10")
            + news_score * Decimal("0.03")
            + directional_bonus
        )
        overall = max(Decimal("0"), min(Decimal("100"), gross - risk_penalty)).quantize(
            Decimal("0.01")
        )
        active_model = confidence_model or self._confidence_model
        confidence_result = compute_opportunity_confidence(
            model=active_model,
            ensemble=ensemble,
            features=features,
            regime_confidence=regime.confidence,
            data_quality_score=data_quality_score,
        )
        calibration_profile = (
            guarded_v2b_calibration_profile()
            if active_model is ConfidenceModel.V2_B_GUARDED
            else legacy_v1_calibration_profile(confidence_threshold or Decimal("0.65"))
        )
        return AegisOpportunityScore(
            instrument=features.instrument,
            timestamp=as_of,
            overall_score=overall,
            band=score_band(overall),
            trend_score=trend_score,
            momentum_score=momentum_score,
            regime_score=regime_score,
            risk_adjusted_score=risk_adjusted,
            liquidity_score=liquidity_score,
            portfolio_fit_score=portfolio_score,
            data_quality_score=data_quality_score,
            news_score=news_score,
            event_risk_penalty=event_risk_penalty,
            risk_penalty=risk_penalty,
            confidence=confidence_result.confidence,
            signal_reliability_confidence=(
                confidence_result.confidence
                if active_model is ConfidenceModel.V2_B_GUARDED
                else None
            ),
            execution_readiness_score=confidence_result.execution_readiness_score,
            confidence_model_version=confidence_result.model_version,
            confidence_semantics_version=confidence_result.semantics_version,
            confidence_threshold_provenance=calibration_profile.threshold_provenance,
            confidence_threshold=calibration_profile.threshold,
            calibration_dataset_id=calibration_profile.calibration_dataset_id,
            calibration_version=calibration_profile.calibration_version,
            asset_evidence_warning=asset_evidence_warning,
            score_version=self._score_version,
            components=(
                "trend",
                "momentum",
                "regime",
                "risk_adjusted",
                "liquidity",
                "portfolio_fit",
                "data_quality",
                "news",
                "event_risk",
            ),
        )


def score_band(score: Decimal) -> ScoreBand:
    if score >= Decimal("90"):
        return ScoreBand.EXCEPTIONAL
    if score >= Decimal("80"):
        return ScoreBand.VERY_STRONG
    if score >= Decimal("70"):
        return ScoreBand.STRONG
    if score >= Decimal("60"):
        return ScoreBand.INTERESTING
    if score >= Decimal("50"):
        return ScoreBand.NEUTRAL
    if score >= Decimal("40"):
        return ScoreBand.WEAK
    return ScoreBand.AVOID


def _feature_score(features: MultiTimeframeFeatureSet, name: FeatureName) -> Decimal:
    value = None
    for feature_set in features.feature_sets:
        value = feature_set.normalized(name)
        if value is not None:
            break
    if value is None:
        return Decimal("35")
    return max(Decimal("0"), min(Decimal("100"), Decimal("50") + value * Decimal("50")))


def _regime_score(regime: MarketRegimeAssessment) -> Decimal:
    score = Decimal("50")
    if regime.trend is RegimeLabel.STRONG_UPTREND:
        score += Decimal("25")
    elif regime.trend is RegimeLabel.UPTREND:
        score += Decimal("15")
    elif regime.trend in {RegimeLabel.DOWNTREND, RegimeLabel.STRONG_DOWNTREND}:
        score -= Decimal("25")
    if regime.volatility is RegimeLabel.HIGH_VOLATILITY:
        score -= Decimal("20")
    elif regime.volatility is RegimeLabel.LOW_VOLATILITY:
        score += Decimal("10")
    if regime.risk_environment is RegimeLabel.RISK_OFF:
        score -= Decimal("20")
    elif regime.risk_environment is RegimeLabel.RISK_ON:
        score += Decimal("10")
    return max(Decimal("0"), min(Decimal("100"), score))


def _liquidity_score(features: MultiTimeframeFeatureSet) -> Decimal:
    spread = features.first_value(FeatureName.SPREAD)
    liquidity = features.first_value(FeatureName.LIQUIDITY_PROXY)
    if spread is None and liquidity is None:
        return Decimal("45")
    score = Decimal("60")
    if spread is not None:
        score -= min(Decimal("35"), spread * Decimal("1000"))
    if liquidity is not None:
        score += min(Decimal("20"), liquidity / Decimal("100000"))
    return max(Decimal("0"), min(Decimal("100"), score))


def _quality_score(quality: FeatureQuality) -> Decimal:
    return {
        FeatureQuality.GOOD: Decimal("90"),
        FeatureQuality.PARTIAL: Decimal("65"),
        FeatureQuality.DATA_INSUFFICIENT: Decimal("20"),
        FeatureQuality.UNKNOWN: Decimal("10"),
        FeatureQuality.STALE: Decimal("10"),
        FeatureQuality.CONFLICTING: Decimal("20"),
    }[quality]


def _news_score(news_signal: NewsSignal | None) -> Decimal:
    if news_signal is None or news_signal.status is NewsSignalStatus.NEWS_NOT_CONFIGURED:
        return Decimal("50")
    if news_signal.sentiment is None:
        return Decimal("50")
    impact = news_signal.impact or Decimal("0.50")
    confidence = news_signal.confidence
    bounded = Decimal("50") + news_signal.sentiment * Decimal("20") * impact * confidence
    return max(Decimal("0"), min(Decimal("100"), bounded))


def _event_risk_penalty(news_signal: NewsSignal | None) -> Decimal:
    if news_signal is None or not news_signal.event_risks:
        return Decimal("0")
    strongest = max(event.severity * event.confidence for event in news_signal.event_risks)
    return min(Decimal("20"), strongest * Decimal("20"))


def _risk_adjusted_score(
    features: MultiTimeframeFeatureSet,
    regime: MarketRegimeAssessment,
    portfolio_fit: PortfolioFitAssessment,
) -> Decimal:
    score = Decimal("70")
    volatility = features.first_value(FeatureName.REALIZED_VOLATILITY)
    drawdown = features.first_value(FeatureName.DRAWDOWN)
    if volatility is not None:
        score -= min(Decimal("30"), volatility * Decimal("200"))
    if drawdown is not None:
        score -= min(Decimal("25"), drawdown * Decimal("100"))
    if regime.risk_environment is RegimeLabel.RISK_OFF:
        score -= Decimal("20")
    if portfolio_fit.status is PortfolioFitStatus.POSITIVE:
        score += Decimal("10")
    elif portfolio_fit.status is PortfolioFitStatus.NEGATIVE:
        score -= Decimal("20")
    elif portfolio_fit.status is PortfolioFitStatus.BLOCKED:
        score -= Decimal("45")
    return max(Decimal("0"), min(Decimal("100"), score))


def _risk_penalty(
    features: MultiTimeframeFeatureSet,
    regime: MarketRegimeAssessment,
    portfolio_fit: PortfolioFitAssessment,
) -> Decimal:
    penalty = Decimal("0")
    volatility = features.first_value(FeatureName.REALIZED_VOLATILITY)
    spread = features.first_value(FeatureName.SPREAD)
    if volatility is not None and volatility >= Decimal("0.10"):
        penalty += Decimal("12")
    if spread is not None and spread >= Decimal("0.03"):
        penalty += Decimal("8")
    if regime.volatility is RegimeLabel.HIGH_VOLATILITY:
        penalty += Decimal("10")
    if portfolio_fit.status is PortfolioFitStatus.BLOCKED:
        penalty += Decimal("35")
    elif portfolio_fit.status is PortfolioFitStatus.NEGATIVE:
        penalty += Decimal("15")
    return min(Decimal("100"), penalty)


def _directional_bonus(direction: StrategyDirection) -> Decimal:
    return {
        StrategyDirection.STRONG_BUY: Decimal("12"),
        StrategyDirection.BUY: Decimal("8"),
        StrategyDirection.WATCH: Decimal("2"),
        StrategyDirection.HOLD: Decimal("0"),
        StrategyDirection.REDUCE: Decimal("-8"),
        StrategyDirection.AVOID: Decimal("-12"),
    }[direction]
