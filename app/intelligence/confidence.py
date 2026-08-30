"""Versioned confidence models for Aegis opportunity analysis."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.intelligence.models import (
    FeatureName,
    FeatureQuality,
    MultiTimeframeFeatureSet,
    StrategyDirection,
    StrategyEnsembleResult,
)

CONFIDENCE_MODEL_V1 = "V1_LEGACY"
CONFIDENCE_MODEL_V2_B = "V2_B_GUARDED_V1"
CONFIDENCE_SEMANTICS_V1 = "MIXED_SIGNAL_AND_MARKET_QUALITY_V1"
CONFIDENCE_SEMANTICS_V2 = "SIGNAL_RELIABILITY_V2"
V2_B_CALIBRATION_DATASET_ID = "cached-etoro-step80a-1d"
V2_B_CALIBRATION_VERSION = "STEP80A_V2B_2026_08_29"
V2_B_THRESHOLD_PROVENANCE = "EMPIRICALLY_CALIBRATED_GUARDED"
V2_B_THRESHOLD = Decimal("0.5475")


class ConfidenceModel(StrEnum):
    V1_LEGACY = CONFIDENCE_MODEL_V1
    V2_B_GUARDED = CONFIDENCE_MODEL_V2_B


@dataclass(frozen=True)
class ConfidenceCalibrationProfile:
    model: ConfidenceModel
    threshold: Decimal
    threshold_provenance: str
    semantics_version: str
    calibration_dataset_id: str | None = None
    calibration_version: str | None = None
    guarded: bool = False


@dataclass(frozen=True)
class ConfidenceComputation:
    confidence: Decimal
    model_version: str
    semantics_version: str
    directional_consistency: Decimal | None = None
    mean_base_strategy_confidence: Decimal | None = None
    agreement_ratio: Decimal | None = None
    regime_confidence: Decimal | None = None
    required_feature_sufficiency: Decimal | None = None
    execution_readiness_score: Decimal | None = None
    notes: tuple[str, ...] = ()


def guarded_v2b_calibration_profile() -> ConfidenceCalibrationProfile:
    return ConfidenceCalibrationProfile(
        model=ConfidenceModel.V2_B_GUARDED,
        threshold=V2_B_THRESHOLD,
        threshold_provenance=V2_B_THRESHOLD_PROVENANCE,
        semantics_version=CONFIDENCE_SEMANTICS_V2,
        calibration_dataset_id=V2_B_CALIBRATION_DATASET_ID,
        calibration_version=V2_B_CALIBRATION_VERSION,
        guarded=True,
    )


def legacy_v1_calibration_profile(threshold: Decimal) -> ConfidenceCalibrationProfile:
    return ConfidenceCalibrationProfile(
        model=ConfidenceModel.V1_LEGACY,
        threshold=threshold,
        threshold_provenance="SAFETY_DEFAULT",
        semantics_version=CONFIDENCE_SEMANTICS_V1,
    )


def compute_opportunity_confidence(
    *,
    model: ConfidenceModel,
    ensemble: StrategyEnsembleResult,
    features: MultiTimeframeFeatureSet,
    regime_confidence: Decimal,
    data_quality_score: Decimal,
) -> ConfidenceComputation:
    if model is ConfidenceModel.V1_LEGACY:
        return compute_v1_legacy_confidence(
            ensemble_confidence=ensemble.confidence,
            regime_confidence=regime_confidence,
            data_quality_score=data_quality_score,
        )
    return compute_v2b_signal_reliability_confidence(
        strategy_directions=tuple(signal.direction for signal in ensemble.signals),
        strategy_confidences=tuple(signal.confidence for signal in ensemble.signals),
        agreement_ratio=ensemble.agreement,
        regime_confidence=regime_confidence,
        required_feature_sufficiency=required_directional_feature_sufficiency(features),
        execution_readiness_score=execution_readiness_score(features),
    )


def compute_v1_legacy_confidence(
    *,
    ensemble_confidence: Decimal,
    regime_confidence: Decimal,
    data_quality_score: Decimal,
) -> ConfidenceComputation:
    confidence = min(
        Decimal("1"),
        (ensemble_confidence + regime_confidence + (data_quality_score / Decimal("100")))
        / Decimal("3"),
    ).quantize(Decimal("0.01"))
    return ConfidenceComputation(
        confidence=confidence,
        model_version=CONFIDENCE_MODEL_V1,
        semantics_version=CONFIDENCE_SEMANTICS_V1,
        regime_confidence=regime_confidence,
        notes=("legacy formula mixes signal reliability and market-quality completeness",),
    )


def compute_v2b_signal_reliability_confidence(
    *,
    strategy_directions: tuple[StrategyDirection, ...],
    strategy_confidences: tuple[Decimal, ...],
    agreement_ratio: Decimal,
    regime_confidence: Decimal,
    required_feature_sufficiency: Decimal,
    execution_readiness_score: Decimal | None = None,
) -> ConfidenceComputation:
    mean_strategy_confidence = (
        sum(strategy_confidences, Decimal("0")) / Decimal(len(strategy_confidences))
        if strategy_confidences
        else Decimal("0")
    )
    consistency = directional_consistency(strategy_directions)
    confidence = (
        consistency * Decimal("0.30")
        + mean_strategy_confidence * Decimal("0.25")
        + agreement_ratio * Decimal("0.20")
        + regime_confidence * Decimal("0.15")
        + required_feature_sufficiency * Decimal("0.10")
    )
    return ConfidenceComputation(
        confidence=_clamp(confidence).quantize(Decimal("0.0001")),
        model_version=CONFIDENCE_MODEL_V2_B,
        semantics_version=CONFIDENCE_SEMANTICS_V2,
        directional_consistency=consistency,
        mean_base_strategy_confidence=mean_strategy_confidence.quantize(Decimal("0.0001")),
        agreement_ratio=agreement_ratio,
        regime_confidence=regime_confidence,
        required_feature_sufficiency=required_feature_sufficiency,
        execution_readiness_score=execution_readiness_score,
        notes=(
            "spread, liquidity, slippage, and execution microstructure are not folded into "
            "signal reliability confidence",
        ),
    )


def directional_consistency(directions: tuple[StrategyDirection, ...]) -> Decimal:
    positive = sum(
        1
        for direction in directions
        if direction
        in {StrategyDirection.STRONG_BUY, StrategyDirection.BUY, StrategyDirection.WATCH}
    )
    negative = sum(
        1
        for direction in directions
        if direction in {StrategyDirection.REDUCE, StrategyDirection.AVOID}
    )
    total = len(directions)
    if total <= 0:
        return Decimal("0")
    return (Decimal(max(positive - negative, 0)) / Decimal(total)).quantize(Decimal("0.0001"))


def required_directional_feature_sufficiency(features: MultiTimeframeFeatureSet) -> Decimal:
    for required in required_directional_features():
        if not _has_valid_feature(features, required):
            return Decimal("0.35")
    return Decimal("1")


def required_directional_features() -> frozenset[FeatureName]:
    return frozenset(
        {
            FeatureName.SHORT_TERM_MOMENTUM,
            FeatureName.MEDIUM_TERM_MOMENTUM,
            FeatureName.MOVING_AVERAGE_SLOPE,
            FeatureName.PRICE_VS_MOVING_AVERAGE,
            FeatureName.REALIZED_VOLATILITY,
            FeatureName.TREND_PERSISTENCE,
        }
    )


def execution_readiness_score(features: MultiTimeframeFeatureSet) -> Decimal:
    spread = features.first_value(FeatureName.SPREAD)
    liquidity = features.first_value(FeatureName.LIQUIDITY_PROXY)
    if spread is None and liquidity is None:
        return Decimal("0.45")
    score = Decimal("0.60")
    if spread is not None:
        score -= min(Decimal("0.35"), spread * Decimal("10"))
    if liquidity is not None:
        score += min(Decimal("0.20"), liquidity / Decimal("10000000"))
    return _clamp(score).quantize(Decimal("0.0001"))


def _has_valid_feature(features: MultiTimeframeFeatureSet, name: FeatureName) -> bool:
    for feature_set in features.feature_sets:
        feature = feature_set.get(name)
        if feature is not None and feature.quality is FeatureQuality.GOOD:
            return True
    return False


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))
