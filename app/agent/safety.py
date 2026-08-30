"""Serialization boundary that keeps untrusted external text inert."""

import re
from typing import Any

from app.agent.context import AegisAgentContext

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_external_text(value: str, *, maximum_length: int = 2_000) -> str:
    """Remove control characters and cap size; content remains quoted data."""

    return _CONTROL_CHARACTERS.sub("", value)[:maximum_length]


def build_sanitized_ai_payload(context: AegisAgentContext) -> dict[str, Any]:
    """Create a primitive, secret-free DTO with external text in named data fields."""

    return {
        "analysis_timestamp": context.analysis_timestamp.isoformat(),
        "portfolio": {
            "currency": context.portfolio.currency.value,
            "cash": str(context.portfolio.cash),
            "total_value": str(context.portfolio.total_value),
            "positions": [
                {
                    "symbol": position.symbol,
                    "units": str(position.units),
                    "market_value": str(position.market_value),
                }
                for position in context.portfolio.positions
            ],
        },
        "quotes": [quote.model_dump(mode="json") for quote in context.quotes],
        "news_items": [
            {
                "source": sanitize_external_text(item.source),
                "headline_data": sanitize_external_text(item.headline),
                "summary_data": sanitize_external_text(item.summary),
                "symbols": list(item.asset_relevance),
                "sentiment": str(item.sentiment),
                "importance": str(item.importance),
                "confidence": str(item.confidence),
                "published_at": item.timestamp.isoformat(),
            }
            for item in context.news
        ],
        "allowed_symbols": [instrument.symbol for instrument in context.instruments],
        "ranked_candidates": [
            {
                "rank": candidate.rank,
                "symbol": candidate.instrument.symbol,
                "asset_class": candidate.asset_class.value,
                "state": candidate.candidate_state.value,
                "score": str(candidate.candidate_score),
                "data_quality": candidate.data_quality.value,
            }
            for candidate in context.candidates
        ],
        "opportunity_intelligence": [
            {
                "symbol": report.candidate.instrument.symbol,
                "asset_class": report.candidate.asset_class.value,
                "decision": report.decision.value,
                "opportunity_score": str(report.opportunity_score.overall_score),
                "score_band": report.opportunity_score.band.value,
                "confidence": str(report.opportunity_score.confidence),
                "market_regime": {
                    "trend": report.regime.trend.value,
                    "volatility": report.regime.volatility.value,
                    "risk_environment": report.regime.risk_environment.value,
                },
                "portfolio_fit": report.portfolio_fit.status.value,
                "data_digest": report.data_digest,
            }
            for report in context.intelligence_reports
        ],
        "hard_constraints": {
            "maximum_allocation_weight": "0.10",
            "asset_policy_gate": "required",
            "risk_manager_gate": "required",
            "execution_capability": "unavailable in agent context",
        },
    }
