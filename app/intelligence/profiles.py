"""Versioned asset-specific strategy profiles."""

from decimal import Decimal

from app.domain.enums import AssetClass
from app.domain.versions import STRATEGY_PROFILE_VERSION
from app.intelligence.confidence import (
    CONFIDENCE_MODEL_V2_B,
    CONFIDENCE_SEMANTICS_V2,
    V2_B_CALIBRATION_DATASET_ID,
    V2_B_CALIBRATION_VERSION,
    V2_B_THRESHOLD,
    V2_B_THRESHOLD_PROVENANCE,
)
from app.intelligence.models import AssetStrategyProfile, StrategyWeight

TREND_FOLLOWING_ID = "trend-following"
MOMENTUM_ID = "momentum"
BREAKOUT_ID = "breakout"
MEAN_REVERSION_ID = "mean-reversion"
DEFENSIVE_ID = "defensive"


def default_asset_strategy_profiles() -> tuple[AssetStrategyProfile, ...]:
    return legacy_v1_asset_strategy_profiles()


def legacy_v1_asset_strategy_profiles() -> tuple[AssetStrategyProfile, ...]:
    profiles: list[AssetStrategyProfile] = []
    for asset_class in AssetClass:
        if asset_class is AssetClass.EQUITY:
            profiles.append(
                AssetStrategyProfile(
                    asset_class=asset_class,
                    profile_version=STRATEGY_PROFILE_VERSION,
                    minimum_score_for_buy=Decimal("70"),
                    minimum_confidence_for_buy=Decimal("0.65"),
                    strategy_weights=(
                        StrategyWeight(strategy_id=TREND_FOLLOWING_ID, weight=Decimal("0.30")),
                        StrategyWeight(strategy_id=MOMENTUM_ID, weight=Decimal("0.25")),
                        StrategyWeight(strategy_id=BREAKOUT_ID, weight=Decimal("0.15")),
                        StrategyWeight(strategy_id=MEAN_REVERSION_ID, weight=Decimal("0.10")),
                        StrategyWeight(strategy_id=DEFENSIVE_ID, weight=Decimal("0.20")),
                    ),
                )
            )
        elif asset_class is AssetClass.ETF:
            profiles.append(
                AssetStrategyProfile(
                    asset_class=asset_class,
                    profile_version=STRATEGY_PROFILE_VERSION,
                    minimum_score_for_buy=Decimal("68"),
                    minimum_confidence_for_buy=Decimal("0.62"),
                    volatility_caution_threshold=Decimal("0.07"),
                    strategy_weights=(
                        StrategyWeight(strategy_id=TREND_FOLLOWING_ID, weight=Decimal("0.25")),
                        StrategyWeight(strategy_id=MOMENTUM_ID, weight=Decimal("0.20")),
                        StrategyWeight(strategy_id=BREAKOUT_ID, weight=Decimal("0.10")),
                        StrategyWeight(strategy_id=MEAN_REVERSION_ID, weight=Decimal("0.10")),
                        StrategyWeight(strategy_id=DEFENSIVE_ID, weight=Decimal("0.35")),
                    ),
                )
            )
        elif asset_class is AssetClass.CRYPTO:
            profiles.append(
                AssetStrategyProfile(
                    asset_class=asset_class,
                    profile_version=STRATEGY_PROFILE_VERSION,
                    # Crypto uses a lower score floor because its 24/7 market
                    # produces fewer comparable 1H setups.  The guarded
                    # confidence gate and downstream RiskManager remain
                    # unchanged, so this does not by itself authorize a trade.
                    minimum_score_for_buy=Decimal("55"),
                    # Fallback Demo calibration: the prior 0.72/0.50 gates
                    # produced no admitted crypto candidate across the
                    # observed cycles, so widen the signal floor modestly.
                    # RiskManager and execution preflight remain hard gates.
                    minimum_confidence_for_buy=Decimal("0.45"),
                    maximum_spread_for_positive_liquidity=Decimal("0.03"),
                    volatility_caution_threshold=Decimal("0.12"),
                    strong_volatility_threshold=Decimal("0.25"),
                    # Crypto tactical ensemble: give price-action signals enough
                    # influence to produce a candidate, while retaining a
                    # meaningful defensive vote.  RiskManager and preflight are
                    # downstream hard gates and are intentionally unchanged.
                    strategy_weights=(
                        StrategyWeight(strategy_id=TREND_FOLLOWING_ID, weight=Decimal("0.30")),
                        StrategyWeight(strategy_id=MOMENTUM_ID, weight=Decimal("0.30")),
                        StrategyWeight(strategy_id=BREAKOUT_ID, weight=Decimal("0.20")),
                        StrategyWeight(strategy_id=MEAN_REVERSION_ID, weight=Decimal("0.05")),
                        StrategyWeight(strategy_id=DEFENSIVE_ID, weight=Decimal("0.15")),
                    ),
                )
            )
        else:
            profiles.append(
                AssetStrategyProfile(
                    asset_class=asset_class,
                    profile_version=STRATEGY_PROFILE_VERSION,
                    enabled=False,
                    minimum_score_for_buy=Decimal("90"),
                    minimum_confidence_for_buy=Decimal("0.90"),
                    strategy_weights=(
                        StrategyWeight(strategy_id=TREND_FOLLOWING_ID, weight=Decimal("0.10")),
                        StrategyWeight(strategy_id=MOMENTUM_ID, weight=Decimal("0.10")),
                        StrategyWeight(strategy_id=BREAKOUT_ID, weight=Decimal("0.05")),
                        StrategyWeight(strategy_id=MEAN_REVERSION_ID, weight=Decimal("0.05")),
                        StrategyWeight(strategy_id=DEFENSIVE_ID, weight=Decimal("0.70")),
                    ),
                )
            )
    return tuple(profiles)


def guarded_v2b_asset_strategy_profiles() -> tuple[AssetStrategyProfile, ...]:
    return tuple(
        _promote_to_guarded_v2b(profile)
        if profile.asset_class in {AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO}
        else profile
        for profile in legacy_v1_asset_strategy_profiles()
    )


def asset_strategy_profiles_for_confidence_profile(
    confidence_profile: str,
) -> tuple[AssetStrategyProfile, ...]:
    if confidence_profile == "V1_LEGACY":
        return legacy_v1_asset_strategy_profiles()
    if confidence_profile == "V2_B_GUARDED":
        return guarded_v2b_asset_strategy_profiles()
    raise ValueError("unsupported confidence profile")


def _promote_to_guarded_v2b(profile: AssetStrategyProfile) -> AssetStrategyProfile:
    evidence_warning = {
        AssetClass.EQUITY: "EQUITY_OOS_EVIDENCE_WEAK",
        AssetClass.ETF: "ETF_OOS_EVIDENCE_BETTER",
        AssetClass.CRYPTO: "CRYPTO_OOS_EVIDENCE_BETTER",
    }[profile.asset_class]
    return profile.model_copy(
        update={
            "minimum_confidence_for_buy": V2_B_THRESHOLD,
            "confidence_model_version": CONFIDENCE_MODEL_V2_B,
            "confidence_semantics_version": CONFIDENCE_SEMANTICS_V2,
            "confidence_threshold_provenance": V2_B_THRESHOLD_PROVENANCE,
            "calibration_dataset_id": V2_B_CALIBRATION_DATASET_ID,
            "calibration_version": V2_B_CALIBRATION_VERSION,
            "validation_status": "GUARDED_EMPIRICALLY_CALIBRATED_STEP80A",
            "evidence_warning": evidence_warning,
        }
    )


def profile_for(asset_class: AssetClass) -> AssetStrategyProfile:
    return next(
        profile
        for profile in default_asset_strategy_profiles()
        if profile.asset_class is asset_class
    )


def legacy_profile_for(asset_class: AssetClass) -> AssetStrategyProfile:
    return next(
        profile
        for profile in legacy_v1_asset_strategy_profiles()
        if profile.asset_class is asset_class
    )
