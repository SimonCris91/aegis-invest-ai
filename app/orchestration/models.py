"""Typed, secret-free result returned by each Aegis pipeline run."""

from datetime import datetime

from pydantic import Field, field_validator

from app.agent.models import AegisAnalysis
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AegisRunStatus
from app.domain.market import MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskDecision
from app.paper_trading.models import PaperExecutionResult


class AegisRunResult(FrozenDomainModel):
    timestamp: datetime
    status: AegisRunStatus
    portfolio: PortfolioSnapshot
    market_quotes: tuple[MarketQuote, ...] = ()
    news: tuple[NewsItem, ...] = ()
    agent_analysis: AegisAnalysis | None = None
    trade_proposal: TradeProposal | None = None
    risk_decision: RiskDecision | None = None
    paper_execution: PaperExecutionResult | None = None
    warnings: tuple[str, ...] = ()
    data_sources: tuple[str, ...] = ()
    real_execution_available: bool = Field(default=False)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")
