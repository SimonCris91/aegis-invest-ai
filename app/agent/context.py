"""Secret-free, immutable context exposed to Aegis Agent implementations."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.agent.exit_policy import PositionManagementState
from app.config.models import AegisStrategyConfig
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import OpportunityCandidate
from app.intelligence.models import AegisOpportunityAnalysis


class AegisAgentContext(FrozenDomainModel):
    portfolio: PortfolioSnapshot
    quotes: tuple[MarketQuote, ...]
    news: tuple[NewsItem, ...]
    instruments: tuple[InstrumentMetadata, ...]
    candidates: tuple[OpportunityCandidate, ...] = ()
    intelligence_reports: tuple[AegisOpportunityAnalysis, ...] = ()
    position_states: tuple[PositionManagementState, ...] = ()
    analysis_timestamp: datetime
    strategy: AegisStrategyConfig
    minimum_trade_amount: Decimal | None = Field(default=None, gt=0)

    @field_validator("analysis_timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "analysis_timestamp")
