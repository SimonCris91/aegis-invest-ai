"""Step 9.0E active intelligence orchestrator tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

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
    ActiveIntelligenceAuditStore,
    AegisActiveIntelligenceOrchestrator,
    CycleChangeClassification,
    DataHealthState,
    DuplicateCycleError,
)
from app.storage.sqlite import SqliteRecordStore


def test_ten_minute_scheduler_and_duplicate_cycle_rejection() -> None:
    start = datetime(2026, 8, 28, 10, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    orchestrator = AegisActiveIntelligenceOrchestrator()
    instrument = _instrument("BTC", AssetClass.CRYPTO, "4001", start)
    bars = {"BTC": _bars(instrument, end=start, count=70)}

    schedule = orchestrator.schedule(start=start, end=end)
    orchestrator.run_cycle(
        scheduled_at=schedule[0],
        instruments=(instrument,),
        bars_by_symbol=bars,
        portfolio=_portfolio(start),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert tuple(item.strftime("%H:%M") for item in schedule) == (
        "10:00",
        "10:10",
        "10:20",
        "10:30",
    )
    with pytest.raises(DuplicateCycleError):
        orchestrator.run_cycle(
            scheduled_at=schedule[0],
            instruments=(instrument,),
            bars_by_symbol=bars,
            portfolio=_portfolio(start),
            timeframe=TimeFrame.ONE_HOUR,
        )


def test_future_market_data_and_future_news_are_not_visible() -> None:
    as_of = datetime(2026, 8, 28, 10, tzinfo=UTC)
    instrument = _instrument("BTC", AssetClass.CRYPTO, "4001", as_of)
    bars = _bars(instrument, end=as_of, count=70) + (
        _bar(instrument, timestamp=as_of + timedelta(hours=1), price=Decimal("999")),
    )
    news_engine = GlobalNewsIntelligenceEngine(
        NewsFeedProvider(
            items=(
                _news("Bitcoin regulation uncertainty", as_of - timedelta(minutes=5)),
                _news("Bitcoin bullish approval tomorrow", as_of + timedelta(minutes=5)),
            )
        )
    )

    record = AegisActiveIntelligenceOrchestrator(news_engine=news_engine).run_cycle(
        scheduled_at=as_of,
        instruments=(instrument,),
        bars_by_symbol={"BTC": bars},
        portfolio=_portfolio(as_of),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert record.market_data_timestamp == as_of
    assert record.fresh_news_events == 1
    assert "future" not in str(record.scanner_result).casefold()
    assert record.broker_write_calls == 0


def test_same_market_bar_can_be_reused_while_news_changes_between_bars() -> None:
    start = datetime(2026, 8, 28, 10, tzinfo=UTC)
    instrument = _instrument("BTC", AssetClass.CRYPTO, "4001", start)
    bars = {"BTC": _bars(instrument, end=start, count=70)}
    orchestrator = AegisActiveIntelligenceOrchestrator(
        news_engine=GlobalNewsIntelligenceEngine(
            NewsFeedProvider(
                items=(
                    _news(
                        "SEC crypto regulation creates Bitcoin uncertainty",
                        start + timedelta(minutes=5),
                    ),
                )
            )
        )
    )

    first = orchestrator.run_cycle(
        scheduled_at=start,
        instruments=(instrument,),
        bars_by_symbol=bars,
        portfolio=_portfolio(start),
        timeframe=TimeFrame.ONE_HOUR,
    )
    second = orchestrator.run_cycle(
        scheduled_at=start + timedelta(minutes=10),
        instruments=(instrument,),
        bars_by_symbol=bars,
        portfolio=_portfolio(start + timedelta(minutes=10)),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert first.market_data_timestamp == second.market_data_timestamp
    assert first.fresh_news_events == 0
    assert second.fresh_news_events == 1
    assert second.change_classification in {
        CycleChangeClassification.NEWS_STATE_CHANGED,
        CycleChangeClassification.MULTIPLE_CHANGES,
    }


def test_duplicate_news_ignored_and_global_risk_change_detected() -> None:
    start = datetime(2026, 8, 28, 10, tzinfo=UTC)
    instrument = _instrument("SPY", AssetClass.ETF, "3001", start)
    headline = "Federal Reserve signals inflation risk and possible rate hike"
    orchestrator = AegisActiveIntelligenceOrchestrator(
        news_engine=GlobalNewsIntelligenceEngine(
            NewsFeedProvider(
                items=(
                    _news(
                        headline,
                        start + timedelta(minutes=5),
                        source="Federal Reserve",
                        quality=NewsSourceQuality.PRIMARY_OFFICIAL,
                    ),
                    _news(headline, start + timedelta(minutes=6), source="Reuters"),
                )
            )
        )
    )

    first = orchestrator.run_cycle(
        scheduled_at=start,
        instruments=(instrument,),
        bars_by_symbol={"SPY": _bars(instrument, end=start, count=70)},
        portfolio=_portfolio(start),
        timeframe=TimeFrame.ONE_HOUR,
    )
    second = orchestrator.run_cycle(
        scheduled_at=start + timedelta(minutes=10),
        instruments=(instrument,),
        bars_by_symbol={"SPY": _bars(instrument, end=start, count=70)},
        portfolio=_portfolio(start + timedelta(minutes=10)),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert first.fresh_news_events == 0
    assert second.fresh_news_events == 2
    assert second.duplicate_events_ignored == 1
    assert second.global_risk_context["monetary_policy_risk"] != "0"


def test_provider_unavailable_and_stale_market_state_are_explicit() -> None:
    as_of = datetime(2026, 8, 28, 20, tzinfo=UTC)
    instrument = _instrument("BTC", AssetClass.CRYPTO, "4001", as_of)
    record = AegisActiveIntelligenceOrchestrator(
        news_engine=GlobalNewsIntelligenceEngine(NewsFeedProvider(unavailable=True))
    ).run_cycle(
        scheduled_at=as_of,
        instruments=(instrument,),
        bars_by_symbol={"BTC": _bars(instrument, end=as_of - timedelta(hours=8), count=70)},
        portfolio=_portfolio(as_of),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert record.data_health_state is DataHealthState.STALE
    scanner_candidates = cast(list[dict[str, Any]], record.scanner_result["candidates"])
    assert scanner_candidates[0]["news_sentiment"] == "NEUTRAL"
    assert record.broker_write_calls == 0


def test_position_monitoring_every_cycle_and_immutable_audit_order(tmp_path: Path) -> None:
    start = datetime(2026, 8, 28, 14, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", start)
    portfolio = PortfolioSnapshot(
        as_of=start,
        currency=Currency.EUR,
        cash=Decimal("170"),
        positions=(
            Position(
                position_id="p1",
                instrument_id=1001,
                symbol="AAPL",
                settlement_type=SettlementType.REAL,
                units=Decimal("0.1"),
                average_entry_price=Decimal("100"),
                market_price=Decimal("110"),
            ),
        ),
    )
    store = ActiveIntelligenceAuditStore(SqliteRecordStore(tmp_path / "cycles.sqlite3"))
    orchestrator = AegisActiveIntelligenceOrchestrator(audit_store=store)

    first = orchestrator.run_cycle(
        scheduled_at=start,
        instruments=(instrument,),
        bars_by_symbol={"AAPL": _bars(instrument, end=start, count=70)},
        portfolio=portfolio,
        timeframe=TimeFrame.ONE_HOUR,
    )
    second = orchestrator.run_cycle(
        scheduled_at=start + timedelta(minutes=10),
        instruments=(instrument,),
        bars_by_symbol={"AAPL": _bars(instrument, end=start, count=70)},
        portfolio=portfolio.model_copy(update={"as_of": start + timedelta(minutes=10)}),
        timeframe=TimeFrame.ONE_HOUR,
    )

    persisted = store.cycles()
    assert first.positions_monitored == 1
    assert second.positions_monitored == 1
    assert second.change_classification is CycleChangeClassification.NO_MATERIAL_CHANGE
    assert tuple(row["cycle_id"] for row in persisted) == (first.cycle_id, second.cycle_id)
    assert all(row["broker_write_calls"] == 0 for row in persisted)


def _instrument(
    symbol: str,
    asset_class: AssetClass,
    broker_instrument_id: str,
    as_of: datetime,
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id=broker_instrument_id,
        symbol=symbol,
        display_name=symbol,
        asset_class=asset_class,
        currency=Currency.USD,
        exchange="TEST",
        market_status=MarketStatus.CONTINUOUS_24_7
        if asset_class is AssetClass.CRYPTO
        else MarketStatus.OPEN,
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
    end: datetime,
    count: int,
) -> tuple[MarketBar, ...]:
    start = end - timedelta(hours=count - 1)
    return tuple(
        _bar(instrument, timestamp=start + timedelta(hours=index), price=Decimal("100") + index)
        for index in range(count)
    )


def _bar(
    instrument: UniversalInstrument,
    *,
    timestamp: datetime,
    price: Decimal,
) -> MarketBar:
    close = price.quantize(Decimal("0.0001"))
    return MarketBar(
        instrument=instrument,
        timestamp=timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        open=close,
        high=close * Decimal("1.01"),
        low=close * Decimal("0.99"),
        close=close,
        volume=Decimal("100000"),
        currency=Currency.USD,
        source="fixture",
        data_quality=FeatureQuality.GOOD,
    )


def _news(
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
    )


def _portfolio(as_of: datetime) -> PortfolioSnapshot:
    return PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("200"))
