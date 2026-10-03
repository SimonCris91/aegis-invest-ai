from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.config.loader import load_config
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.news.intelligence import (
    GlobalNewsIntelligenceEngine,
    NewsProviderError,
    NewsProviderStatus,
)
from app.orchestration.active_intelligence import (
    ActiveIntelligenceAuditStore,
    AegisActiveIntelligenceOrchestrator,
    _news_asset_contexts,
)
from app.orchestration.active_runtime import (
    ETORO_DEMO_RUNTIME_STATUS_KIND,
    EtoroDemoContinuousRunner,
    build_runtime_news_engine,
)
from app.storage.sqlite import SqliteRecordStore

AS_OF = datetime(2026, 9, 1, 12, tzinfo=UTC)


class RaisingProvider:
    provider_name = "fixture-live-news"

    def __init__(self, status: NewsProviderStatus) -> None:
        self.status = status

    def fetch_global_news(self, *, as_of: datetime) -> tuple[object, ...]:
        raise NewsProviderError(
            "synthetic provider failure",
            status=self.status,
            provider_error_message="safe-provider-detail",
        )


def _instrument() -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id="1",
        symbol="BTC",
        display_name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
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


def _bars(instrument: UniversalInstrument) -> tuple[MarketBar, ...]:
    return tuple(
        MarketBar(
            instrument=instrument,
            timestamp=AS_OF - timedelta(hours=69 - index),
            timeframe=TimeFrame.ONE_HOUR,
            open=Decimal("100") + index,
            high=Decimal("101") + index,
            low=Decimal("99") + index,
            close=Decimal("100") + index,
            volume=Decimal("1000"),
            currency=Currency.USD,
            source="fixture",
            data_quality=FeatureQuality.GOOD,
        )
        for index in range(70)
    )


def test_runtime_selects_alpha_vantage_without_fallback() -> None:
    config = load_config({"AEGIS_NEWS_PROVIDER": "alpha_vantage"})
    engine = build_runtime_news_engine(config, {"ALPHA_VANTAGE_API_KEY": "secret-not-for-output"})

    assert engine.provider_name == "ALPHA_VANTAGE"


def test_runtime_reuses_bounded_gdelt_provider_across_engine_rebuilds(monkeypatch) -> None:
    import app.orchestration.active_runtime as runtime

    instances = []

    class FakeGdeltProvider:
        provider_name = "GDELT_DOC"

        def __init__(self, *, cache_ttl, cache_path):
            self.cache_ttl = cache_ttl
            self.cache_path = cache_path
            instances.append(self)

    monkeypatch.setattr(runtime, "GdeltNewsProvider", FakeGdeltProvider)
    monkeypatch.setattr(runtime, "_RUNTIME_GDELT_PROVIDER", None)
    config = load_config({"AEGIS_NEWS_PROVIDER": "alpha_vantage"})
    values = {
        "ALPHA_VANTAGE_API_KEY": "test-key",
        "AEGIS_NEWS_SECONDARY_PROVIDER": "alpaca",
        "AEGIS_NEWS_GDELT_ENABLED": "true",
    }

    first = build_runtime_news_engine(config, values)
    second = build_runtime_news_engine(config, values)

    assert first.provider_name == "ALPHA_VANTAGE_PLUS_ALPACA_PLUS_GDELT"
    assert second.provider_name == first.provider_name
    assert len(instances) == 1
    assert instances[0].cache_ttl == timedelta(minutes=30)
    assert instances[0].cache_path.name == "gdelt-news-cache.json"


def test_missing_alpha_key_is_explicit_and_fail_closed() -> None:
    config = load_config({"AEGIS_NEWS_PROVIDER": "alpha_vantage"})
    result = build_runtime_news_engine(config, {}).analyze(
        instruments=(_instrument(),), as_of=AS_OF
    )

    assert result.provider_status is NewsProviderStatus.AUTH_FAILED
    assert result.provider_error_code == "AUTH_FAILED"
    assert result.asset_contexts["BTC"].freshness.value == "NEWS_SOURCE_UNAVAILABLE"
    assert "secret" not in str(result.model_dump()).casefold()


def test_provider_failure_does_not_abort_market_cycle_and_is_persisted(tmp_path: Path) -> None:
    instrument = _instrument()
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    result = AegisActiveIntelligenceOrchestrator(
        news_engine=GlobalNewsIntelligenceEngine(RaisingProvider(NewsProviderStatus.RATE_LIMITED)),
        audit_store=store,
    ).run_if_new_bar_cycle(
        scheduled_at=AS_OF,
        instruments=(instrument,),
        bars_by_symbol={"BTC": _bars(instrument)},
        portfolio=PortfolioSnapshot(as_of=AS_OF, currency=Currency.EUR, cash=Decimal("200")),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result is not None
    assert result.news_provider_status == "RATE_LIMITED"
    assert result.news_acquisition_error_code == "RATE_LIMITED"
    assert result.news_acquisition_error_detail_safe == "safe-provider-detail"
    assert result.broker_write_calls == 0


def test_news_status_is_persisted_in_runner_heartbeat_without_secret(tmp_path: Path) -> None:
    status_store = SqliteRecordStore(tmp_path / "runtime.sqlite3")
    runner = EtoroDemoContinuousRunner(
        run_once=lambda: {
            "status": "NO_CYCLE",
            "pilot_enabled": False,
            "demo_broker_write_calls": 0,
            "broker_write_calls_real": 0,
            "news_provider": "ALPHA_VANTAGE",
            "news_provider_status": "AUTH_FAILED",
            "news_scan_cutoff_timestamp": AS_OF.isoformat(),
            "news_scan_completed_at": AS_OF.isoformat(),
            "news_events_received": 0,
            "news_events_fresh": 0,
            "news_events_material": 0,
            "news_duplicates_ignored": 0,
            "news_event_digest": ({"headline": "Safe published headline"},),
            "news_asset_contexts": {
                "AAPL": {"freshness": "NEWS_FRESH"},
                "EMPTY": {
                    "freshness": "NEWS_SOURCE_UNAVAILABLE",
                    "aggregate_sentiment": "NEUTRAL",
                    "aggregate_relevance": "0",
                    "event_risk": "0",
                    "unique_event_count": 0,
                    "material_event_count": 0,
                    "event_summaries": [],
                    "news_risk_flags": [],
                },
            },
            "global_risk_context": {"freshness": "NEWS_FRESH"},
            "news_acquisition_error_code": "AUTH_FAILED",
            "news_acquisition_error_detail_safe": "safe-provider-detail",
            "api_key": "must-not-be-persisted",
        },
        sleeper=lambda _: None,
        status_store=status_store,
    )

    runner.run(max_iterations=1)
    persisted = status_store.list(ETORO_DEMO_RUNTIME_STATUS_KIND)[-1]

    assert persisted["news_provider"] == "ALPHA_VANTAGE"
    assert persisted["news_provider_status"] == "AUTH_FAILED"
    assert persisted["news_acquisition_error_code"] == "AUTH_FAILED"
    assert persisted["news_event_digest"] == [{"headline": "Safe published headline"}]
    assert persisted["news_asset_contexts"] == {"AAPL": {"freshness": "NEWS_FRESH"}}
    assert persisted["global_risk_context"] == {"freshness": "NEWS_FRESH"}
    assert "api_key" not in persisted
    assert "must-not-be-persisted" not in str(persisted)
    assert persisted["demo_broker_write_calls"] == 0
    assert persisted["broker_write_calls_real"] == 0


def test_news_projection_keeps_real_evidence_and_omits_only_empty_defaults() -> None:
    empty = {
        "freshness": "NEWS_SOURCE_UNAVAILABLE",
        "aggregate_sentiment": "NEUTRAL",
        "aggregate_relevance": "0.00",
        "event_risk": "0",
        "unique_event_count": 0,
        "material_event_count": 0,
        "event_summaries": (),
        "news_risk_flags": (),
    }
    with_evidence = {**empty, "event_risk": "0.7", "news_risk_flags": ("EVENT",)}
    projected = _news_asset_contexts(
        SimpleNamespace(asset_contexts={"EMPTY": empty, "EVENT": with_evidence})
    )

    assert "EMPTY" not in projected
    assert projected["EVENT"]["event_risk"] == "0.7"
    assert projected["EVENT"]["news_risk_flags"] == ("EVENT",)
