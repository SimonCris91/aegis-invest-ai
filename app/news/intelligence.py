"""Broker-neutral global news intelligence foundation."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator

from app.agent.safety import sanitize_external_text
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass
from app.domain.universe import UniversalInstrument

NEWS_INTELLIGENCE_VERSION = "global-news-intelligence-v3"
NEWS_FRESH_MAX_AGE = timedelta(hours=6)


class NewsSentiment(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"


class NewsImpactHorizon(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    INTRADAY = "INTRADAY"
    MULTIDAY = "MULTIDAY"
    LONG_TERM = "LONG_TERM"


class NewsFreshnessStatus(StrEnum):
    NEWS_FRESH = "NEWS_FRESH"
    NEWS_DELAYED = "NEWS_DELAYED"
    NEWS_STALE = "NEWS_STALE"
    NEWS_SOURCE_UNAVAILABLE = "NEWS_SOURCE_UNAVAILABLE"


class NewsProviderStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    DELAYED = "DELAYED"


class NewsProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: NewsProviderStatus,
        http_status: int | None = None,
        sanitized_endpoint: str | None = None,
        provider_error_message: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status
        self.sanitized_endpoint = sanitized_endpoint
        self.provider_error_message = provider_error_message
        self.retry_after = retry_after[:80] if retry_after is not None else None

    def safe_diagnostics(self) -> dict[str, object]:
        diagnostics = {
            "status": self.status.value,
            "http_status": self.http_status,
            "sanitized_endpoint": self.sanitized_endpoint,
            "provider_error_message": self.provider_error_message,
        }
        if self.retry_after is not None:
            diagnostics["retry_after"] = self.retry_after
        return diagnostics


class NewsSourceQuality(StrEnum):
    PRIMARY_OFFICIAL = "PRIMARY_OFFICIAL"
    MAJOR_FINANCIAL_NEWS = "MAJOR_FINANCIAL_NEWS"
    COMPANY_RELEASE = "COMPANY_RELEASE"
    REGULATORY_GOVERNMENT = "REGULATORY_GOVERNMENT"
    SECONDARY_MEDIA = "SECONDARY_MEDIA"
    UNKNOWN_LOW_CONFIDENCE = "UNKNOWN_LOW_CONFIDENCE"


class GlobalNewsEventCategory(StrEnum):
    CENTRAL_BANK = "CENTRAL_BANK"
    INTEREST_RATES = "INTEREST_RATES"
    INFLATION = "INFLATION"
    EMPLOYMENT = "EMPLOYMENT"
    MACRO = "MACRO"
    EARNINGS = "EARNINGS"
    COMPANY_GUIDANCE = "COMPANY_GUIDANCE"
    MERGER_ACQUISITION = "MERGER_ACQUISITION"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    REGULATION = "REGULATION"
    LEGAL = "LEGAL"
    GEOPOLITICS = "GEOPOLITICS"
    ENERGY = "ENERGY"
    COMMODITIES = "COMMODITIES"
    CRYPTO = "CRYPTO"
    CYBER_SECURITY = "CYBER_SECURITY"
    SUPPLY_CHAIN = "SUPPLY_CHAIN"
    NATURAL_DISASTER = "NATURAL_DISASTER"
    MARKET_STRESS = "MARKET_STRESS"
    OTHER = "OTHER"


class RawNewsItem(FrozenDomainModel):
    headline: str = Field(min_length=1)
    source: str = Field(min_length=1)
    published_at: datetime
    url_or_reference: str | None = Field(default=None, min_length=1)
    language: str = Field(default="en", min_length=2)
    geographic_scope: str = Field(default="GLOBAL", min_length=1)
    summary: str | None = Field(default=None, min_length=1)
    provider_symbols: tuple[str, ...] = ()
    source_quality: NewsSourceQuality = NewsSourceQuality.UNKNOWN_LOW_CONFIDENCE
    provider: str = Field(default="unknown", min_length=1)

    @field_validator("published_at")
    @classmethod
    def published_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "news publication timestamp")


class NewsAssetLink(FrozenDomainModel):
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    reason: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)


class NormalizedGlobalNewsEvent(FrozenDomainModel):
    event_id: str = Field(min_length=16)
    headline: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_type: NewsSourceQuality
    source_url_reference: str | None = Field(default=None, min_length=1)
    published_at: datetime
    first_seen_at: datetime
    language: str = Field(min_length=2)
    geographic_scope: str = Field(min_length=1)
    named_entities: tuple[str, ...] = ()
    companies_assets_affected: tuple[NewsAssetLink, ...] = ()
    sectors_affected: tuple[str, ...] = ()
    asset_classes_affected: tuple[AssetClass, ...] = ()
    event_category: GlobalNewsEventCategory
    sentiment: NewsSentiment
    relevance_score: Decimal = Field(ge=0, le=1)
    novelty_score: Decimal = Field(ge=0, le=1)
    source_reliability_score: Decimal = Field(ge=0, le=1)
    impact_score: Decimal = Field(ge=0, le=1)
    impact_horizon: NewsImpactHorizon
    confidence: Decimal = Field(ge=0, le=1)
    explanation: str = Field(min_length=1)
    provenance: tuple[str, ...] = ()

    @field_validator("published_at", "first_seen_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "news event timestamp")


class NewsEventCluster(FrozenDomainModel):
    cluster_id: str = Field(min_length=16)
    canonical_event: NormalizedGlobalNewsEvent
    first_publication_time: datetime
    sources: tuple[str, ...]
    corroboration_count: int = Field(ge=1)
    source_diversity: int = Field(ge=1)
    event_ids: tuple[str, ...]

    @field_validator("first_publication_time")
    @classmethod
    def first_publication_time_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "cluster first publication timestamp")


class AssetNewsContext(FrozenDomainModel):
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    as_of: datetime
    freshness: NewsFreshnessStatus
    unique_event_count: int = Field(ge=0)
    strongest_positive_event: str | None = None
    strongest_negative_event: str | None = None
    aggregate_sentiment: NewsSentiment
    aggregate_relevance: Decimal = Field(ge=0, le=1)
    aggregate_confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    aggregate_source_reliability: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    aggregate_impact: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    event_risk: Decimal = Field(ge=0, le=1)
    conflicting_news: bool
    latest_material_event_timestamp: datetime | None = None
    material_event_count: int = Field(ge=0)
    event_summaries: tuple[str, ...] = ()
    news_risk_flags: tuple[str, ...] = ()
    explanation: str = Field(min_length=1)

    @field_validator("as_of", "latest_material_event_timestamp")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "asset news context timestamp")


class GlobalRiskSnapshot(FrozenDomainModel):
    as_of: datetime
    macro_risk: Decimal = Field(ge=0, le=1)
    geopolitical_risk: Decimal = Field(ge=0, le=1)
    monetary_policy_risk: Decimal = Field(ge=0, le=1)
    market_stress: Decimal = Field(ge=0, le=1)
    major_events: tuple[str, ...] = ()
    high_impact_event_count: int = Field(ge=0)
    freshness: NewsFreshnessStatus
    explanation: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "global risk timestamp")


class GlobalNewsIntelligenceResult(FrozenDomainModel):
    engine_version: str = NEWS_INTELLIGENCE_VERSION
    as_of: datetime
    normalized_events: tuple[NormalizedGlobalNewsEvent, ...]
    event_clusters: tuple[NewsEventCluster, ...]
    asset_contexts: dict[str, AssetNewsContext]
    global_risk_snapshot: GlobalRiskSnapshot
    audit_records: tuple[dict[str, object], ...]
    provider_read_calls: int = Field(default=0, ge=0)
    raw_event_count: int = Field(default=0, ge=0)
    broker_write_calls: int = Field(default=0, ge=0, le=0)
    provider_name: str = "unknown"
    provider_status: NewsProviderStatus = NewsProviderStatus.PROVIDER_UNAVAILABLE
    provider_error_code: str | None = None
    provider_error_detail_safe: str | None = None
    provider_diagnostics: dict[str, object] = Field(default_factory=dict)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "news intelligence timestamp")


class NewsFeedProvider:
    provider_name = "in-memory-global-news"

    def __init__(self, items: tuple[RawNewsItem, ...] = (), *, unavailable: bool = False) -> None:
        self._items = items
        self._unavailable = unavailable
        self.read_calls = 0

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        self.read_calls += 1
        if self._unavailable:
            return ()
        return tuple(item for item in self._items if item.published_at <= as_of)


class GlobalNewsProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]: ...


class GlobalNewsIntelligenceEngine:
    """Normalizes and links news without access to execution or credentials."""

    def __init__(self, provider: GlobalNewsProvider | None = None) -> None:
        self._provider = provider or NewsFeedProvider()

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    def analyze(
        self, *, instruments: tuple[UniversalInstrument, ...], as_of: datetime
    ) -> GlobalNewsIntelligenceResult:
        setter = getattr(self._provider, "set_tickers", None)
        if callable(setter):
            setter(tuple(instrument.symbol for instrument in instruments))
        try:
            raw_items = self._provider.fetch_global_news(as_of=as_of)
        except NewsProviderError as exc:
            return self._unavailable_result(instruments=instruments, as_of=as_of, error=exc)
        except Exception as exc:
            error = NewsProviderError(
                "news provider read failed",
                status=NewsProviderStatus.PROVIDER_UNAVAILABLE,
                provider_error_message=type(exc).__name__,
            )
            return self._unavailable_result(instruments=instruments, as_of=as_of, error=error)
        events = tuple(
            self._normalize(item=item, instruments=instruments, first_seen_at=as_of)
            for item in raw_items
        )
        clusters = _cluster_events(events)
        contexts = {
            instrument.symbol: _asset_context(
                instrument=instrument,
                clusters=clusters,
                as_of=as_of,
                provider_configured=bool(raw_items),
            )
            for instrument in instruments
        }
        snapshot = _global_risk_snapshot(clusters=clusters, as_of=as_of)
        provider_status = getattr(self._provider, "last_status", None)
        if not isinstance(provider_status, NewsProviderStatus):
            provider_status = (
                NewsProviderStatus.AVAILABLE
                if raw_items
                else NewsProviderStatus.PROVIDER_UNAVAILABLE
            )
        return GlobalNewsIntelligenceResult(
            as_of=as_of,
            normalized_events=events,
            event_clusters=clusters,
            asset_contexts=contexts,
            global_risk_snapshot=snapshot,
            audit_records=tuple(_audit_record(event) for event in events),
            provider_read_calls=getattr(self._provider, "read_calls", 0),
            raw_event_count=len(raw_items),
            broker_write_calls=0,
            provider_name=self._provider.provider_name,
            provider_status=provider_status,
            provider_diagnostics=_provider_diagnostics(self._provider),
        )

    def _unavailable_result(
        self,
        *,
        instruments: tuple[UniversalInstrument, ...],
        as_of: datetime,
        error: NewsProviderError,
    ) -> GlobalNewsIntelligenceResult:
        contexts = {
            instrument.symbol: _asset_context(
                instrument=instrument,
                clusters=(),
                as_of=as_of,
                provider_configured=False,
            )
            for instrument in instruments
        }
        return GlobalNewsIntelligenceResult(
            as_of=as_of,
            normalized_events=(),
            event_clusters=(),
            asset_contexts=contexts,
            global_risk_snapshot=_global_risk_snapshot(clusters=(), as_of=as_of),
            audit_records=(),
            provider_read_calls=getattr(self._provider, "read_calls", 0),
            raw_event_count=0,
            broker_write_calls=0,
            provider_name=self._provider.provider_name,
            provider_status=error.status,
            provider_error_code=error.status.value,
            provider_error_detail_safe=error.provider_error_message,
            provider_diagnostics=_provider_diagnostics(self._provider),
        )

    def _normalize(
        self,
        *,
        item: RawNewsItem,
        instruments: tuple[UniversalInstrument, ...],
        first_seen_at: datetime,
    ) -> NormalizedGlobalNewsEvent:
        headline = sanitize_external_text(item.headline, maximum_length=280)
        summary = sanitize_external_text(item.summary, maximum_length=1200) if item.summary else ""
        category = _classify_category(headline)
        if category is GlobalNewsEventCategory.OTHER and summary:
            category = _classify_category(summary)
        sentiment = _classify_sentiment(headline)
        if sentiment is NewsSentiment.NEUTRAL and summary:
            sentiment = _classify_sentiment(summary)
        links = _link_assets(text=headline, category=category, instruments=instruments)
        link_evidence = headline
        if summary:
            summary_links = _link_assets(text=summary, category=category, instruments=instruments)
            links = _dedupe_links((*links, *summary_links))
            link_evidence = f"{headline} {summary}"
        # Alpaca supplies exact article tickers independently of prose. Keep
        # this evidence outside the bounded summary, and match only the source
        # symbols supplied by the caller (which verifies broker aliases).
        if (
            item.provider in {"ALPACA_NEWS", "GOOGLE_NEWS_RSS"}
            and item.provider_symbols
            and item.source_quality
            in {NewsSourceQuality.MAJOR_FINANCIAL_NEWS, NewsSourceQuality.SECONDARY_MEDIA}
        ):
            provider_symbols = set(item.provider_symbols)
            explicit_links = tuple(
                NewsAssetLink(
                    symbol=instrument.symbol,
                    asset_class=instrument.asset_class,
                    reason="provider article symbol metadata",
                    confidence=Decimal("0.95"),
                )
                for instrument in instruments
                if instrument.symbol in provider_symbols
            )
            links = _dedupe_links((*links, *explicit_links))
        sectors = _sectors_for_links(links, link_evidence)
        asset_classes = tuple(
            sorted({link.asset_class for link in links}, key=lambda value: value.value)
        )
        source_score = _source_reliability(item.source_quality)
        impact = _impact_score(category)
        relevance = max((link.confidence for link in links), default=Decimal("0.35"))
        confidence = _bounded((source_score + relevance + impact) / Decimal("3"))
        event_id = _stable_id(
            "news",
            headline.casefold(),
            item.published_at.isoformat(),
            item.source.casefold(),
        )
        return NormalizedGlobalNewsEvent(
            event_id=event_id,
            headline=headline,
            source=sanitize_external_text(item.source, maximum_length=80),
            source_type=item.source_quality,
            source_url_reference=item.url_or_reference,
            published_at=item.published_at,
            first_seen_at=first_seen_at,
            language=item.language,
            geographic_scope=item.geographic_scope,
            named_entities=_named_entities(link_evidence, instruments),
            companies_assets_affected=links,
            sectors_affected=sectors,
            asset_classes_affected=asset_classes,
            event_category=category,
            sentiment=sentiment,
            relevance_score=relevance,
            novelty_score=Decimal("1"),
            source_reliability_score=source_score,
            impact_score=impact,
            impact_horizon=_impact_horizon(category),
            confidence=confidence,
            explanation=_explanation(category, links),
            provenance=(self._provider.provider_name, item.provider, item.source_quality.value),
        )


def _cluster_events(events: tuple[NormalizedGlobalNewsEvent, ...]) -> tuple[NewsEventCluster, ...]:
    grouped: dict[str, list[NormalizedGlobalNewsEvent]] = defaultdict(list)
    for event in events:
        grouped[_cluster_key(event)].append(event)
    clusters: list[NewsEventCluster] = []
    for key, items in grouped.items():
        ordered = sorted(items, key=lambda event: event.published_at)
        canonical = max(
            ordered,
            key=lambda event: (
                event.source_reliability_score,
                event.impact_score,
                event.confidence,
            ),
        )
        sources = tuple(sorted({event.source for event in ordered}))
        clusters.append(
            NewsEventCluster(
                cluster_id=_stable_id("cluster", key),
                canonical_event=canonical,
                first_publication_time=ordered[0].published_at,
                sources=sources,
                corroboration_count=len(ordered),
                source_diversity=len(sources),
                event_ids=tuple(event.event_id for event in ordered),
            )
        )
    return tuple(sorted(clusters, key=lambda item: item.first_publication_time, reverse=True))


def _asset_context(
    *,
    instrument: UniversalInstrument,
    clusters: tuple[NewsEventCluster, ...],
    as_of: datetime,
    provider_configured: bool,
) -> AssetNewsContext:
    relevant = tuple(
        cluster
        for cluster in clusters
        if any(
            link.symbol == instrument.symbol
            for link in cluster.canonical_event.companies_assets_affected
        )
    )
    if not provider_configured:
        return AssetNewsContext(
            symbol=instrument.symbol,
            asset_class=instrument.asset_class,
            as_of=as_of,
            freshness=NewsFreshnessStatus.NEWS_SOURCE_UNAVAILABLE,
            unique_event_count=0,
            aggregate_sentiment=NewsSentiment.NEUTRAL,
            aggregate_relevance=Decimal("0"),
            event_risk=Decimal("0"),
            conflicting_news=False,
            material_event_count=0,
            explanation="news feed is not configured; neutral news was not fabricated",
        )
    positives = tuple(c for c in relevant if c.canonical_event.sentiment is NewsSentiment.POSITIVE)
    negatives = tuple(c for c in relevant if c.canonical_event.sentiment is NewsSentiment.NEGATIVE)
    latest = max((cluster.first_publication_time for cluster in relevant), default=None)
    aggregate_relevance = _bounded(
        sum((cluster.canonical_event.relevance_score for cluster in relevant), Decimal("0"))
        / Decimal(max(len(relevant), 1))
    )
    aggregate_confidence = _bounded(
        sum((cluster.canonical_event.confidence for cluster in relevant), Decimal("0"))
        / Decimal(max(len(relevant), 1))
    )
    aggregate_source_reliability = _bounded(
        sum(
            (cluster.canonical_event.source_reliability_score for cluster in relevant),
            Decimal("0"),
        )
        / Decimal(max(len(relevant), 1))
    )
    aggregate_impact = max(
        (cluster.canonical_event.impact_score for cluster in relevant),
        default=Decimal("0"),
    )
    sentiment = _aggregate_sentiment(positives=positives, negatives=negatives, total=len(relevant))
    freshness = _news_freshness(latest=latest, as_of=as_of)
    high_impact = tuple(c for c in relevant if c.canonical_event.impact_score >= Decimal("0.70"))
    return AssetNewsContext(
        symbol=instrument.symbol,
        asset_class=instrument.asset_class,
        as_of=as_of,
        freshness=freshness,
        unique_event_count=len(relevant),
        strongest_positive_event=_strongest(positives),
        strongest_negative_event=_strongest(negatives),
        aggregate_sentiment=sentiment,
        aggregate_relevance=aggregate_relevance,
        aggregate_confidence=aggregate_confidence,
        aggregate_source_reliability=aggregate_source_reliability,
        aggregate_impact=aggregate_impact,
        event_risk=max((c.canonical_event.impact_score for c in negatives), default=Decimal("0")),
        conflicting_news=bool(positives and negatives),
        latest_material_event_timestamp=latest,
        material_event_count=len(high_impact),
        event_summaries=tuple(c.canonical_event.headline for c in relevant[:3]),
        news_risk_flags=tuple(
            sorted({c.canonical_event.event_category.value for c in high_impact if c in negatives})
        ),
        explanation=(
            "asset-linked news affects the score only through its configured low weight; "
            "it cannot execute trades or bypass RiskManager"
        ),
    )


def _global_risk_snapshot(
    *, clusters: tuple[NewsEventCluster, ...], as_of: datetime
) -> GlobalRiskSnapshot:
    macro_categories = {
        GlobalNewsEventCategory.CENTRAL_BANK,
        GlobalNewsEventCategory.INTEREST_RATES,
        GlobalNewsEventCategory.INFLATION,
        GlobalNewsEventCategory.EMPLOYMENT,
        GlobalNewsEventCategory.MACRO,
    }
    geopolitical_categories = {
        GlobalNewsEventCategory.GEOPOLITICS,
        GlobalNewsEventCategory.NATURAL_DISASTER,
        GlobalNewsEventCategory.SUPPLY_CHAIN,
    }
    monetary = tuple(c for c in clusters if c.canonical_event.event_category in macro_categories)
    geopolitical = tuple(
        c for c in clusters if c.canonical_event.event_category in geopolitical_categories
    )
    high_impact = tuple(c for c in clusters if c.canonical_event.impact_score >= Decimal("0.70"))
    latest = max((c.first_publication_time for c in clusters), default=None)
    return GlobalRiskSnapshot(
        as_of=as_of,
        macro_risk=_risk_from(monetary),
        geopolitical_risk=_risk_from(geopolitical),
        monetary_policy_risk=_risk_from(monetary),
        market_stress=_risk_from(high_impact),
        major_events=tuple(c.canonical_event.headline for c in high_impact[:5]),
        high_impact_event_count=len(high_impact),
        freshness=_news_freshness(latest=latest, as_of=as_of),
        explanation="global news risk is read-only context and does not change RiskPolicy",
    )


def _classify_category(text: str) -> GlobalNewsEventCategory:
    lowered = text.casefold()
    rules = (
        (
            GlobalNewsEventCategory.CENTRAL_BANK,
            ("federal reserve", "fed", "ecb", "central bank", "economy_monetary"),
        ),
        (GlobalNewsEventCategory.INTEREST_RATES, ("interest rate", "rate cut", "rate hike")),
        (GlobalNewsEventCategory.INFLATION, ("inflation", "cpi", "pce")),
        (GlobalNewsEventCategory.EMPLOYMENT, ("jobs", "employment", "payroll")),
        (GlobalNewsEventCategory.MACRO, ("gdp", "recession", "macro", "economy_fiscal")),
        (GlobalNewsEventCategory.EARNINGS, ("earnings", "quarterly results")),
        (GlobalNewsEventCategory.COMPANY_GUIDANCE, ("guidance", "forecast")),
        (
            GlobalNewsEventCategory.MERGER_ACQUISITION,
            ("merger", "acquisition", "takeover", "mergers_and_acquisitions"),
        ),
        (GlobalNewsEventCategory.PRODUCT_LAUNCH, ("launch", "product")),
        (
            GlobalNewsEventCategory.CRYPTO,
            ("bitcoin", "crypto regulation", "crypto", "sec", "token", "blockchain"),
        ),
        (GlobalNewsEventCategory.REGULATION, ("regulation", "regulator")),
        (GlobalNewsEventCategory.LEGAL, ("lawsuit", "court", "legal")),
        (
            GlobalNewsEventCategory.GEOPOLITICS,
            (
                "war",
                "sanction",
                "geopolitical",
                "conflict",
                "ceasefire",
                "military",
                "invasion",
                "missile",
                "nato",
                "ukraine",
                "russia",
                "taiwan",
                "china",
                "iran",
                "israel",
                "tariff",
                "trade war",
            ),
        ),
        (GlobalNewsEventCategory.ENERGY, ("oil", "energy", "opec")),
        (GlobalNewsEventCategory.COMMODITIES, ("gold", "commodity", "commodities")),
        (GlobalNewsEventCategory.CYBER_SECURITY, ("cyber", "hack", "breach")),
        (GlobalNewsEventCategory.SUPPLY_CHAIN, ("supply chain", "shipping")),
        (GlobalNewsEventCategory.NATURAL_DISASTER, ("hurricane", "earthquake", "flood")),
        (GlobalNewsEventCategory.MARKET_STRESS, ("exchange outage", "market structure")),
    )
    for category, terms in rules:
        if any(term in lowered for term in terms):
            return category
    return GlobalNewsEventCategory.OTHER


def _classify_sentiment(text: str) -> NewsSentiment:
    lowered = text.casefold()
    positive_terms = (
        "beats",
        "raises",
        "approval",
        "surges",
        "growth",
        "easing",
        "cut",
        "bullish",
    )
    negative_terms = (
        "misses",
        "cuts guidance",
        "lawsuit",
        "ban",
        "sanction",
        "hack",
        "hike",
        "uncertainty",
        "bearish",
    )
    positive = any(term in lowered for term in positive_terms)
    negative = any(term in lowered for term in negative_terms)
    if positive and negative:
        return NewsSentiment.MIXED
    if positive:
        return NewsSentiment.POSITIVE
    if negative:
        return NewsSentiment.NEGATIVE
    return NewsSentiment.NEUTRAL


def _link_assets(
    *,
    text: str,
    category: GlobalNewsEventCategory,
    instruments: tuple[UniversalInstrument, ...],
) -> tuple[NewsAssetLink, ...]:
    links: list[NewsAssetLink] = []
    for instrument in instruments:
        symbol_match = _explicit_symbol_mention(instrument.symbol, text)
        name_match = _instrument_name_mention(instrument.display_name, instrument.symbol, text)
        alias_match = _explicit_phrase_mention(_company_alias_for_symbol(instrument.symbol), text)
        if symbol_match or name_match or alias_match:
            links.append(
                NewsAssetLink(
                    symbol=instrument.symbol,
                    asset_class=instrument.asset_class,
                    reason="explicit symbol/name mention",
                    confidence=Decimal("0.95"),
                )
            )
    # Broad macro/geopolitical headlines remain in GlobalRiskSnapshot. They
    # are not evidence about every ETF or crypto in the universe; linking them
    # to thousands of instruments inflated per-asset news scores and payloads.
    if category is GlobalNewsEventCategory.ENERGY:
        for instrument in instruments:
            if instrument.symbol in {"XOM", "XLE"}:
                links.append(
                    NewsAssetLink(
                        symbol=instrument.symbol,
                        asset_class=instrument.asset_class,
                        reason="energy event linked to energy exposure",
                        confidence=Decimal("0.70"),
                    )
                )
    return _dedupe_links(tuple(links))


_AMBIGUOUS_ASSET_WORDS = frozenset(
    {
        "SAFE",
        "GOLD",
        "ALL",
        "ONE",
        "NEAR",
        "LINK",
        "FLOW",
        "GAS",
        "SUN",
        "CORE",
        "ROSE",
        "SAND",
        "MASK",
        "CAKE",
        "STORY",
        "MOVEMENT",
        "RENDER",
    }
)


def _qualified_asset_mention(name: str, text: str, *, ignore_case: bool = False) -> bool:
    """Require local financial identity, not a common word elsewhere in a headline."""
    token = re.escape(name.strip())
    flags = re.IGNORECASE if ignore_case else 0
    # Explicit cashtags are issuer/asset notation, even for one-letter tickers.
    if re.search(rf"(?<!\w)\${token}(?!\w)", text, flags=flags):
        return True
    if re.search(rf"\b(?:NASDAQ|NYSE|ASX|LSE)\s*:\s*{token}(?!\w)", text, flags=flags):
        return True
    financial_noun = r"(?i:shares?|stocks?|tokens?|crypto(?:currency)?|blockchain|protocol|ETF)"
    return bool(
        re.search(rf"(?<!\w){token}(?!\w)\s+{financial_noun}\b", text, flags=flags)
        or re.search(rf"\b{financial_noun}\s+(?:(?i:of|in)\s+)?{token}(?!\w)", text, flags=flags)
    )


def _instrument_name_mention(name: str | None, symbol: str, text: str) -> bool:
    if not name:
        return False
    normalized = name.strip().upper()
    if (
        len(normalized) <= 2
        or normalized == symbol.strip().upper()
        or normalized in _AMBIGUOUS_ASSET_WORDS
    ):
        return _qualified_asset_mention(name, text, ignore_case=True)
    return _explicit_phrase_mention(name, text)


def _explicit_symbol_mention(symbol: str, text: str) -> bool:
    """Match a ticker as a token without treating short common words as news."""
    normalized = symbol.strip().upper()
    if not normalized:
        return False
    if len(normalized) <= 2 or normalized in _AMBIGUOUS_ASSET_WORDS:
        return _qualified_asset_mention(normalized, text)
    # Tickers of every length collide with ordinary headline words (e.g.
    # SAFE, GOLD, or VISA). Match exact uppercase ticker notation; issuer
    # display names and known aliases are checked independently, case-insensitively.
    return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", text) is not None


def _explicit_phrase_mention(phrase: str | None, text: str) -> bool:
    if not phrase or phrase.startswith("__no_alias_for_"):
        return False
    return (
        re.search(
            rf"(?<!\w){re.escape(phrase.strip())}(?!\w)",
            text,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _company_alias_for_symbol(symbol: str) -> str:
    return {
        "AAPL": "apple",
        "MSFT": "microsoft",
        "NVDA": "nvidia",
        "AMZN": "amazon",
        "GOOGL": "google",
        "META": "meta",
        "TSLA": "tesla",
        "AMD": "advanced micro devices",
        "JPM": "jpmorgan",
        "UNH": "unitedhealth",
        "XOM": "exxon",
        "COST": "costco",
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
    }.get(symbol.upper(), f"__no_alias_for_{symbol.casefold()}__")


def _dedupe_links(links: tuple[NewsAssetLink, ...]) -> tuple[NewsAssetLink, ...]:
    selected: dict[str, NewsAssetLink] = {}
    for link in links:
        current = selected.get(link.symbol)
        if current is None or link.confidence > current.confidence:
            selected[link.symbol] = link
    return tuple(selected[symbol] for symbol in sorted(selected))


def _sectors_for_links(links: tuple[NewsAssetLink, ...], text: str) -> tuple[str, ...]:
    sectors = set()
    for link in links:
        if link.symbol in {
            "AAPL",
            "MSFT",
            "NVDA",
            "AMZN",
            "GOOGL",
            "META",
            "TSLA",
            "AMD",
            "QQQ",
            "XLK",
        }:
            sectors.add("TECHNOLOGY")
        if link.symbol in {"JPM", "XLF"}:
            sectors.add("FINANCIALS")
        if link.symbol in {"XOM", "XLE"}:
            sectors.add("ENERGY")
        if link.asset_class is AssetClass.CRYPTO:
            sectors.add("DIGITAL_ASSETS")
    lowered = text.casefold()
    if "technology" in lowered:
        sectors.add("TECHNOLOGY")
    if "bank" in lowered:
        sectors.add("FINANCIALS")
    return tuple(sorted(sectors))


def _named_entities(text: str, instruments: tuple[UniversalInstrument, ...]) -> tuple[str, ...]:
    entities = {
        instrument.symbol
        for instrument in instruments
        if _explicit_symbol_mention(instrument.symbol, text)
    }
    for term in ("Federal Reserve", "ECB", "SEC", "OPEC", "Apple", "Microsoft"):
        if term.casefold() in text.casefold():
            entities.add(term)
    return tuple(sorted(entities))


def _source_reliability(quality: NewsSourceQuality) -> Decimal:
    return {
        NewsSourceQuality.PRIMARY_OFFICIAL: Decimal("1.00"),
        NewsSourceQuality.REGULATORY_GOVERNMENT: Decimal("0.95"),
        NewsSourceQuality.COMPANY_RELEASE: Decimal("0.90"),
        NewsSourceQuality.MAJOR_FINANCIAL_NEWS: Decimal("0.85"),
        NewsSourceQuality.SECONDARY_MEDIA: Decimal("0.60"),
        NewsSourceQuality.UNKNOWN_LOW_CONFIDENCE: Decimal("0.30"),
    }[quality]


def _impact_score(category: GlobalNewsEventCategory) -> Decimal:
    base = {
        GlobalNewsEventCategory.CENTRAL_BANK: Decimal("0.85"),
        GlobalNewsEventCategory.INTEREST_RATES: Decimal("0.85"),
        GlobalNewsEventCategory.INFLATION: Decimal("0.80"),
        GlobalNewsEventCategory.EARNINGS: Decimal("0.70"),
        GlobalNewsEventCategory.COMPANY_GUIDANCE: Decimal("0.75"),
        GlobalNewsEventCategory.CRYPTO: Decimal("0.75"),
        GlobalNewsEventCategory.GEOPOLITICS: Decimal("0.75"),
        GlobalNewsEventCategory.ENERGY: Decimal("0.70"),
    }.get(category, Decimal("0.50"))
    # Event impact is global; lack of a defensible per-asset link must not
    # erase material macro/geopolitical risk from the global dashboard.
    return _bounded(base)


def _impact_horizon(category: GlobalNewsEventCategory) -> NewsImpactHorizon:
    if category in {GlobalNewsEventCategory.MARKET_STRESS, GlobalNewsEventCategory.CYBER_SECURITY}:
        return NewsImpactHorizon.IMMEDIATE
    if category in {GlobalNewsEventCategory.EARNINGS, GlobalNewsEventCategory.COMPANY_GUIDANCE}:
        return NewsImpactHorizon.INTRADAY
    if category in {
        GlobalNewsEventCategory.CENTRAL_BANK,
        GlobalNewsEventCategory.INTEREST_RATES,
        GlobalNewsEventCategory.INFLATION,
    }:
        return NewsImpactHorizon.MULTIDAY
    return NewsImpactHorizon.LONG_TERM


def _cluster_key(event: NormalizedGlobalNewsEvent) -> str:
    linked = ",".join(link.symbol for link in event.companies_assets_affected)
    words = " ".join(sorted(set(re.findall(r"[a-zA-Z]{4,}", event.headline.casefold())))[:8])
    return f"{event.event_category.value}|{event.published_at.date().isoformat()}|{linked}|{words}"


def _news_freshness(*, latest: datetime | None, as_of: datetime) -> NewsFreshnessStatus:
    if latest is None:
        return NewsFreshnessStatus.NEWS_SOURCE_UNAVAILABLE
    age = as_of - latest
    if age <= NEWS_FRESH_MAX_AGE:
        return NewsFreshnessStatus.NEWS_FRESH
    if age <= timedelta(days=2):
        return NewsFreshnessStatus.NEWS_DELAYED
    return NewsFreshnessStatus.NEWS_STALE


def _aggregate_sentiment(
    *,
    positives: tuple[NewsEventCluster, ...],
    negatives: tuple[NewsEventCluster, ...],
    total: int,
) -> NewsSentiment:
    if total == 0:
        return NewsSentiment.NEUTRAL
    if positives and negatives:
        return NewsSentiment.MIXED
    if positives:
        return NewsSentiment.POSITIVE
    if negatives:
        return NewsSentiment.NEGATIVE
    return NewsSentiment.NEUTRAL


def _strongest(clusters: tuple[NewsEventCluster, ...]) -> str | None:
    if not clusters:
        return None
    return max(
        clusters,
        key=lambda cluster: (
            cluster.canonical_event.impact_score,
            cluster.canonical_event.confidence,
        ),
    ).canonical_event.headline


def _risk_from(clusters: tuple[NewsEventCluster, ...]) -> Decimal:
    if not clusters:
        return Decimal("0")
    return _bounded(max(c.canonical_event.impact_score for c in clusters))


def _explanation(category: GlobalNewsEventCategory, links: tuple[NewsAssetLink, ...]) -> str:
    if not links:
        return f"{category.value} event retained globally without direct asset link"
    symbols = ", ".join(link.symbol for link in links[:5])
    return f"{category.value} event linked to {symbols} using explicit/provenance rules"


def _audit_record(event: NormalizedGlobalNewsEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "published_at": event.published_at.isoformat(),
        "first_seen_at": event.first_seen_at.isoformat(),
        "category": event.event_category.value,
        "sentiment": event.sentiment.value,
        "linked_symbols": tuple(link.symbol for link in event.companies_assets_affected),
        "source_quality": str(event.source_reliability_score),
        "broker_write_calls": 0,
    }


def _provider_diagnostics(provider: object) -> dict[str, object]:
    diagnostics = getattr(provider, "last_diagnostics", {})
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _bounded(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))
