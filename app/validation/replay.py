"""Historical replay clock, dataset metadata, and period split helpers."""

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime

from app.data.models import EventRiskItem, NormalizedNewsItem
from app.domain.base import require_aware
from app.domain.enums import Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame
from app.validation.models import (
    PeriodSplit,
    PeriodSplitName,
    ResearchDatasetMetadata,
    WalkForwardConfig,
    WalkForwardWindow,
)


class FutureLeakageError(ValueError):
    """Raised when a replay component attempts to observe future data."""


class HistoricalReplayClock:
    def __init__(self, current_time: datetime) -> None:
        self._current_time = require_aware(current_time, "replay clock time")

    @property
    def now(self) -> datetime:
        return self._current_time

    def move_to(self, timestamp: datetime) -> None:
        self._current_time = require_aware(timestamp, "replay clock time")

    def assert_visible(self, timestamp: datetime, source: str) -> None:
        timestamp = require_aware(timestamp, source)
        if timestamp > self._current_time:
            raise FutureLeakageError(f"{source} is after replay time")

    def visible_bars(self, bars: Iterable[MarketBar]) -> tuple[MarketBar, ...]:
        return tuple(
            sorted((bar for bar in bars if bar.timestamp <= self.now), key=lambda x: x.timestamp)
        )

    def visible_news(self, items: Iterable[NormalizedNewsItem]) -> tuple[NormalizedNewsItem, ...]:
        return tuple(item for item in items if item.published_at <= self.now)

    def visible_events(self, items: Iterable[EventRiskItem]) -> tuple[EventRiskItem, ...]:
        return tuple(
            item for item in items if item.scheduled_at is None or item.scheduled_at <= self.now
        )


def build_dataset_metadata(
    *,
    provider: str,
    instruments: tuple[UniversalInstrument, ...],
    bars_by_instrument: dict[str, tuple[MarketBar, ...]],
    timeframes: tuple[TimeFrame, ...],
    created_at: datetime,
    mapping_version: str,
) -> ResearchDatasetMetadata:
    all_bars = tuple(bar for bars in bars_by_instrument.values() for bar in bars)
    if not all_bars:
        raise ValueError("validation dataset requires at least one historical bar")
    payload = tuple(
        {
            "instrument": key,
            "bars": tuple(
                {
                    "timestamp": bar.timestamp.isoformat(),
                    "timeframe": bar.timeframe.value,
                    "open": str(bar.open),
                    "high": str(bar.high),
                    "low": str(bar.low),
                    "close": str(bar.close),
                    "volume": str(bar.volume) if bar.volume is not None else None,
                }
                for bar in bars
            ),
        }
        for key, bars in sorted(bars_by_instrument.items())
    )
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ResearchDatasetMetadata(
        dataset_id=f"dataset-{digest[:16]}",
        provider=provider,
        instruments=tuple(instrument.symbol for instrument in instruments),
        asset_classes=tuple(
            sorted({instrument.asset_class for instrument in instruments}, key=str)
        ),
        timeframes=timeframes,
        start=min(bar.timestamp for bar in all_bars),
        end=max(bar.timestamp for bar in all_bars),
        currency=instruments[0].currency or Currency.USD,
        data_quality="GOOD",
        mapping_version=mapping_version,
        data_digest=digest,
        created_at=created_at,
    )


def split_periods(timestamps: tuple[datetime, ...]) -> tuple[PeriodSplit, ...]:
    ordered = tuple(sorted(timestamps))
    if len(ordered) < 6:
        raise ValueError("at least six timestamps are required for train/validation/OOS split")
    train_end_index = max(1, int(len(ordered) * 0.60) - 1)
    validation_end_index = max(train_end_index + 1, int(len(ordered) * 0.80) - 1)
    return (
        PeriodSplit(
            name=PeriodSplitName.TRAIN,
            start=ordered[0],
            end=ordered[train_end_index],
        ),
        PeriodSplit(
            name=PeriodSplitName.VALIDATION,
            start=ordered[train_end_index + 1],
            end=ordered[validation_end_index],
        ),
        PeriodSplit(
            name=PeriodSplitName.OUT_OF_SAMPLE,
            start=ordered[validation_end_index + 1],
            end=ordered[-1],
        ),
    )


class WalkForwardSplitter:
    def windows(
        self, timestamps: tuple[datetime, ...], config: WalkForwardConfig
    ) -> tuple[WalkForwardWindow, ...]:
        ordered = tuple(sorted(set(timestamps)))
        minimum = config.training_length + config.validation_length
        if len(ordered) < max(config.minimum_observations, minimum):
            return ()
        windows: list[WalkForwardWindow] = []
        start = 0
        while start + minimum <= len(ordered):
            train_end = start + config.training_length - 1
            validation_start = train_end + 1
            validation_end = validation_start + config.validation_length - 1
            windows.append(
                WalkForwardWindow(
                    window_index=len(windows) + 1,
                    train_start=ordered[start],
                    train_end=ordered[train_end],
                    validation_start=ordered[validation_start],
                    validation_end=ordered[validation_end],
                )
            )
            start += config.step_size
        return tuple(windows)
