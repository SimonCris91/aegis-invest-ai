"""Forward outcome tracking for strategy research without look-ahead bias."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from app.data.models import CandidateObservation, ResearchLabel, ResearchOutcomeSnapshot
from app.intelligence.models import AegisOpportunityAnalysis, TimeFrame
from app.storage.sqlite import SqliteRecordStore


class ResearchOutcomeTracker:
    def __init__(self, store: SqliteRecordStore) -> None:
        self._store = store

    def record_observation(self, analysis: AegisOpportunityAnalysis) -> CandidateObservation:
        observation_id = hashlib.sha256(
            "|".join((analysis.data_digest, analysis.generated_at.isoformat())).encode("utf-8")
        ).hexdigest()
        observation = CandidateObservation(
            observation_id=observation_id,
            analysis=analysis,
            recorded_at=analysis.generated_at,
        )
        self._store.append(
            "strategy-research-observation",
            {
                "observation_id": observation_id,
                "timestamp": observation.recorded_at.isoformat(),
                "symbol": analysis.candidate.instrument.symbol,
                "score": str(analysis.opportunity_score.overall_score),
                "confidence": str(analysis.opportunity_score.confidence),
                "score_band": analysis.opportunity_score.band.value,
                "decision": analysis.decision.value,
                "data_digest": analysis.data_digest,
                "strategy_version": analysis.strategy_version,
                "score_version": analysis.opportunity_score.score_version,
            },
        )
        return observation

    def record_outcome(
        self,
        *,
        observation: CandidateObservation,
        observed_at: datetime,
        horizon: TimeFrame,
        future_prices: tuple[Decimal, ...],
        ranking_percentile: Decimal | None = None,
    ) -> ResearchOutcomeSnapshot:
        if observed_at <= observation.recorded_at:
            raise ValueError("research outcomes must be observed after the T0 decision")
        quote = observation.analysis.candidate.quote
        if quote is None:
            raise ValueError("research outcome requires a T0 reference quote")
        reference_price = quote.price
        if not future_prices:
            label = ResearchLabel.INSUFFICIENT_HORIZON
            future_price = reference_price
            mfe = Decimal("0")
            mae = Decimal("0")
            drawdown = Decimal("0")
        else:
            future_price = future_prices[-1]
            returns = tuple((price - reference_price) / reference_price for price in future_prices)
            mfe = max(returns)
            mae = min(returns)
            drawdown = abs(min(Decimal("0"), mae))
            label = _label(final_return=returns[-1], drawdown=drawdown)
        snapshot = ResearchOutcomeSnapshot(
            observation_id=observation.observation_id,
            observed_at=observed_at,
            horizon=horizon,
            reference_price=reference_price,
            future_price=future_price,
            maximum_favorable_excursion=mfe,
            maximum_adverse_excursion=mae,
            drawdown=drawdown,
            ranking_percentile=ranking_percentile,
            label=label,
        )
        self._store.append(
            "strategy-research-outcome",
            json.loads(snapshot.model_dump_json()),
        )
        return snapshot


def _label(*, final_return: Decimal, drawdown: Decimal) -> ResearchLabel:
    if final_return >= Decimal("0.02") and drawdown <= Decimal("0.03"):
        return ResearchLabel.GOOD_SIGNAL
    if final_return <= Decimal("-0.02") or drawdown >= Decimal("0.08"):
        return ResearchLabel.BAD_SIGNAL
    return ResearchLabel.NEUTRAL
