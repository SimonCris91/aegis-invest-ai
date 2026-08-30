"""Offline report builders for news intelligence phases."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.models import ApplicationConfig
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.news.alpha_vantage import (
    ALPHA_VANTAGE_API_KEY_ENV,
    ALPHA_VANTAGE_NEWS_FUNCTION,
    AlphaVantageNewsProvider,
    alpha_vantage_request_plan,
    alpha_vantage_symbol_to_aegis,
    news_provider_readiness_matrix,
)
from app.news.intelligence import GlobalNewsIntelligenceEngine
from app.scanner.active import ActiveMarketScanner


def build_alpha_vantage_news_adapter_report(config: ApplicationConfig) -> dict[str, object]:
    as_of = datetime(2026, 8, 28, 16, tzinfo=UTC)
    instruments = _news_demo_instruments(as_of)
    provider = AlphaVantageNewsProvider(
        api_key="fixture-key",
        transport=_FixtureAlphaVantageTransport(_alpha_vantage_fixture_payload(as_of)),
        tickers=("AAPL", "CRYPTO:BTC"),
        topics=("economy_monetary", "earnings", "blockchain"),
    )
    result = GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=instruments,
        as_of=as_of,
    )
    scanner_without_news = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(instruments[0],),
        bars_by_symbol={"AAPL": _news_demo_bars(instruments[0], as_of=as_of)},
        portfolio=PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("200")),
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )
    scanner_with_news = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(instruments[0],),
        bars_by_symbol={"AAPL": _news_demo_bars(instruments[0], as_of=as_of)},
        portfolio=PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("200")),
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
        news_context_by_symbol=result.asset_contexts,
    )
    before = scanner_without_news.candidates[0]
    after = scanner_with_news.candidates[0]
    return {
        "status": "ALPHA_VANTAGE_NEWS_ADAPTER_READY",
        "phase": "STEP_9_0D_ALPHA_VANTAGE_NEWS_PROVIDER_ADAPTER",
        "adapter": {
            "provider": provider.provider_name,
            "function": ALPHA_VANTAGE_NEWS_FUNCTION,
            "required_env_var": ALPHA_VANTAGE_API_KEY_ENV,
            "network_calls_from_codex": 0,
            "credentials_modified": False,
        },
        "normalized_fields_supported": (
            "title/headline",
            "source",
            "url/reference",
            "publication timestamp",
            "ticker/entity sentiment through summary/provenance",
            "overall sentiment label through summary/provenance",
            "topics",
            "affected tickers",
            "provider provenance",
            "raw provider event/article identity where available",
        ),
        "topic_mapping": {
            "economy_monetary": "CENTRAL_BANK / INTEREST_RATES / INFLATION through text evidence",
            "economy_fiscal": "MACRO",
            "mergers_and_acquisitions": "MERGER_ACQUISITION",
            "blockchain": "CRYPTO",
            "earnings": "EARNINGS",
        },
        "symbol_mapping_examples": (
            alpha_vantage_symbol_to_aegis("AAPL"),
            alpha_vantage_symbol_to_aegis("CRYPTO:BTC"),
            alpha_vantage_symbol_to_aegis("FOREX:USD"),
        ),
        "provider_readiness": news_provider_readiness_matrix(),
        "polling_strategy": alpha_vantage_request_plan(
            universe_symbols=("AAPL", "SPY", "QQQ", "GLD", "BTC", "ETH", "SOL")
        ),
        "synthetic_events": tuple(
            event.model_dump(mode="json") for event in result.normalized_events
        ),
        "aapl_news_context": result.asset_contexts["AAPL"].model_dump(mode="json"),
        "btc_news_context": result.asset_contexts["BTC"].model_dump(mode="json"),
        "global_risk_context": result.global_risk_snapshot.model_dump(mode="json"),
        "scanner_integration": {
            "news_fields_present": after.news_context is not None,
            "market_opportunity_score_unchanged": (
                before.opportunity_score == after.opportunity_score
            ),
            "confidence_unchanged": before.confidence == after.confidence,
            "tradeproposal_triggered_by_news": False,
            "riskmanager_changed": False,
            "broker_write_calls": scanner_with_news.broker_write_calls,
        },
        "failure_contract": (
            "AVAILABLE",
            "RATE_LIMITED",
            "AUTH_FAILED",
            "PROVIDER_UNAVAILABLE",
            "MALFORMED_RESPONSE",
            "DELAYED",
        ),
        "next_real_news_step": "run one Windows read-only Alpha Vantage NEWS_SENTIMENT probe",
        "windows_powershell_pilot": (
            "$env:AEGIS_CONFIDENCE_PROFILE='V2_B_GUARDED'; "
            "$code=@'\n"
            "from datetime import UTC, datetime\n"
            "import json\n"
            "from app.config.loader import load_runtime_values\n"
            "from app.news.alpha_vantage import AlphaVantageNewsProvider\n"
            "values=load_runtime_values()\n"
            "provider=AlphaVantageNewsProvider(api_key=values.get('ALPHA_VANTAGE_API_KEY'), "
            "tickers=('AAPL','CRYPTO:BTC'), topics=('economy_monetary','earnings','blockchain'), "
            "limit=10)\n"
            "items=provider.fetch_global_news(as_of=datetime.now(UTC))\n"
            "print(json.dumps({'provider': provider.provider_name, 'articles': len(items), "
            "'diagnostics': provider.last_diagnostics, 'broker_write_calls': 0, "
            "'demo': False, 'real': False}, indent=2, default=str))\n"
            "'@; .\\.venv\\Scripts\\python.exe -c $code"
        ),
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


class _FixtureAlphaVantageTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.urls.append(url)
        return json.dumps(self.payload)


def _alpha_vantage_fixture_payload(as_of: datetime) -> dict[str, object]:
    return {
        "items": "5",
        "feed": (
            _alpha_article(
                title="Apple raises guidance after product launch",
                source="Reuters",
                time=as_of - timedelta(hours=2),
                topics=("earnings",),
                tickers=("AAPL",),
                sentiment="Bullish",
            ),
            _alpha_article(
                title="SEC crypto regulation creates Bitcoin token uncertainty",
                source="SEC",
                time=as_of - timedelta(hours=1),
                topics=("blockchain",),
                tickers=("CRYPTO:BTC",),
                sentiment="Bearish",
            ),
            _alpha_article(
                title="Federal Reserve signals inflation risk and possible rate hike",
                source="Federal Reserve",
                time=as_of - timedelta(hours=3),
                topics=("economy_monetary",),
                tickers=("SPY", "CRYPTO:BTC"),
                sentiment="Somewhat-Bearish",
            ),
            _alpha_article(
                title="Unknown private company announces financing",
                source="Fixture Source",
                time=as_of - timedelta(hours=1),
                topics=("financial_markets",),
                tickers=("UNKNOWN1",),
                sentiment="Neutral",
            ),
            _alpha_article(
                title="Apple raises guidance after product launch",
                source="CNBC",
                time=as_of - timedelta(minutes=90),
                topics=("earnings",),
                tickers=("AAPL",),
                sentiment="Bullish",
            ),
            _alpha_article(
                title="Apple faces legal lawsuit over platform fees",
                source="Reuters",
                time=as_of - timedelta(minutes=45),
                topics=("financial_markets",),
                tickers=("AAPL",),
                sentiment="Bearish",
            ),
            _alpha_article(
                title="Future article must remain hidden",
                source="Reuters",
                time=as_of + timedelta(hours=1),
                topics=("earnings",),
                tickers=("AAPL",),
                sentiment="Bullish",
            ),
        ),
    }


def _alpha_article(
    *,
    title: str,
    source: str,
    time: datetime,
    topics: tuple[str, ...],
    tickers: tuple[str, ...],
    sentiment: str,
) -> dict[str, object]:
    return {
        "title": title,
        "url": (
            "fixture://alpha-vantage/"
            f"{hashlib.sha256(f'{title}|{source}'.encode()).hexdigest()[:12]}"
        ),
        "time_published": time.strftime("%Y%m%dT%H%M%S"),
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


def _news_demo_instruments(as_of: datetime) -> tuple[UniversalInstrument, ...]:
    return (
        _news_demo_instrument("AAPL", AssetClass.EQUITY, "1001", as_of, "Apple"),
        _news_demo_instrument("SPY", AssetClass.ETF, "3001", as_of, "SPY"),
        _news_demo_instrument("QQQ", AssetClass.ETF, "3002", as_of, "QQQ"),
        _news_demo_instrument("GLD", AssetClass.ETF, "3003", as_of, "GLD"),
        _news_demo_instrument("BTC", AssetClass.CRYPTO, "4001", as_of, "Bitcoin"),
        _news_demo_instrument("ETH", AssetClass.CRYPTO, "4002", as_of, "Ethereum"),
    )


def _news_demo_instrument(
    symbol: str,
    asset_class: AssetClass,
    broker_instrument_id: str,
    as_of: datetime,
    display_name: str,
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id=broker_instrument_id,
        symbol=symbol,
        display_name=display_name,
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


def _news_demo_bars(
    instrument: UniversalInstrument, *, as_of: datetime, count: int = 80
) -> tuple[MarketBar, ...]:
    start = as_of - timedelta(days=count - 1)
    price = Decimal("100")
    bars = []
    for index in range(count):
        close = (price * Decimal("1.003")).quantize(Decimal("0.0001"))
        bars.append(
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
    return tuple(bars)
