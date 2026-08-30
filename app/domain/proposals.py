"""Structured strategy output accepted from an AI or deterministic strategy."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import (
    AssetClass,
    Currency,
    HoldingPeriod,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem


class TradeProposal(FrozenDomainModel):
    """An untrusted proposal. It is never executable on its own."""

    proposal_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=128)
    created_at: datetime
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    asset_class: AssetClass = AssetClass.UNKNOWN
    side: TradeSide
    intent: TradeIntent
    amount: Decimal = Field(gt=0)
    currency: Currency
    target_weight: Decimal = Field(ge=0, le=1)
    current_weight: Decimal = Field(ge=0, le=1)
    leverage: int = Field(default=1, ge=1)
    settlement_type: SettlementType
    reason: str = Field(min_length=1)
    evidence: tuple[EvidenceItem, ...]
    confidence: Decimal = Field(ge=0, le=1)
    confidence_model_version: str = Field(default="V1_LEGACY", min_length=1)
    confidence_semantics_version: str = Field(
        default="MIXED_SIGNAL_AND_MARKET_QUALITY_V1", min_length=1
    )
    confidence_threshold: Decimal | None = Field(default=None, ge=0, le=1)
    confidence_threshold_provenance: str | None = Field(default=None, min_length=1)
    risk_factors: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    expected_holding_period: HoldingPeriod
    is_martingale: bool = False
    is_averaging_down: bool = False
    thesis_revalidated: bool = False

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "created_at")
