"""Real historical validation dataset builder using the existing data layer."""

from datetime import datetime

from app.data.models import DataProviderStatus
from app.data.registry import HistoricalDataProviderRegistry
from app.domain.universe import UniversalInstrument
from app.domain.versions import HISTORICAL_DATA_VERSION
from app.intelligence.models import MarketBar, TimeFrame
from app.validation.models import (
    EvidenceRequirements,
    HistoricalValidationDataset,
    HistoricalValidationDatasetBuild,
    InstrumentTimeframeCoverage,
)
from app.validation.replay import build_dataset_metadata

UNIVERSE_SELECTION_RULE_VERSION = (
    "fixed-liquid-research-universe-v1:"
    "resolve-symbols-first;filter-by-policy-and-historical-coverage;"
    "no-profitability-filter"
)


def build_real_historical_validation_datasets(
    *,
    registry: HistoricalDataProviderRegistry,
    instruments: tuple[UniversalInstrument, ...],
    timeframes: tuple[TimeFrame, ...],
    as_of: datetime,
    start: datetime | None = None,
    limit: int,
    requirements: EvidenceRequirements,
    universe_rule: str = UNIVERSE_SELECTION_RULE_VERSION,
) -> HistoricalValidationDatasetBuild:
    coverage: list[InstrumentTimeframeCoverage] = []
    datasets_by_timeframe: dict[TimeFrame, HistoricalValidationDataset] = {}
    selected_symbols: set[str] = set()
    rejected_symbols: set[str] = set()

    for timeframe in timeframes:
        bars_by_instrument: dict[str, tuple[MarketBar, ...]] = {}
        included_instruments: list[UniversalInstrument] = []
        for instrument in instruments:
            result = registry.fetch(
                instrument=instrument,
                timeframes=(timeframe,),
                as_of=as_of,
                limit=limit,
            )
            bars = tuple(
                bar
                for bar in result.bars_by_timeframe.get(timeframe, ())
                if start is None or bar.timestamp >= start
            )
            coverage.append(
                _coverage(
                    instrument=instrument,
                    timeframe=timeframe,
                    bars=bars,
                    status=result.status.value,
                    provider=_provider_name(result.provider_statuses),
                    data_quality=result.consistency_status.value,
                    cached=result.provider_statuses.get("cache") is DataProviderStatus.CACHE_HIT,
                    reasons=result.reasons,
                )
            )
            if _meets_bar_requirements(bars, requirements):
                bars_by_instrument[instrument.key] = bars
                included_instruments.append(instrument)
                selected_symbols.add(instrument.symbol)
            else:
                rejected_symbols.add(f"{instrument.symbol}:{timeframe.value}")
        if bars_by_instrument:
            metadata = build_dataset_metadata(
                provider="real-historical-data",
                instruments=tuple(included_instruments),
                bars_by_instrument=bars_by_instrument,
                timeframes=(timeframe,),
                created_at=as_of,
                mapping_version=HISTORICAL_DATA_VERSION,
            )
            datasets_by_timeframe[timeframe] = HistoricalValidationDataset(
                metadata=metadata,
                bars_by_instrument=bars_by_instrument,
            )

    return HistoricalValidationDatasetBuild(
        provider="real-historical-data",
        universe_rule=universe_rule,
        requested_timeframes=timeframes,
        selected_instruments=tuple(sorted(selected_symbols)),
        coverage=tuple(coverage),
        datasets_by_timeframe=datasets_by_timeframe,
        rejected_symbols=tuple(sorted(rejected_symbols)),
        broker_write=False,
        broker_write_calls=0,
        real_execution_available=False,
    )


def _coverage(
    *,
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
    bars: tuple[MarketBar, ...],
    status: str,
    provider: str,
    data_quality: str,
    cached: bool,
    reasons: tuple[str, ...],
) -> InstrumentTimeframeCoverage:
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    start = ordered[0].timestamp if ordered else None
    end = ordered[-1].timestamp if ordered else None
    span_days = (end - start).days if start is not None and end is not None else 0
    return InstrumentTimeframeCoverage(
        symbol=instrument.symbol,
        asset_class=instrument.asset_class,
        timeframe=timeframe,
        provider=provider,
        bar_count=len(ordered),
        start=start,
        end=end,
        span_days=span_days,
        status=status,
        data_quality=data_quality,
        cached=cached,
        reasons=reasons,
    )


def _meets_bar_requirements(
    bars: tuple[MarketBar, ...], requirements: EvidenceRequirements
) -> bool:
    if len(bars) < requirements.minimum_bars_per_instrument:
        return False
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    span_days = (ordered[-1].timestamp - ordered[0].timestamp).days
    return span_days >= requirements.minimum_historical_span_days


def _provider_name(statuses: dict[str, DataProviderStatus]) -> str:
    for provider, status in statuses.items():
        if status in {DataProviderStatus.SUCCESS, DataProviderStatus.CACHE_HIT}:
            return provider
    if statuses:
        return next(iter(statuses))
    return "none"
