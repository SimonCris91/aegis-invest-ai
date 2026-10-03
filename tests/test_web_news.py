from datetime import UTC, datetime
from decimal import Decimal

from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.universe import UniversalInstrument
from app.news.intelligence import GlobalNewsIntelligenceEngine, NewsProviderStatus
from app.news.web_rss import BingNewsRssProvider, GoogleNewsRssProvider
from app.orchestration.active_runtime import _candidate_news_search_terms


class FakeTransport:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.urls.append(url)
        return self.payload


def _japanese_instrument() -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id="4062",
        symbol="4062.T",
        display_name="Inpex Corporation",
        asset_class=AssetClass.EQUITY,
        currency=Currency.USD,
        exchange="TSE",
        market="Japan",
        market_status=MarketStatus.OPEN,
        tradeable=True,
        buy_allowed=True,
        sell_allowed=True,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=False,
        metadata_timestamp=datetime(2026, 10, 2, 8, tzinfo=UTC),
    )


def test_google_news_rss_reads_candidate_specific_japanese_result() -> None:
    transport = FakeTransport(
        """<?xml version="1.0"?><rss><channel><item>
        <title>Inpex Corporation shares rise after earnings</title>
        <source url="https://example.test">Tokyo Financial News</source>
        <link>https://example.test/inpex</link>
        <pubDate>Fri, 02 Oct 2026 08:00:00 GMT</pubDate>
        <description>Tokyo market update for Inpex Corporation.</description>
        </item></channel></rss>"""
    )
    provider = GoogleNewsRssProvider(
        query='("Inpex Corporation" OR "4062.T" OR "4062 Tokyo")',
        candidate_symbol="4062.T",
        locale="ja-JP",
        region="JP",
        transport=transport,
    )

    items = provider.fetch_global_news(as_of=datetime(2026, 10, 2, 9, tzinfo=UTC))

    assert len(items) == 1
    assert items[0].provider == "GOOGLE_NEWS_RSS"
    assert items[0].provider_symbols == ("4062.T",)
    assert items[0].geographic_scope == "ASIA"
    assert provider.last_status is NewsProviderStatus.AVAILABLE
    assert "gl=JP" in transport.urls[0]


def test_japanese_search_terms_include_exchange_context_without_short_ticker_noise() -> None:
    terms = _candidate_news_search_terms(_japanese_instrument())

    assert terms[:1] == ("Inpex Corporation",)
    assert "4062.T" in terms
    assert "4062 Tokyo" in terms
    assert "4062 JPX" in terms


def test_web_provider_metadata_links_candidate_without_global_fallback() -> None:
    instrument = _japanese_instrument()
    provider = GoogleNewsRssProvider(
        query='("Inpex Corporation" OR "4062.T")',
        candidate_symbol=instrument.symbol,
        transport=FakeTransport(
            """<rss><channel><item>
            <title>Japanese market update</title>
            <source>Tokyo Financial News</source>
            <link>https://example.test/inpex</link>
            <pubDate>Fri, 02 Oct 2026 08:00:00 GMT</pubDate>
            </item></channel></rss>"""
        ),
    )

    result = GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=(instrument,), as_of=datetime(2026, 10, 2, 9, tzinfo=UTC)
    )

    context = result.asset_contexts[instrument.symbol]
    assert context.freshness.value == "NEWS_FRESH"
    assert result.provider_status is NewsProviderStatus.AVAILABLE


def test_bing_news_rss_provider_uses_rss_endpoint_and_parses_candidate() -> None:
    transport = FakeTransport(
        """<?xml version="1.0"?><rss><channel><item>
        <title>Inpex Corporation reports results</title>
        <source>Example News</source>
        <link>https://example.test/inpex</link>
        <pubDate>Fri, 02 Oct 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>"""
    )
    provider = BingNewsRssProvider(
        query='"4062.T" "Inpex Corporation"',
        candidate_symbol="4062.T",
        locale="ja-JP",
        region="JP",
        transport=transport,
    )

    items = provider.fetch_global_news(as_of=datetime(2026, 10, 2, 9, tzinfo=UTC))

    assert len(items) == 1
    assert items[0].provider == "BING_NEWS_RSS"
    assert items[0].provider_symbols == ("4062.T",)
    assert provider.last_status is NewsProviderStatus.AVAILABLE
    assert "format=rss" in transport.urls[0]
    assert "setlang=ja-JP" in transport.urls[0]
