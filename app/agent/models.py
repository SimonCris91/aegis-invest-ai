"""Validated Aegis analysis and optional external-AI response models."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import HoldingPeriod, RecommendedAction, TradeSide
from app.domain.proposals import TradeProposal


class AegisAnalysis(FrozenDomainModel):
    timestamp: datetime
    market_assessment: str = Field(min_length=1)
    portfolio_assessment: str = Field(min_length=1)
    opportunity_summary: str = Field(min_length=1)
    risk_summary: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)
    supporting_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]
    recommended_action: RecommendedAction
    rationale: str = Field(min_length=1)
    symbol: str | None = Field(default=None, min_length=1)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")

    @model_validator(mode="after")
    def active_action_requires_symbol(self) -> "AegisAnalysis":
        if self.recommended_action is not RecommendedAction.HOLD and self.symbol is None:
            raise ValueError("an active recommendation requires a symbol")
        return self


class AegisAgentResult(FrozenDomainModel):
    analysis: AegisAnalysis
    proposal: TradeProposal | None = None

    @model_validator(mode="after")
    def proposal_matches_action(self) -> "AegisAgentResult":
        expects_proposal = self.analysis.recommended_action is not RecommendedAction.HOLD
        if expects_proposal != (self.proposal is not None):
            raise ValueError("only active recommendations may carry a TradeProposal")
        return self


class AIAnalysisResponse(FrozenDomainModel):
    """Strict schema accepted from an optional, untrusted AI provider."""

    symbol: str | None = Field(default=None, min_length=1)
    action: RecommendedAction
    confidence: Decimal = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    horizon: HoldingPeriod
    supporting_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]
    side: TradeSide | None = None
    requested_leverage: int = Field(default=1, ge=1)
    allocation_weight: Decimal = Field(default=Decimal("0"), ge=0, le=1)

    @model_validator(mode="after")
    def prohibit_unsafe_requests(self) -> "AIAnalysisResponse":
        if self.requested_leverage != 1:
            raise ValueError("AI leverage requests are prohibited")
        if self.allocation_weight > Decimal("0.10"):
            raise ValueError("AI all-in or oversized requests are prohibited")
        if self.action is RecommendedAction.HOLD:
            if self.symbol is not None or self.side is not None or self.allocation_weight != 0:
                raise ValueError("HOLD cannot contain trade instructions")
            return self
        if self.symbol is None or self.side is None or self.allocation_weight <= 0:
            raise ValueError("active AI actions require symbol, side, and allocation")
        expected_side = (
            TradeSide.BUY
            if self.action in {RecommendedAction.OPEN, RecommendedAction.INCREASE}
            else TradeSide.SELL
        )
        if self.side is not expected_side:
            raise ValueError("AI action would request short selling or an invalid direction")
        return self
