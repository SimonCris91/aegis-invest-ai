"""Step 9.0D Alpha Vantage news adapter tests."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from app.data.models import DataProviderError, DataProviderStatus
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.news.alpha_vantage import (
    ALPHA_VANTAGE_API_KEY_ENV,
    AlphaVantageNewsProvider,
    alpha_vantage_request_plan,
    alpha_vantage_symbol_to_aegis,
    news_provider_readiness_matrix,
)
from app.news.intelligence import (
    GlobalNewsIntelligenceEngine,
    NewsProviderError,
    NewsProviderStatus,
    NewsSentiment,
)
from app.scanner.active import ActiveMarketScanner


def test_alpha_vantage_adapter_maps_fixture_payload_to_global_news_context() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    provider = AlphaVantageNewsProvider(
        api_key="secret",
        transport=_JsonTransport(
            _payload(
                as_of,
                articles=(
                    _article(
                        title="Apple raises guidance after product launch",
                        source="Reuters",
                        published=as_of - timedelta(hours=1),
                        topics=("earnings",),
                        tickers=("AAPL",),
                        sentiment="Bullish",
                    ),
                    _article(
                        title="SEC crypto regulation creates Bitcoin token uncertainty",
                        source="SEC",
                        published=as_of - timedelta(hours=2),
                        topics=("blockchain",),
                        tickers=("CRYPTO:BTC",),
                        sentiment="Bearish",
                    ),
                ),
            )
        ),
        tickers=("AAPL", "CRYPTO:BTC"),
        topics=("earnings", "blockchain"),
    )

    result = GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=_instruments(as_of),
        as_of=as_of,
    )

    assert provider.provider_name == "ALPHA_VANTAGE"
    assert len(result.normalized_events) == 2
    assert result.asset_contexts["AAPL"].aggregate_sentiment is NewsSentiment.POSITIVE
    assert result.asset_contexts["BTC"].aggregate_sentiment is NewsSentiment.NEGATIVE
    assert "apikey=<redacted>" in str(provider.last_diagnostics["sanitized_endpoint"])
    assert "secret" not in str(provider.last_diagnostics)
    assert result.broker_write_calls == 0


def test_alpha_vantage_symbol_normalization_preserves_provider_identity() -> None:
    assert alpha_vantage_symbol_to_aegis("AAPL") == {
        "provider_symbol": "AAPL",
        "canonical_symbol": "AAPL",
        "asset_class": "UNKNOWN",
        "known": False,
    }
    assert alpha_vantage_symbol_to_aegis("CRYPTO:BTC") == {
        "provider_symbol": "CRYPTO:BTC",
        "canonical_symbol": "BTC",
        "asset_class": "CRYPTO",
        "known": True,
    }
    assert alpha_vantage_symbol_to_aegis("FOREX:USD") == {
        "provider_symbol": "FOREX:USD",
        "canonical_symbol": "USD",
        "asset_class": "FOREX",
        "known": False,
    }


def test_alpha_vantage_future_article_is_hidden_by_provider_causality() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    provider = AlphaVantageNewsProvider(
        api_key="secret",
        transport=_JsonTransport(
            _payload(
                as_of,
                articles=(
                    _article(
                        title="Apple beats earnings",
                        source="Reuters",
                        published=as_of - timedelta(minutes=30),
                        topics=("earnings",),
                        tickers=("AAPL",),
                        sentiment="Bullish",
                    ),
                    _article(
                        title="Apple raises guidance tomorrow",
                        source="Reuters",
                        published=as_of + timedelta(hours=1),
                        topics=("earnings",),
                        tickers=("AAPL",),
                        sentiment="Bullish",
                    ),
                ),
            )
        ),
    )

    items = provider.fetch_global_news(as_of=as_of)

    assert len(items) == 1
    assert items[0].headline == "Apple beats earnings"


def test_alpha_vantage_rate_limit_and_auth_fail_closed_with_secret_safe_diagnostics() -> None:
    provider = AlphaVantageNewsProvider(
        api_key="secret",
        transport=_FailingTransport(http_status=429),
    )

    with pytest.raises(NewsProviderError) as exc_info:
        provider.fetch_global_news(as_of=datetime(2026, 8, 28, tzinfo=UTC))

    diagnostics = exc_info.value.safe_diagnostics()
    assert diagnostics["status"] == NewsProviderStatus.RATE_LIMITED.value
    assert diagnostics["http_status"] == 429
    assert "secret" not in str(diagnostics)

    missing = AlphaVantageNewsProvider(api_key=None, transport=_JsonTransport({"feed": []}))
    with pytest.raises(NewsProviderError) as missing_exc:
        missing.fetch_global_news(as_of=datetime(2026, 8, 28, tzinfo=UTC))
    assert missing_exc.value.status is NewsProviderStatus.AUTH_FAILED


def test_alpha_vantage_malformed_response_fails_closed() -> None:
    provider = AlphaVantageNewsProvider(api_key="secret", transport=_JsonTransport({"feed": {}}))

    with pytest.raises(NewsProviderError) as exc_info:
        provider.fetch_global_news(as_of=datetime(2026, 8, 28, tzinfo=UTC))

    assert exc_info.value.status is NewsProviderStatus.MALFORMED_RESPONSE


def test_alpha_vantage_duplicate_and_conflicting_reports_flow_through_clustering() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    provider = AlphaVantageNewsProvider(
        api_key="secret",
        transport=_JsonTransport(
            _payload(
                as_of,
                articles=(
                    _article(
                        title="Apple beats earnings",
                        source="Reuters",
                        published=as_of - timedelta(hours=2),
                        topics=("earnings",),
                        tickers=("AAPL",),
                        sentiment="Bullish",
                    ),
                    _article(
                        title="Apple beats earnings",
                        source="CNBC",
                        published=as_of - timedelta(minutes=90),
                        topics=("earnings",),
                        tickers=("AAPL",),
                        sentiment="Bullish",
                    ),
                    _article(
                        title="Apple faces legal lawsuit",
                        source="Reuters",
                        published=as_of - timedelta(hours=1),
                        topics=("financial_markets",),
                        tickers=("AAPL",),
                        sentiment="Bearish",
                    ),
                ),
            )
        ),
    )

    result = GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=_instruments(as_of),
        as_of=as_of,
    )

    assert len(result.event_clusters) == 2
    assert max(cluster.corroboration_count for cluster in result.event_clusters) == 2
    assert result.asset_contexts["AAPL"].conflicting_news is True


def test_alpha_vantage_request_planning_and_secondary_provider_readiness() -> None:
    plan = alpha_vantage_request_plan(universe_symbols=("AAPL", "SPY", "BTC"))
    readiness = news_provider_readiness_matrix()
    ticker_batches = cast(tuple[tuple[str, ...], ...], plan["ticker_batches"])

    assert plan["strategy"] == "batch broad requests; do not fetch once per symbol"
    assert ("AAPL", "SPY") in ticker_batches
    assert ("CRYPTO:BTC",) in ticker_batches
    assert readiness[0]["credentials"] == (ALPHA_VANTAGE_API_KEY_ENV,)
    assert readiness[1]["provider"] == "MASSIVE_NEWS"
    assert readiness[1]["implemented"] is False


def test_alpha_vantage_news_context_reaches_scanner_observationally_only() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of, display_name="Apple")
    portfolio = PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("200"))
    bars = _bars(instrument, as_of=as_of)
    scanner = ActiveMarketScanner(minimum_bars=60)
    baseline = scanner.scan(
        instruments=(instrument,),
        bars_by_symbol={"AAPL": bars},
        portfolio=portfolio,
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
    )
    provider = AlphaVantageNewsProvider(
        api_key="secret",
        transport=_JsonTransport(
            _payload(
                as_of,
                articles=(
                    _article(
                        title="Apple raises guidance",
                        source="Reuters",
                        published=as_of - timedelta(hours=1),
                        topics=("earnings",),
                        tickers=("AAPL",),
                        sentiment="Bullish",
                    ),
                ),
            )
        ),
    )
    contexts = (
        GlobalNewsIntelligenceEngine(provider)
        .analyze(
            instruments=(instrument,),
            as_of=as_of,
        )
        .asset_contexts
    )

    with_news = scanner.scan(
        instruments=(instrument,),
        bars_by_symbol={"AAPL": bars},
        portfolio=portfolio,
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        news_context_by_symbol=contexts,
    )

    assert with_news.candidates[0].news_sentiment == "POSITIVE"
    assert with_news.candidates[0].material_event_count == 1
    assert with_news.candidates[0].opportunity_score == baseline.candidates[0].opportunity_score
    assert with_news.candidates[0].confidence == baseline.candidates[0].confidence
    assert with_news.broker_write_calls == 0


def _payload(as_of: datetime, *, articles: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {"items": str(len(articles)), "feed": articles, "as_of": as_of.isoformat()}


def _article(
    *,
    title: str,
    source: str,
    published: datetime,
    topics: tuple[str, ...],
    tickers: tuple[str, ...],
    sentiment: str,
) -> dict[str, object]:
    return {
        "title": title,
        "url": f"fixture://{source}/{title}",
        "time_published": published.strftime("%Y%m%dT%H%M%S"),
        "source": source,
        "summary": title,
        "overall_sentiment_label": sentiment,
        "topics": tuple({"topic": topic, "relevance_score": "0.9"} for topic in topics),
        "ticker_sentiment": tuple(
            {
                "ticker": ticker,
                "relevance_score": "0.9",
                "ticker_sentiment_label": sentiment,
            }
            for ticker in tickers
        ),
    }


def _instruments(as_of: datetime) -> tuple[UniversalInstrument, ...]:
    return (
        _instrument("AAPL", AssetClass.EQUITY, "1001", as_of, display_name="Apple"),
        _instrument("BTC", AssetClass.CRYPTO, "4001", as_of, display_name="Bitcoin"),
    )


def _instrument(
    symbol: str,
    asset_class: AssetClass,
    broker_instrument_id: str,
    as_of: datetime,
    *,
    display_name: str | None = None,
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id=broker_instrument_id,
        symbol=symbol,
        display_name=display_name or symbol,
        asset_class=asset_class,
        currency=Currency.USD,
        exchange="TEST",
        market_status=(
            MarketStatus.CONTINUOUS_24_7 if asset_class is AssetClass.CRYPTO else MarketStatus.OPEN
        ),
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=as_of,
    )


def _bars(
    instrument: UniversalInstrument, *, as_of: datetime, count: int = 80
) -> tuple[MarketBar, ...]:
    start = as_of - timedelta(days=count - 1)
    price = Decimal("100")
    rows = []
    for index in range(count):
        close = (price * Decimal("1.003")).quantize(Decimal("0.0001"))
        rows.append(
            MarketBar(
                instrument=instrument,
                timestamp=start + timedelta(days=index),
                timeframe=TimeFrame.ONE_DAY,
                open=price,
                high=max(price, close) * Decimal("1.01"),
                low=min(price, close) * Decimal("0.99"),
                close=close,
                volume=Decimal("100000"),
                currency=Currency.USD,
                source="fixture",
                data_quality=FeatureQuality.GOOD,
            )
        )
        price = close
    return tuple(rows)


class _JsonTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.urls.append(url)
        assert headers["User-Agent"] == "AegisInvestAI/0.7"
        return json.dumps(self.payload)


class _FailingTransport:
    def __init__(self, *, http_status: int) -> None:
        self.http_status = http_status

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        raise DataProviderError(
            "rate limited",
            status=DataProviderStatus.RATE_LIMITED,
            http_status=self.http_status,
            sanitized_endpoint=url.replace("secret", "<redacted>"),
            provider_error_message="rate limit reached",
        )
