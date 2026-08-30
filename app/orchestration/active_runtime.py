"""Offline runtime report for the active intelligence orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.models import ApplicationConfig
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.news.intelligence import (
    GlobalNewsIntelligenceEngine,
    NewsFeedProvider,
    NewsSourceQuality,
    RawNewsItem,
)
from app.orchestration.active_intelligence import (
    ActiveIntelligenceCycleRecord,
    AegisActiveIntelligenceOrchestrator,
)


def build_active_intelligence_orchestrator_report(
    config: ApplicationConfig,
) -> dict[str, object]:
    start = datetime(2026, 8, 28, 9, tzinfo=UTC)
    end = datetime(2026, 8, 28, 18, tzinfo=UTC)
    instruments = _orchestrator_demo_instruments(start)
    bars_by_symbol = {
        instrument.symbol: _orchestrator_demo_bars(instrument, start=start, end=end)
        for instrument in instruments
    }
    news_engine = GlobalNewsIntelligenceEngine(
        NewsFeedProvider(items=_orchestrator_demo_news(start))
    )
    portfolio = PortfolioSnapshot(
        as_of=start,
        currency=Currency.EUR,
        cash=Decimal("170"),
        positions=(
            Position(
                position_id="shadow-aapl",
                instrument_id=1001,
                symbol="AAPL",
                settlement_type=SettlementType.REAL,
                units=Decimal("0.15"),
                average_entry_price=Decimal("190"),
                market_price=Decimal("200"),
            ),
        ),
    )
    orchestrator = AegisActiveIntelligenceOrchestrator(news_engine=news_engine)
    records = tuple(
        orchestrator.run_cycle(
            scheduled_at=scheduled_at,
            instruments=instruments,
            bars_by_symbol=bars_by_symbol,
            portfolio=portfolio.model_copy(update={"as_of": scheduled_at}),
            timeframe=TimeFrame.ONE_HOUR,
            shadow_capital=Decimal("200"),
        )
        for scheduled_at in orchestrator.schedule(start=start, end=end)
    )
    unavailable = AegisActiveIntelligenceOrchestrator(
        news_engine=GlobalNewsIntelligenceEngine(NewsFeedProvider(unavailable=True))
    ).run_cycle(
        scheduled_at=start,
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        portfolio=portfolio,
        timeframe=TimeFrame.ONE_HOUR,
        shadow_capital=Decimal("200"),
    )
    return {
        "status": "ACTIVE_INTELLIGENCE_ORCHESTRATOR_READY",
        "phase": "STEP_9_0E_ACTIVE_INTELLIGENCE_ORCHESTRATOR",
        "orchestrator_architecture": (
            "CLOCK T",
            "ingest latest causal market state",
            "ingest latest causal news/events",
            "update GLOBAL_RISK_CONTEXT",
            "update per-asset NEWS_CONTEXT",
            "run ActiveMarketScanner",
            "monitor existing positions",
            "detect meaningful state changes",
            "persist immutable audit cycle",
        ),
        "cadence": {
            "default_minutes": 10,
            "meaning": "scan cadence; does not imply trading cadence",
            "one_hour_market_bar_handling": (
                "same 1H bar may be reused across 10-minute cycles; no fabricated bar"
            ),
        },
        "synthetic_full_day_timeline": tuple(_timeline_payload(record) for record in records),
        "example_cycle_output": records[0].model_dump(mode="json"),
        "provider_unavailable_example": unavailable.model_dump(mode="json"),
        "audit_model": {
            "store": "work/active-intelligence-cycles.sqlite3",
            "record_kind": "active-intelligence-cycle",
            "immutable_fields": (
                "cycle_id",
                "scheduled_at",
                "market_data_timestamp",
                "news_cutoff_timestamp",
                "scanner_result",
                "global_risk_context",
                "broker_write_calls",
            ),
        },
        "real_provider_integration_points": {
            "market": ("Alpaca", "eToro read-only", "future Massive/other provider"),
            "news": ("Alpha Vantage", "future Massive News", "official macro feeds"),
            "remaining_blockers": (
                "Windows validation of fresh 1H market data",
                "one real read-only Alpha Vantage news probe",
                "persistent scheduled runner for prospective shadow mode",
            ),
        },
        "euro_200_context": {
            "shadow_capital": "200",
            "available_simulated_cash": str(records[0].available_simulated_cash),
            "existing_exposure": str(records[0].existing_exposure),
            "allocation_diagnostics": records[0].allocation_diagnostics,
        },
        "safety": {
            "news_can_trigger_trade": False,
            "riskmanager_changed": False,
            "strategy_thresholds_changed": False,
            "broker_write_calls": sum(record.broker_write_calls for record in records),
            "demo_execution_enabled": config.etoro_demo_execution_enabled,
            "real_execution_available": False,
        },
        "broker_write": False,
        "broker_write_calls": sum(record.broker_write_calls for record in records),
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def _timeline_payload(record: ActiveIntelligenceCycleRecord) -> dict[str, object]:
    return {
        "time": record.scheduled_at.strftime("%H:%M"),
        "change": record.change_classification.value,
        "market_data_timestamp": (
            None
            if record.market_data_timestamp is None
            else record.market_data_timestamp.isoformat()
        ),
        "fresh_news_events": record.fresh_news_events,
        "duplicate_events_ignored": record.duplicate_events_ignored,
        "material_events": record.material_events,
        "top_opportunities": record.top_opportunities,
        "watchlist": record.watchlist,
        "no_trade": record.no_trade,
        "rejected": record.rejected,
        "data_health": record.data_health_state.value,
        "positions_monitored": record.positions_monitored,
        "decision_changes": record.decision_change_events,
        "broker_write_calls": record.broker_write_calls,
    }


def _orchestrator_demo_news(start: datetime) -> tuple[RawNewsItem, ...]:
    return (
        _raw_news(
            "Apple raises guidance after product launch", start + timedelta(hours=1, minutes=20)
        ),
        _raw_news(
            "Federal Reserve signals inflation risk and possible rate hike",
            start + timedelta(hours=1, minutes=30),
            source="Federal Reserve",
            quality=NewsSourceQuality.PRIMARY_OFFICIAL,
        ),
        _raw_news(
            "Federal Reserve signals inflation risk and possible rate hike",
            start + timedelta(hours=1, minutes=35),
            source="Reuters",
        ),
        _raw_news(
            "SEC crypto regulation creates Bitcoin token uncertainty",
            start + timedelta(hours=2, minutes=30),
            source="SEC",
            quality=NewsSourceQuality.REGULATORY_GOVERNMENT,
        ),
        _raw_news(
            "Apple faces legal lawsuit over platform fees", start + timedelta(hours=3, minutes=10)
        ),
    )


def _raw_news(
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
        geographic_scope="US",
    )


def _orchestrator_demo_instruments(as_of: datetime) -> tuple[UniversalInstrument, ...]:
    return (
        _instrument("AAPL", AssetClass.EQUITY, "1001", as_of, "Apple"),
        _instrument("SPY", AssetClass.ETF, "3001", as_of, "SPY"),
        _instrument("BTC", AssetClass.CRYPTO, "4001", as_of, "Bitcoin"),
    )


def _instrument(
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


def _orchestrator_demo_bars(
    instrument: UniversalInstrument,
    *,
    start: datetime,
    end: datetime,
) -> tuple[MarketBar, ...]:
    warmup_start = start - timedelta(hours=65)
    rows: list[MarketBar] = []
    price = Decimal("100")
    current = warmup_start
    while current <= end:
        drift = Decimal("1.001") if current.minute == 0 else Decimal("1")
        close = (price * drift).quantize(Decimal("0.0001"))
        rows.append(
            MarketBar(
                instrument=instrument,
                timestamp=current,
                timeframe=TimeFrame.ONE_HOUR,
                open=price,
                high=max(price, close) * Decimal("1.01"),
                low=min(price, close) * Decimal("0.99"),
                close=close,
                volume=Decimal("100000"),
                currency=Currency.USD,
                source="synthetic-offline-fixture",
                data_quality=FeatureQuality.GOOD,
            )
        )
        price = close
        current += timedelta(hours=1)
    return tuple(rows)
