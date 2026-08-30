"""Deterministic opportunity ranking for analysis prioritization."""

from decimal import Decimal

from app.domain.universe import (
    CandidateState,
    DataQualityStatus,
    OpportunityCandidate,
)


class OpportunityRankingEngine:
    def __init__(self, *, ranking_version: str) -> None:
        self._ranking_version = ranking_version

    @property
    def ranking_version(self) -> str:
        return self._ranking_version

    def rank(
        self, candidates: tuple[OpportunityCandidate, ...], *, top_n: int
    ) -> tuple[OpportunityCandidate, ...]:
        scored = tuple(
            candidate.model_copy(
                update={
                    "candidate_score": self._score(candidate),
                    "opportunity_factors": self._opportunity_factors(candidate),
                    "risk_factors": self._risk_factors(candidate),
                    "confidence": self._confidence(candidate),
                }
            )
            for candidate in candidates
            if candidate.candidate_state is CandidateState.OPEN_AND_ALLOWED
        )
        ordered = sorted(
            scored,
            key=lambda item: (
                item.candidate_score,
                item.confidence,
                item.instrument.symbol.casefold(),
            ),
            reverse=True,
        )[:top_n]
        return tuple(
            candidate.model_copy(update={"rank": rank})
            for rank, candidate in enumerate(ordered, start=1)
        )

    @staticmethod
    def _score(candidate: OpportunityCandidate) -> Decimal:
        if candidate.candidate_state is not CandidateState.OPEN_AND_ALLOWED:
            return Decimal("0")
        score = Decimal("40")
        if candidate.data_quality is DataQualityStatus.GOOD:
            score += Decimal("25")
        elif candidate.data_quality is DataQualityStatus.PARTIAL:
            score += Decimal("10")
        if candidate.features.spread_percentage is not None:
            spread_penalty = min(
                candidate.features.spread_percentage * Decimal("1000"),
                Decimal("20"),
            )
            score += max(Decimal("0"), Decimal("15") - spread_penalty)
        if candidate.features.short_term_momentum is not None:
            if candidate.features.short_term_momentum > 0:
                score += min(
                    candidate.features.short_term_momentum * Decimal("100"),
                    Decimal("10"),
                )
            else:
                score -= min(
                    abs(candidate.features.short_term_momentum) * Decimal("100"),
                    Decimal("10"),
                )
        if candidate.features.volatility is not None:
            score -= min(candidate.features.volatility * Decimal("100"), Decimal("15"))
        score -= min(candidate.features.current_portfolio_weight * Decimal("50"), Decimal("10"))
        return max(Decimal("0"), min(score.quantize(Decimal("0.01")), Decimal("100")))

    @staticmethod
    def _confidence(candidate: OpportunityCandidate) -> Decimal:
        score = OpportunityRankingEngine._score(candidate)
        quality_cap = {
            DataQualityStatus.GOOD: Decimal("0.85"),
            DataQualityStatus.PARTIAL: Decimal("0.55"),
            DataQualityStatus.STALE: Decimal("0.20"),
            DataQualityStatus.INSUFFICIENT: Decimal("0.10"),
            DataQualityStatus.CONFLICTING: Decimal("0.05"),
        }[candidate.data_quality]
        return min((score / Decimal("100")).quantize(Decimal("0.01")), quality_cap)

    @staticmethod
    def _opportunity_factors(candidate: OpportunityCandidate) -> tuple[str, ...]:
        factors = ["policy allowed", "broker eligible", "market available"]
        if candidate.features.spread_percentage is not None:
            factors.append("spread measured")
        if candidate.features.short_term_momentum is not None:
            factors.append("momentum measured")
        return tuple(factors)

    @staticmethod
    def _risk_factors(candidate: OpportunityCandidate) -> tuple[str, ...]:
        factors = ["market risk", "ranking is not a profit guarantee"]
        if candidate.features.volatility is None:
            factors.append("volatility unavailable")
        if candidate.features.news_signal == "NEWS_NOT_CONFIGURED":
            factors.append("news not configured")
        return tuple(factors)
