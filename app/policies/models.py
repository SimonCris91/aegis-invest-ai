"""Typed asset policy models for broker-neutral opportunity screening."""

from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from app.domain.base import FrozenDomainModel
from app.domain.enums import AssetClass, MarketStatus
from app.domain.universe import CandidateState, DataQualityStatus


class RiskProfile(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    EXPERIMENTAL = "EXPERIMENTAL"


class AssetPolicy(FrozenDomainModel):
    asset_class: AssetClass
    enabled: bool = False
    long_allowed: bool = False
    short_allowed: bool = False
    leverage_allowed: bool = False
    max_leverage: Decimal = Field(default=Decimal("1"), ge=Decimal("1"))
    max_position_exposure: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    max_new_trade_exposure: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    minimum_cash_reserve: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1)
    max_daily_new_trades: int = Field(default=3, ge=0)
    minimum_confidence: Decimal = Field(default=Decimal("0.70"), ge=0, le=1)
    maximum_spread: Decimal | None = Field(default=Decimal("0.02"), ge=0)
    volatility_limit: Decimal | None = Field(default=None, ge=0)
    maximum_drawdown_budget: Decimal | None = Field(default=Decimal("0.20"), ge=0, le=1)
    require_broker_eligibility: bool = True
    require_fresh_price: bool = True
    max_price_age_seconds: int = Field(default=300, gt=0)
    actionable_market_statuses: tuple[MarketStatus, ...] = (
        MarketStatus.OPEN,
        MarketStatus.CONTINUOUS_24_7,
    )
    settlement_constraints: tuple[str, ...] = ()
    broker_capability_requirements: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_policy_shape(self) -> "AssetPolicy":
        if not self.leverage_allowed and self.max_leverage != Decimal("1"):
            raise ValueError("max_leverage must be 1 when leverage is disabled")
        if self.max_new_trade_exposure > self.max_position_exposure:
            raise ValueError("max_new_trade_exposure cannot exceed max_position_exposure")
        if self.minimum_cash_reserve + self.max_position_exposure > Decimal("1"):
            raise ValueError("cash reserve plus max position exposure cannot exceed total value")
        if not self.actionable_market_statuses:
            raise ValueError("at least one actionable market status is required")
        return self


class BrokerAssetPolicy(FrozenDomainModel):
    broker: str = Field(min_length=1)
    risk_profile: RiskProfile = RiskProfile.CONSERVATIVE
    policies: tuple[AssetPolicy, ...]
    policy_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def one_policy_per_asset_class(self) -> "BrokerAssetPolicy":
        asset_classes = [policy.asset_class for policy in self.policies]
        if len(asset_classes) != len(set(asset_classes)):
            raise ValueError("asset policies must be unique by asset class")
        return self


class PolicyDecision(FrozenDomainModel):
    asset_class: AssetClass
    allowed: bool
    candidate_state: CandidateState
    data_quality: DataQualityStatus
    reasons: tuple[str, ...] = ()
    policy_version: str = Field(min_length=1)
    policy_enabled: bool
