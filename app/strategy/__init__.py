"""Strategy boundary. Strategies produce proposals, never executable orders."""

from app.strategy.ports import StrategyEngine

__all__ = ["StrategyEngine"]
