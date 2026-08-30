"""Live data intelligence pipeline over Step 7.7 shortlisted candidates."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.data.models import (
    DataProviderStatus,
    EventRiskAssessment,
    HistoricalDataQualityStatus,
    NewsProviderResult,
)
from app.data.registry import (
    EventRiskProviderRegistry,
    HistoricalDataProviderRegistry,
    NewsProviderRegistry,
)
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import OpportunityCandidate
from app.intelligence.models import (
    AegisOpportunityAnalysis,
    EventRiskFlag,
    EventRiskType,
    NewsSignal,
    NewsSignalStatus,
    TimeFrame,
)
from app.intelligence.research import StrategyResearchStore
from app.intelligence.service import AegisOpportunityIntelligenceEngine


class LiveCandidateIntelligence(FrozenDomainModel):
    candidate: OpportunityCandidate
    status: DataProviderStatus
    analysis: AegisOpportunityAnalysis | None = None
    historical_status: DataProviderStatus
    news_status: DataProviderStatus
    event_status: DataProviderStatus
    reasons: tuple[str, ...] = ()


class LiveIntelligenceRun(FrozenDomainModel):
    as_of: datetime
    universe_discovered: int = Field(ge=0)
    candidates_input: int = Field(ge=0)
    deep_analyzed: int = Field(ge=0)
    results: tuple[LiveCandidateIntelligence, ...]
    broker_write_calls: int = Field(default=0, ge=0, le=0)
    demo_execution_enabled: bool = False
    real_execution_available: bool = False

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "live intelligence timestamp")


class LiveMarketIntelligencePipeline:
    def __init__(
        self,
        *,
        historical_registry: HistoricalDataProviderRegistry,
        news_registry: NewsProviderRegistry,
        event_registry: EventRiskProviderRegistry,
        intelligence_engine: AegisOpportunityIntelligenceEngine | None = None,
        research_store: StrategyResearchStore | None = None,
    ) -> None:
        self._historical = historical_registry
        self._news = news_registry
        self._events = event_registry
        self._engine = intelligence_engine or AegisOpportunityIntelligenceEngine()
        self._research_store = research_store

    def run(
        self,
        *,
        candidates: tuple[OpportunityCandidate, ...],
        portfolio: PortfolioSnapshot,
        as_of: datetime,
        required_timeframes: tuple[TimeFrame, ...],
        top_n: int,
        historical_limit: int = 120,
    ) -> LiveIntelligenceRun:
        selected = candidates[:top_n]
        results: list[LiveCandidateIntelligence] = []
        for candidate in selected:
            historical = self._historical.fetch(
                instrument=candidate.instrument,
                timeframes=required_timeframes,
                as_of=as_of,
                limit=historical_limit,
            )
            news = self._news.fetch(candidate.instrument, as_of=as_of)
            events = self._events.assess(candidate.instrument, as_of=as_of)
            if historical.status is not DataProviderStatus.SUCCESS:
                results.append(
                    LiveCandidateIntelligence(
                        candidate=candidate,
                        status=historical.status,
                        historical_status=historical.status,
                        news_status=news.status,
                        event_status=events.status,
                        reasons=historical.reasons,
                    )
                )
                continue
            news_signal = _news_signal(candidate, news, events, as_of=as_of)
            analysis = self._engine.analyze_candidate(
                candidate=candidate,
                portfolio=portfolio,
                bars_by_timeframe=historical.bars_by_timeframe,
                as_of=as_of,
                required_timeframes=required_timeframes,
                news_signal=news_signal,
            )
            if self._research_store is not None:
                self._research_store.record_analysis(analysis)
            results.append(
                LiveCandidateIntelligence(
                    candidate=candidate,
                    status=DataProviderStatus.SUCCESS,
                    analysis=analysis,
                    historical_status=historical.status,
                    news_status=news.status,
                    event_status=events.status,
                    reasons=(),
                )
            )
        return LiveIntelligenceRun(
            as_of=as_of,
            universe_discovered=len(candidates),
            candidates_input=len(selected),
            deep_analyzed=sum(1 for item in results if item.analysis is not None),
            results=tuple(results),
            broker_write_calls=0,
            demo_execution_enabled=False,
            real_execution_available=False,
        )


def _news_signal(
    candidate: OpportunityCandidate,
    news: NewsProviderResult,
    events: EventRiskAssessment,
    *,
    as_of: datetime,
) -> NewsSignal:
    if not news.items and not events.events:
        return NewsSignal(
            instrument=candidate.instrument,
            timestamp=as_of,
            status=NewsSignalStatus.NEWS_NOT_CONFIGURED,
            sentiment=None,
            impact=None,
            confidence=Decimal("0"),
            source_quality=Decimal("0"),
            event_risks=(),
        )
    sentiment_values = tuple(
        item.sentiment_score for item in news.items if item.sentiment_score is not None
    )
    sentiment = (
        sum(sentiment_values, Decimal("0")) / Decimal(len(sentiment_values))
        if sentiment_values
        else None
    )
    impact = max((item.relevance for item in news.items), default=Decimal("0"))
    event_flags = tuple(
        EventRiskFlag(
            event_type=_event_type(event.category.value),
            timestamp=event.scheduled_at or as_of,
            severity=event.severity,
            confidence=event.confidence,
            source=event.source,
            description=event.description,
        )
        for event in events.events
    )
    status = (
        NewsSignalStatus.AVAILABLE
        if news.status is DataProviderStatus.SUCCESS
        else NewsSignalStatus.DATA_INSUFFICIENT
    )
    source_quality = {
        HistoricalDataQualityStatus.GOOD: Decimal("0.80"),
        HistoricalDataQualityStatus.PARTIAL: Decimal("0.50"),
        HistoricalDataQualityStatus.DEGRADED: Decimal("0.30"),
        HistoricalDataQualityStatus.STALE: Decimal("0.20"),
        HistoricalDataQualityStatus.INSUFFICIENT: Decimal("0.10"),
        HistoricalDataQualityStatus.CONFLICTING: Decimal("0.05"),
    }[news.quality]
    confidence = max(
        (item.confidence for item in news.items),
        default=Decimal("0.20") if event_flags else Decimal("0"),
    )
    return NewsSignal(
        instrument=candidate.instrument,
        timestamp=as_of,
        status=status,
        sentiment=sentiment,
        impact=impact or None,
        confidence=confidence,
        source_quality=source_quality,
        event_risks=event_flags,
    )


def _event_type(value: str) -> EventRiskType:
    mapping = {
        "EARNINGS": EventRiskType.EARNINGS,
        "MACRO_EVENT": EventRiskType.MACRO_EVENT,
        "REGULATORY": EventRiskType.REGULATORY_EVENT,
        "TOKEN_EVENT": EventRiskType.TOKEN_SPECIFIC_EVENT,
        "LISTING_DELISTING": EventRiskType.REGULATORY_EVENT,
    }
    return mapping.get(value, EventRiskType.UNKNOWN_MAJOR_EVENT)
