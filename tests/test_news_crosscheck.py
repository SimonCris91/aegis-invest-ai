from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.loader import load_config
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.universe import UniversalInstrument
from app.news.alpaca import AlpacaNewsProvider, alpaca_news_symbol
from app.news.crosscheck import CrossCheckedNewsProvider
from app.news.intelligence import (
    NewsProviderError,
    NewsProviderStatus,
    NewsSourceQuality,
    RawNewsItem,
)
from app.orchestration.active_runtime import build_runtime_news_engine

AS_OF = datetime(2026, 9, 10, 12, tzinfo=UTC)


class _Transport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        import json

        self.urls.append(url)
        self.headers.append(headers)
        return json.dumps(self.payload)


class _Provider:
    def __init__(self, name: str, items: tuple[RawNewsItem, ...]) -> None:
        self.provider_name = name
        self.items = items
        self.last_status = NewsProviderStatus.AVAILABLE
        self.last_diagnostics: dict[str, object] = {}

    def set_tickers(self, tickers: tuple[str, ...]) -> None:
        self.tickers = tickers

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        return tuple(item for item in self.items if item.published_at <= as_of)


def _instrument(
    symbol: str = "BTC", asset_class: AssetClass = AssetClass.CRYPTO
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id="1",
        symbol=symbol,
        display_name="Bitcoin" if symbol == "BTC" else symbol,
        asset_class=asset_class,
        currency=Currency.USD,
        exchange="TEST",
        market_status=MarketStatus.CONTINUOUS_24_7,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=AS_OF,
    )


def _item(headline: str, source: str, *, provider: str) -> RawNewsItem:
    return RawNewsItem(
        headline=headline,
        source=source,
        published_at=AS_OF - timedelta(hours=1),
        language="en",
        geographic_scope="GLOBAL",
        source_quality=NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
        provider=provider,
    )


def test_alpaca_news_is_causal_and_does_not_expose_headers() -> None:
    transport = _Transport(
        {
            "news": [
                {
                    "id": 1,
                    "headline": "War sanctions pressure markets",
                    "source": "Fixture Wire",
                    "created_at": "2026-09-10T11:00:00Z",
                    "updated_at": "2026-09-10T11:05:00Z",
                    "url": "https://example.test/news/1",
                    "symbols": ["BTCUSD"],
                },
                {
                    "id": 2,
                    "headline": "Future article",
                    "source": "Fixture Wire",
                    "created_at": "2026-09-10T13:00:00Z",
                    "symbols": ["BTCUSD"],
                },
            ]
        }
    )
    provider = AlpacaNewsProvider(
        api_key_id="alpaca-id-secret",
        api_secret_key="alpaca-secret-value",
        transport=transport,
        symbols=("CRYPTO:BTC",),
    )

    items = provider.fetch_global_news(as_of=AS_OF)

    assert len(items) == 1
    assert items[0].provider == "ALPACA_NEWS"
    assert "alpaca-secret-value" not in str(provider.last_diagnostics)
    assert transport.headers[0]["APCA-API-SECRET-KEY"] == "alpaca-secret-value"
    assert "BTCUSD" in transport.urls[0]


def test_alpaca_news_normalizes_etoro_session_suffix_and_skips_foreign_exchange() -> None:
    transport = _Transport({"news": []})
    provider = AlpacaNewsProvider(
        api_key_id="alpaca-id",
        api_secret_key="alpaca-secret",
        transport=transport,
        symbols=("AMAT.RTH", "RAYb.ST", "CRYPTO:BTC"),
    )

    provider.fetch_global_news(as_of=AS_OF)

    assert alpaca_news_symbol("AMAT.RTH") == "AMAT"
    assert alpaca_news_symbol("RAYb.ST") is None
    assert "symbols=AMAT%2CBTCUSD" in transport.urls[0]


def test_crosscheck_preserves_partial_primary_data_and_reports_status() -> None:
    primary = _Provider(
        "ALPHA_VANTAGE",
        (_item("War sanctions pressure markets", "Reuters", provider="ALPHA_VANTAGE"),),
    )

    class _Unavailable(_Provider):
        def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
            raise NewsProviderError("offline", status=NewsProviderStatus.RATE_LIMITED)

    result = CrossCheckedNewsProvider(
        primary, _Unavailable("ALPACA_NEWS", ())
    ).fetch_global_news(as_of=AS_OF)

    assert len(result) == 1


def test_crosscheck_cools_down_only_rate_limited_source_and_keeps_gdelt_fallback() -> None:
    class _RateLimited(_Provider):
        def __init__(self) -> None:
            super().__init__("ALPHA_VANTAGE", ())
            self.calls = 0

        def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
            self.calls += 1
            raise NewsProviderError(
                "rate limited", status=NewsProviderStatus.RATE_LIMITED
            )

    alpha = _RateLimited()
    gdelt = _Provider(
        "GDELT_DOC",
        (_item("Market risk from new sanctions", "GDELT outlet", provider="GDELT_DOC"),),
    )
    provider = CrossCheckedNewsProvider(alpha, gdelt)

    assert provider.fetch_global_news(as_of=AS_OF)
    assert provider.last_status is NewsProviderStatus.PARTIAL
    assert provider.fetch_global_news(as_of=AS_OF)

    assert alpha.calls == 1
    assert provider.last_diagnostics["suppressed_provider_count"] == 1
    assert provider.last_diagnostics["suppressed_provider_names"] == ("ALPHA_VANTAGE",)


def test_runtime_factory_can_select_alpha_plus_alpaca_without_real_writes() -> None:
    config = load_config({"AEGIS_NEWS_PROVIDER": "alpha_vantage"})
    engine = build_runtime_news_engine(
        config,
        {
            "ALPHA_VANTAGE_API_KEY": "alpha-secret",
            "AEGIS_NEWS_SECONDARY_PROVIDER": "alpaca",
            "ALPACA_API_KEY_ID": "alpaca-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret",
        },
    )

    assert engine.provider_name == "ALPHA_VANTAGE_PLUS_ALPACA"
