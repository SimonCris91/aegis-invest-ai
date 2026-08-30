"""Sanitized strategy research persistence."""

import hashlib
import json
from pathlib import Path

from app.intelligence.models import AegisOpportunityAnalysis
from app.storage.sqlite import SqliteRecordStore

DEFAULT_STRATEGY_RESEARCH_STORE_PATH = Path("work") / "strategy-research.sqlite3"


class StrategyResearchStore:
    def __init__(self, store: SqliteRecordStore) -> None:
        self._store = store

    def record_analysis(self, analysis: AegisOpportunityAnalysis) -> int:
        return self.append_sanitized_record(_record_payload(analysis))

    def append_sanitized_record(self, payload: dict[str, object]) -> int:
        return self._store.append("strategy-research", payload)

    def list_records(self) -> tuple[dict[str, object], ...]:
        return self._store.list("strategy-research")


def default_strategy_research_store(path: Path | None = None) -> StrategyResearchStore:
    return StrategyResearchStore(SqliteRecordStore(path or DEFAULT_STRATEGY_RESEARCH_STORE_PATH))


def _record_payload(analysis: AegisOpportunityAnalysis) -> dict[str, object]:
    feature_payload = tuple(
        {
            "timeframe": feature_set.timeframe.value,
            "quality": feature_set.quality.value,
            "features": tuple(
                {
                    "name": feature.name.value,
                    "value": str(feature.value) if feature.value is not None else None,
                    "quality": feature.quality.value,
                }
                for feature in feature_set.features
            ),
        }
        for feature_set in analysis.features.feature_sets
    )
    feature_digest = hashlib.sha256(
        json.dumps(feature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    reference_price = (
        analysis.candidate.quote.price if analysis.candidate.quote is not None else None
    )
    return {
        "timestamp": analysis.generated_at.isoformat(),
        "instrument": analysis.candidate.instrument.symbol,
        "asset_class": analysis.candidate.asset_class.value,
        "market_regime": {
            "trend": analysis.regime.trend.value,
            "volatility": analysis.regime.volatility.value,
            "risk_environment": analysis.regime.risk_environment.value,
            "confidence": str(analysis.regime.confidence),
        },
        "feature_digest": feature_digest,
        "strategy_signals": tuple(
            {
                "strategy_id": signal.strategy_id,
                "direction": signal.direction.value,
                "strength": str(signal.strength),
                "confidence": str(signal.confidence),
                "data_quality": signal.data_quality.value,
            }
            for signal in analysis.strategy_signals
        ),
        "opportunity_score": str(analysis.opportunity_score.overall_score),
        "score_band": analysis.opportunity_score.band.value,
        "confidence": str(analysis.opportunity_score.confidence),
        "confidence_model_version": analysis.opportunity_score.confidence_model_version,
        "confidence_semantics_version": analysis.opportunity_score.confidence_semantics_version,
        "confidence_threshold_provenance": (
            analysis.opportunity_score.confidence_threshold_provenance
        ),
        "confidence_threshold": (
            str(analysis.opportunity_score.confidence_threshold)
            if analysis.opportunity_score.confidence_threshold is not None
            else None
        ),
        "calibration_dataset_id": analysis.opportunity_score.calibration_dataset_id,
        "calibration_version": analysis.opportunity_score.calibration_version,
        "asset_evidence_warning": analysis.opportunity_score.asset_evidence_warning,
        "execution_readiness_score": (
            str(analysis.opportunity_score.execution_readiness_score)
            if analysis.opportunity_score.execution_readiness_score is not None
            else None
        ),
        "aegis_decision": analysis.decision.value,
        "reference_price": str(reference_price) if reference_price is not None else None,
        "portfolio_fit": {
            "status": analysis.portfolio_fit.status.value,
            "score": str(analysis.portfolio_fit.score),
            "diversification_score": str(analysis.portfolio_fit.diversification_score),
        },
        "strategy_version": analysis.strategy_version,
        "policy_version": analysis.policy_version,
        "data_digest": analysis.data_digest,
        "simulated_funds": True,
        "broker_write_calls": 0,
        "real_execution_available": False,
    }
