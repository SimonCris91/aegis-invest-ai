"""CLI/runtime composition for safe offline strategy validation."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.models import ApplicationConfig
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.universe import UniversalInstrument
from app.domain.versions import HISTORICAL_DATA_VERSION
from app.intelligence.models import MarketBar, TimeFrame
from app.validation.diagnostics import enrich_validation_result
from app.validation.engine import HistoricalValidationEngine
from app.validation.models import (
    EvidenceRequirements,
    HistoricalValidationDataset,
    TransactionCostAssumptions,
)
from app.validation.replay import build_dataset_metadata
from app.validation.storage import DEFAULT_STRATEGY_VALIDATION_STORE_PATH, StrategyValidationStore


def build_strategy_validation_report(
    config: ApplicationConfig,
    *,
    offline_fixture: bool = True,
    store: StrategyValidationStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    requirements = EvidenceRequirements()
    if config.etoro_demo_execution_enabled:
        return _blocked("DEMO_EXECUTION_ENABLED", "Demo execution must remain false")
    if not offline_fixture:
        return _blocked("LIVE_DATASET_NOT_CONFIGURED", "real-data validation needs a dataset")
    dataset = _offline_fixture_dataset(now)
    result = enrich_validation_result(
        HistoricalValidationEngine(
            risk_policy=config.risk,
            strategy_config=config.strategy,
        ).run(
            dataset=dataset,
            cost_assumptions=TransactionCostAssumptions(),
            random_seed=80,
        ),
        requirements=requirements,
        timeframe=TimeFrame.ONE_DAY,
    )
    record_id = None
    if store is not None:
        record_id = store.record_result(result)
    return {
        "status": "RESEARCH_COMPLETE",
        "run_id": result.run_id,
        "dataset_id": result.dataset.dataset_id,
        "dataset_digest": result.dataset.data_digest,
        "record_store_path": str(DEFAULT_STRATEGY_VALIDATION_STORE_PATH),
        "persisted_record_id": record_id,
        "period_splits": tuple(split.name.value for split in result.period_splits),
        "walk_forward_windows": len(result.walk_forward_windows),
        "decisions": len(result.decisions),
        "trade_count": result.metrics.trade_count,
        "total_return": str(result.metrics.total_return),
        "maximum_drawdown": str(result.metrics.maximum_drawdown),
        "score_calibration_buckets": len(result.score_calibration),
        "confidence_calibration_buckets": len(result.confidence_calibration),
        "decision_funnel": (
            result.decision_funnel.model_dump(mode="json")
            if result.decision_funnel is not None
            else None
        ),
        "zero_trade_diagnostics": tuple(
            item.model_dump(mode="json") for item in result.zero_trade_diagnostics[:10]
        ),
        "research_matrix_rows": len(result.research_matrix),
        "evidence_passed": result.evidence_passed,
        "evidence_failures": result.evidence_failures,
        "qualification": result.qualification.status.value,
        "shadow_feed_eligible": result.qualification.shadow_feed_eligible,
        "demo_consideration_allowed": result.qualification.demo_consideration_allowed,
        "broker_write": False,
        "broker_write_calls": result.broker_write_calls,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": result.real_execution_available,
    }


def _offline_fixture_dataset(now: datetime) -> HistoricalValidationDataset:
    instruments = (
        _instrument("EQX", "9001", AssetClass.EQUITY, now=now),
        _instrument("ETFQ", "9002", AssetClass.ETF, now=now),
        _instrument("BTCT", "9003", AssetClass.CRYPTO, now=now),
    )
    bars_by_instrument = {
        instruments[0].key: _bars(instruments[0], now=now, pattern="up"),
        instruments[1].key: _bars(instruments[1], now=now, pattern="range"),
        instruments[2].key: _bars(instruments[2], now=now, pattern="volatile-up"),
    }
    metadata = build_dataset_metadata(
        provider="offline-fixture",
        instruments=instruments,
        bars_by_instrument=bars_by_instrument,
        timeframes=(TimeFrame.ONE_DAY,),
        created_at=now,
        mapping_version=HISTORICAL_DATA_VERSION,
    )
    return HistoricalValidationDataset(metadata=metadata, bars_by_instrument=bars_by_instrument)


def _instrument(
    symbol: str, instrument_id: str, asset_class: AssetClass, *, now: datetime
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="fixture",
        broker_instrument_id=instrument_id,
        symbol=symbol,
        display_name=f"{symbol} Fixture",
        asset_class=asset_class,
        currency=Currency.USD,
        market_status=MarketStatus.CONTINUOUS_24_7
        if asset_class is AssetClass.CRYPTO
        else MarketStatus.OPEN,
        tradeable=True,
        buy_allowed=True,
        sell_allowed=True,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("5"),
        metadata_timestamp=now,
    )


def _bars(instrument: UniversalInstrument, *, now: datetime, pattern: str) -> tuple[MarketBar, ...]:
    bars: list[MarketBar] = []
    start = now - timedelta(days=89)
    for index in range(90):
        if pattern == "range":
            close = Decimal("100") + Decimal(index % 6 - 3) * Decimal("0.30")
        elif pattern == "volatile-up":
            close = Decimal("80") + Decimal(index) * Decimal("0.35") + Decimal(((-1) ** index) * 2)
        else:
            close = Decimal("70") + Decimal(index) * Decimal("0.45")
        open_price = close - Decimal("0.10")
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=start + timedelta(days=index),
                timeframe=TimeFrame.ONE_DAY,
                open=open_price,
                high=max(open_price, close) + Decimal("0.25"),
                low=min(open_price, close) - Decimal("0.25"),
                close=close,
                volume=Decimal("100000") + Decimal(index * 100),
                currency=Currency.USD,
                source="offline-validation-fixture",
            )
        )
    return tuple(bars)


def _blocked(category: str, reason: str) -> dict[str, object]:
    return {
        "status": "BLOCKED",
        "category": category,
        "reason": reason,
        "record_store_path": str(DEFAULT_STRATEGY_VALIDATION_STORE_PATH),
        "broker_write": False,
        "broker_write_calls": 0,
        "real_execution_available": False,
    }
