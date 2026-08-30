"""Strategy interface intentionally isolated from execution capabilities."""

from typing import Protocol

from app.domain.market import NewsItem, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal


class StrategyEngine(Protocol):
    def propose(
        self,
        *,
        portfolio: PortfolioSnapshot,
        prices: tuple[PriceSnapshot, ...],
        news: tuple[NewsItem, ...],
    ) -> tuple[TradeProposal, ...]: ...
