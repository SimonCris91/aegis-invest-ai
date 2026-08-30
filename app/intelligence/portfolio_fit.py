"""Portfolio-aware opportunity fit and diversification scoring."""

from collections.abc import Mapping
from decimal import Decimal

from app.domain.enums import AssetClass
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import OpportunityCandidate
from app.intelligence.models import (
    CorrelationQuality,
    CorrelationResult,
    PortfolioFitAssessment,
    PortfolioFitStatus,
)


class PortfolioFitEngine:
    def evaluate(
        self,
        *,
        candidate: OpportunityCandidate,
        portfolio: PortfolioSnapshot,
        proposed_exposure: Decimal,
        correlations: tuple[CorrelationResult, ...] = (),
        position_asset_classes: Mapping[int, AssetClass] | None = None,
    ) -> PortfolioFitAssessment:
        if portfolio.total_value <= 0:
            return PortfolioFitAssessment(
                instrument=candidate.instrument,
                status=PortfolioFitStatus.BLOCKED,
                score=Decimal("0"),
                diversification_score=Decimal("0"),
                projected_cash_reserve=Decimal("0"),
                projected_concentration=Decimal("1"),
                reasons=("portfolio value is invalid",),
                correlations=correlations,
            )

        current_value = portfolio.market_value_for(candidate.instrument.numeric_instrument_id or -1)
        projected_concentration = (current_value + proposed_exposure) / portfolio.total_value
        projected_cash = max(Decimal("0"), portfolio.cash - proposed_exposure)
        projected_cash_reserve = projected_cash / portfolio.total_value
        reasons: list[str] = []
        score = Decimal("70")

        if proposed_exposure > portfolio.cash:
            reasons.append("available cash is insufficient")
            score -= Decimal("50")
        if projected_cash_reserve < Decimal("0.10"):
            reasons.append("projected cash reserve would be below policy baseline")
            score -= Decimal("30")
        if projected_concentration > Decimal("0.25"):
            reasons.append("projected instrument concentration is high")
            score -= Decimal("25")
        if portfolio.drawdown > Decimal("0.15"):
            reasons.append("portfolio drawdown is elevated")
            score -= Decimal("20")

        diversification = self._diversification_score(
            candidate=candidate,
            portfolio=portfolio,
            correlations=correlations,
            position_asset_classes=position_asset_classes or {},
        )
        score += (diversification - Decimal("50")) / Decimal("4")

        if "available cash is insufficient" in reasons or projected_cash_reserve < Decimal("0.05"):
            status = PortfolioFitStatus.BLOCKED
        elif score < Decimal("45"):
            status = PortfolioFitStatus.NEGATIVE
        elif score >= Decimal("65"):
            status = PortfolioFitStatus.POSITIVE
        else:
            status = PortfolioFitStatus.NEUTRAL

        return PortfolioFitAssessment(
            instrument=candidate.instrument,
            status=status,
            score=max(Decimal("0"), min(Decimal("100"), score)).quantize(Decimal("0.01")),
            diversification_score=diversification,
            projected_cash_reserve=projected_cash_reserve.quantize(Decimal("0.0001")),
            projected_concentration=projected_concentration.quantize(Decimal("0.0001")),
            reasons=tuple(reasons) or ("portfolio fit is acceptable",),
            correlations=correlations,
        )

    @staticmethod
    def _diversification_score(
        *,
        candidate: OpportunityCandidate,
        portfolio: PortfolioSnapshot,
        correlations: tuple[CorrelationResult, ...],
        position_asset_classes: Mapping[int, AssetClass],
    ) -> Decimal:
        if not portfolio.positions:
            return Decimal("65")
        candidate_class = candidate.asset_class
        same_class = sum(
            1
            for position in portfolio.positions
            if position_asset_classes.get(position.instrument_id) is candidate_class
        )
        score = Decimal("60") if same_class == 0 else Decimal("50")
        good_correlations = tuple(
            item
            for item in correlations
            if item.quality is CorrelationQuality.GOOD and item.correlation is not None
        )
        if good_correlations:
            max_positive = max(item.correlation or Decimal("0") for item in good_correlations)
            if max_positive >= Decimal("0.80"):
                score -= Decimal("25")
            elif max_positive >= Decimal("0.60"):
                score -= Decimal("12")
            elif max_positive <= Decimal("0.20"):
                score += Decimal("10")
        return max(Decimal("0"), min(Decimal("100"), score))
