"""Step 9.0C global news intelligence offline foundation tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.news.intelligence import (
    GlobalNewsEventCategory,
    GlobalNewsIntelligenceEngine,
    NewsFeedProvider,
    NewsFreshnessStatus,
    NewsSentiment,
    NewsSourceQuality,
    RawNewsItem,
)
from app.scanner.active import ActiveMarketScanner


def test_duplicate_news_clusters_same_event_without_counting_independent_signals() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    instruments = _instruments(as_of)
    provider = NewsFeedProvider(
        items=(
            _raw(
                "Federal Reserve raises interest rate outlook",
                as_of - timedelta(hours=1),
                source="Official Fed",
                quality=NewsSourceQuality.PRIMARY_OFFICIAL,
            ),
            _raw(
                "Federal Reserve raises interest rate outlook",
                as_of - timedelta(minutes=30),
                source="Major Wire",
                quality=NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
            ),
        )
    )

    result = GlobalNewsIntelligenceEngine(provider).analyze(instruments=instruments, as_of=as_of)

    assert len(result.normalized_events) == 2
    assert len(result.event_clusters) == 1
    assert result.event_clusters[0].corroboration_count == 2
    assert result.event_clusters[0].source_diversity == 2
    assert result.broker_write_calls == 0


def test_future_news_is_excluded_for_strict_anti_lookahead() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    provider = NewsFeedProvider(
        items=(
            _raw("Apple raises guidance", as_of - timedelta(hours=1)),
            _raw("Apple misses earnings tomorrow", as_of + timedelta(hours=1)),
        )
    )

    result = GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=_instruments(as_of), as_of=as_of
    )

    assert len(result.normalized_events) == 1
    assert result.normalized_events[0].headline == "Apple raises guidance"
    assert all(item["broker_write_calls"] == 0 for item in result.audit_records)


def test_company_news_links_to_asset_sector_and_broad_etf_context_only_with_reasons() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    result = GlobalNewsIntelligenceEngine(
        NewsFeedProvider(
            items=(
                _raw(
                    "Apple raises guidance after product launch",
                    as_of - timedelta(hours=2),
                    quality=NewsSourceQuality.COMPANY_RELEASE,
                ),
            )
        )
    ).analyze(instruments=_instruments(as_of), as_of=as_of)

    event = result.normalized_events[0]
    links = {link.symbol: link.reason for link in event.companies_assets_affected}

    assert event.event_category is GlobalNewsEventCategory.COMPANY_GUIDANCE
    assert links == {"AAPL": "explicit symbol/name mention"}
    assert "TECHNOLOGY" in event.sectors_affected
    assert result.asset_contexts["AAPL"].aggregate_sentiment is NewsSentiment.POSITIVE


def test_macro_news_stays_global_without_claiming_every_etf_and_crypto_is_affected() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    result = GlobalNewsIntelligenceEngine(
        NewsFeedProvider(
            items=(
                _raw(
                    "Federal Reserve signals inflation risk and possible rate hike",
                    as_of - timedelta(hours=1),
                    quality=NewsSourceQuality.REGULATORY_GOVERNMENT,
                ),
            )
        )
    ).analyze(instruments=_instruments(as_of), as_of=as_of)

    event = result.normalized_events[0]
    linked = {link.symbol: link for link in event.companies_assets_affected}

    assert event.event_category is GlobalNewsEventCategory.CENTRAL_BANK
    assert linked == {}
    assert all(context.unique_event_count == 0 for context in result.asset_contexts.values())
    assert result.global_risk_snapshot.monetary_policy_risk >= Decimal("0.80")


def test_crypto_regulation_links_crypto_only_when_asset_class_justifies_it() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    result = GlobalNewsIntelligenceEngine(
        NewsFeedProvider(
            items=(
                _raw(
                    "SEC crypto regulation creates Bitcoin token uncertainty",
                    as_of - timedelta(hours=1),
                    quality=NewsSourceQuality.REGULATORY_GOVERNMENT,
                ),
            )
        )
    ).analyze(instruments=_instruments(as_of), as_of=as_of)

    linked = {link.symbol for link in result.normalized_events[0].companies_assets_affected}

    assert linked == {"BTC"}
    assert "AAPL" not in linked
    assert result.asset_contexts["BTC"].event_risk > Decimal("0")
    assert result.asset_contexts["ETH"].unique_event_count == 0


def test_conflicting_sentiment_is_reported_per_asset() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    provider = NewsFeedProvider(
        items=(
            _raw("Apple beats earnings", as_of - timedelta(hours=2)),
            _raw("Apple faces legal lawsuit", as_of - timedelta(hours=1)),
        )
    )

    result = GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=_instruments(as_of), as_of=as_of
    )
    context = result.asset_contexts["AAPL"]

    assert context.aggregate_sentiment is NewsSentiment.MIXED
    assert context.conflicting_news is True
    assert context.strongest_positive_event is not None
    assert context.strongest_negative_event is not None


def test_stale_and_unavailable_news_are_explicit_not_fabricated_neutral() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    stale = GlobalNewsIntelligenceEngine(
        NewsFeedProvider(items=(_raw("Apple beats earnings", as_of - timedelta(days=5)),))
    ).analyze(instruments=_instruments(as_of), as_of=as_of)
    unavailable = GlobalNewsIntelligenceEngine(NewsFeedProvider()).analyze(
        instruments=_instruments(as_of), as_of=as_of
    )

    assert stale.asset_contexts["AAPL"].freshness is NewsFreshnessStatus.NEWS_STALE
    assert (
        unavailable.asset_contexts["AAPL"].freshness is NewsFreshnessStatus.NEWS_SOURCE_UNAVAILABLE
    )
    assert "not fabricated" in unavailable.asset_contexts["AAPL"].explanation


def test_active_scanner_scores_explicit_asset_news_with_bounded_weight() -> None:
    as_of = datetime(2026, 8, 28, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    bars = _bars(instrument, as_of=as_of)
    portfolio = PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("200"))
    scanner = ActiveMarketScanner(minimum_bars=60)
    without_news = scanner.scan(
        instruments=(instrument,),
        bars_by_symbol={"AAPL": bars},
        portfolio=portfolio,
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )
    news_context = (
        GlobalNewsIntelligenceEngine(
            NewsFeedProvider(items=(_raw("Apple beats earnings", as_of - timedelta(hours=1)),))
        )
        .analyze(instruments=(instrument,), as_of=as_of)
        .asset_contexts
    )

    with_news = scanner.scan(
        instruments=(instrument,),
        bars_by_symbol={"AAPL": bars},
        portfolio=portfolio,
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
        news_context_by_symbol=news_context,
    )

    assert with_news.candidates[0].news_sentiment == "POSITIVE"
    assert with_news.candidates[0].material_event_count == 1
    assert with_news.candidates[0].news_relevance > Decimal("0")
    score_delta = (
        with_news.candidates[0].opportunity_score
        - without_news.candidates[0].opportunity_score
    )
    assert Decimal("0") < score_delta <= Decimal("0.60")
    assert with_news.candidates[0].confidence == without_news.candidates[0].confidence
    assert with_news.broker_write_calls == 0


def test_scanner_decision_lanes_do_not_count_crypto_quote_pairs_as_separate_assets() -> None:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    instruments = (
        _instrument(
            "LTCAUD",
            AssetClass.CRYPTO,
            "4101",
            as_of,
            display_name="Litecoin / Australian Dollar",
        ),
        _instrument(
            "LTCEUR",
            AssetClass.CRYPTO,
            "4102",
            as_of,
            display_name="Litecoin / Euro",
        ),
        _instrument(
            "BTCUSD",
            AssetClass.CRYPTO,
            "4103",
            as_of,
            display_name="Bitcoin / US Dollar",
        ),
    )
    portfolio = PortfolioSnapshot(as_of=as_of, currency=Currency.USD, cash=Decimal("2000"))
    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=instruments,
        bars_by_symbol={item.symbol: _bars(item, as_of=as_of) for item in instruments},
        portfolio=portfolio,
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
    )

    assert len(result.candidates) == 3
    assert sum(item.symbol.startswith("LTC") for item in result.top_opportunities) <= 1
    assert sum(item.symbol.startswith("LTC") for item in result.watchlist) <= 1
    assert any(item.symbol == "BTCUSD" for item in (*result.top_opportunities, *result.watchlist))


def _raw(
    headline: str,
    published_at: datetime,
    *,
    source: str = "Fixture Source",
    quality: NewsSourceQuality = NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
) -> RawNewsItem:
    return RawNewsItem(
        headline=headline,
        source=source,
        published_at=published_at,
        source_quality=quality,
        language="en",
        geographic_scope="US",
    )


def _instruments(as_of: datetime) -> tuple[UniversalInstrument, ...]:
    return (
        _instrument("AAPL", AssetClass.EQUITY, "1001", as_of, display_name="Apple"),
        _instrument("SPY", AssetClass.ETF, "3001", as_of),
        _instrument("QQQ", AssetClass.ETF, "3002", as_of),
        _instrument("GLD", AssetClass.ETF, "3003", as_of),
        _instrument("BTC", AssetClass.CRYPTO, "4001", as_of, display_name="Bitcoin"),
        _instrument("ETH", AssetClass.CRYPTO, "4002", as_of, display_name="Ethereum"),
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
    instrument: UniversalInstrument,
    *,
    as_of: datetime,
    count: int = 80,
) -> tuple[MarketBar, ...]:
    start = as_of - timedelta(days=count - 1)
    price = Decimal("100")
    bars = []
    for index in range(count):
        timestamp = start + timedelta(days=index)
        close = (price * Decimal("1.003")).quantize(Decimal("0.0001"))
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=timestamp,
                timeframe=TimeFrame.ONE_DAY,
                open=price,
                high=max(price, close) * Decimal("1.01"),
                low=min(price, close) * Decimal("0.99"),
                close=close,
                volume=Decimal("100000"),
                currency=Currency.USD,
                source="test-cache",
                data_quality=FeatureQuality.GOOD,
            )
        )
        price = close
    return tuple(bars)
