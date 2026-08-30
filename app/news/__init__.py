"""Provider-neutral news boundaries and offline providers."""

from app.news.alpha_vantage import (
    ALPHA_VANTAGE_API_KEY_ENV,
    ALPHA_VANTAGE_NEWS_FUNCTION,
    ALPHA_VANTAGE_PROVIDER,
    AlphaVantageNewsProvider,
    alpha_vantage_request_plan,
    alpha_vantage_symbol_to_aegis,
    news_provider_readiness_matrix,
)
from app.news.intelligence import (
    AssetNewsContext,
    GlobalNewsEventCategory,
    GlobalNewsIntelligenceEngine,
    GlobalNewsIntelligenceResult,
    GlobalNewsProvider,
    GlobalRiskSnapshot,
    NewsEventCluster,
    NewsFeedProvider,
    NewsFreshnessStatus,
    NewsImpactHorizon,
    NewsSentiment,
    NewsSourceQuality,
    NormalizedGlobalNewsEvent,
    RawNewsItem,
)
from app.news.ports import NewsProvider, NewsService
from app.news.providers import FakeNewsProvider, FixtureNewsProvider

__all__ = [
    "AssetNewsContext",
    "ALPHA_VANTAGE_API_KEY_ENV",
    "ALPHA_VANTAGE_NEWS_FUNCTION",
    "ALPHA_VANTAGE_PROVIDER",
    "AlphaVantageNewsProvider",
    "FakeNewsProvider",
    "FixtureNewsProvider",
    "GlobalNewsEventCategory",
    "GlobalNewsIntelligenceEngine",
    "GlobalNewsIntelligenceResult",
    "GlobalNewsProvider",
    "GlobalRiskSnapshot",
    "NewsEventCluster",
    "NewsFeedProvider",
    "NewsFreshnessStatus",
    "NewsImpactHorizon",
    "NewsProvider",
    "NewsSentiment",
    "NewsService",
    "NewsSourceQuality",
    "NormalizedGlobalNewsEvent",
    "RawNewsItem",
    "alpha_vantage_request_plan",
    "alpha_vantage_symbol_to_aegis",
    "news_provider_readiness_matrix",
]
