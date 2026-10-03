"""Incremental eToro market acquisition and coherent 1H snapshot construction."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.data.historical.alpaca import AlpacaHistoricalMarketDataProvider
from app.data.historical.cache import HistoricalDataCache
from app.data.historical.etoro import _normalize_etoro_candles
from app.data.models import DataProviderError, ProviderInstrumentReference
from app.domain.enums import AssetClass, MarketStatus
from app.domain.universe import UniversalInstrument
from app.intelligence.models import MarketBar, TimeFrame
from app.orchestration.active_intelligence import causal_completed_bars
from app.scanner.active import _expected_completed_one_hour_bar_timestamp
from app.storage.sqlite import SqliteRecordStore


class MarketDataAcquisitionOutcome(StrEnum):
    UPDATED = "UPDATED"
    ALREADY_CURRENT = "ALREADY_CURRENT"
    MARKET_CLOSED_NO_NEW_BAR = "MARKET_CLOSED_NO_NEW_BAR"
    NO_DATA = "NO_DATA"
    HTTP_ERROR = "HTTP_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PARSE_ERROR = "PARSE_ERROR"
    CAUSALITY_REJECTED = "CAUSALITY_REJECTED"
    INCOMPLETE_BAR_REJECTED = "INCOMPLETE_BAR_REJECTED"
    FUTURE_BAR_REJECTED = "FUTURE_BAR_REJECTED"
    SESSION_NOT_EXPECTED = "SESSION_NOT_EXPECTED"
    RETRY_BACKOFF = "RETRY_BACKOFF"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    OTHER_ERROR = "OTHER_ERROR"


@dataclass(frozen=True)
class CoherentMarketSnapshot:
    target_completed_bar: datetime
    instruments: tuple[UniversalInstrument, ...]
    bars_by_symbol: dict[str, tuple[MarketBar, ...]]
    total_universe: int
    eligible_for_target_bar: int
    stale: int
    unavailable: int
    session_not_expected: int
    coverage_denominator: int
    coverage_ratio: Decimal
    minimum_coverage_ratio: Decimal
    eligible_by_session_group: dict[str, int]
    excluded_by_session_group: dict[str, int]
    target_completed_bars_by_session_group: dict[str, datetime]

    @property
    def coverage_sufficient(self) -> bool:
        return bool(self.instruments) and self.coverage_ratio >= self.minimum_coverage_ratio


def _classify_error(exc: EtoroApiError) -> MarketDataAcquisitionOutcome:
    if exc.status in {401, 403} or exc.category.value == "AUTH_API_PERMISSION_ERROR":
        return MarketDataAcquisitionOutcome.AUTH_ERROR
    if exc.status == 429:
        return MarketDataAcquisitionOutcome.RATE_LIMITED
    if exc.status is not None:
        return MarketDataAcquisitionOutcome.HTTP_ERROR
    if exc.transport_detail == "TIMEOUT":
        return MarketDataAcquisitionOutcome.TIMEOUT
    if exc.category.value in {"EDGE_WAF_BLOCK", "NETWORK_TRANSPORT_ERROR"}:
        return MarketDataAcquisitionOutcome.PROVIDER_UNAVAILABLE
    return MarketDataAcquisitionOutcome.OTHER_ERROR


def _backoff_seconds(outcome: MarketDataAcquisitionOutcome, failures: int) -> int:
    base = 300 if outcome is MarketDataAcquisitionOutcome.RATE_LIMITED else 60
    return int(min(3600, base * (2 ** min(max(failures - 1, 0), 4))))


class EtoroOneHourAcquisitionCoordinator:
    """Refresh a bounded deterministic slice and persist state for the whole universe."""

    def __init__(
        self,
        *,
        client: EtoroReadClient,
        cache: HistoricalDataCache,
        store: SqliteRecordStore,
        batch_size: int,
        concurrency: int,
        crypto_fallback_provider: AlpacaHistoricalMarketDataProvider | None = None,
    ) -> None:
        if batch_size <= 0 or concurrency <= 0:
            raise ValueError("acquisition batch size and concurrency must be positive")
        self._client = client
        self._cache = cache
        self._store = store
        self._batch_size = batch_size
        self._concurrency = concurrency
        self._crypto_fallback_provider = crypto_fallback_provider

    def refresh(
        self,
        *,
        instruments: tuple[UniversalInstrument, ...],
        as_of: datetime,
    ) -> tuple[dict[str, tuple[MarketBar, ...]], dict[str, object]]:
        ordered = tuple(sorted(instruments, key=lambda item: (item.symbol, item.key)))
        prior = self._store.market_acquisition_states(TimeFrame.ONE_HOUR.value)
        results: dict[str, dict[str, object]] = {}
        candidates: list[UniversalInstrument] = []
        expected = _expected_completed_one_hour_bar_timestamp(as_of)

        for instrument in ordered:
            instrument_id = instrument.broker_instrument_id
            latest = self._latest(instrument)
            state = prior.get(instrument_id)
            if _market_is_explicitly_closed(instrument):
                outcome = (
                    MarketDataAcquisitionOutcome.MARKET_CLOSED_NO_NEW_BAR
                    if latest is not None
                    else MarketDataAcquisitionOutcome.SESSION_NOT_EXPECTED
                )
                results[instrument_id] = self._result(instrument, outcome, latest)
                self._persist_cached_classification(
                    instrument=instrument,
                    outcome=outcome,
                    latest=latest,
                    prior=state,
                )
            elif latest is not None and latest >= expected:
                results[instrument_id] = self._result(
                    instrument, MarketDataAcquisitionOutcome.ALREADY_CURRENT, latest
                )
                self._persist_cached_classification(
                    instrument=instrument,
                    outcome=MarketDataAcquisitionOutcome.ALREADY_CURRENT,
                    latest=latest,
                    prior=state,
                )
            elif (
                state is not None
                and _after(as_of, state.get("retry_after"))
                and not _crypto_fallback_can_bypass_backoff(
                    instrument=instrument,
                    state=state,
                    fallback_provider=self._crypto_fallback_provider,
                )
            ):
                results[instrument_id] = self._result(
                    instrument,
                    MarketDataAcquisitionOutcome.RETRY_BACKOFF,
                    latest,
                    detail_code=f"BACKOFF_ACTIVE:{state['classification']}",
                )
            else:
                candidates.append(instrument)

        # Reserve alternating slots within each class for a broker-verified
        # open session and for discovery of the still-unverified universe.
        # This refreshes executable European/US markets promptly without
        # starving new instruments or allowing crypto to consume the batch.
        by_class: dict[AssetClass, dict[bool, list[UniversalInstrument]]] = {}
        for instrument in sorted(
            candidates,
            key=lambda item: _progress_key(prior.get(item.broker_instrument_id), item),
        ):
            verified_open = "session-state:OPEN_TRADABLE" in instrument.tags
            by_class.setdefault(instrument.asset_class, {True: [], False: []})[
                verified_open
            ].append(instrument)
        class_order = [
            asset_class
            for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO)
            if asset_class in by_class
        ]
        class_order.extend(
            asset_class for asset_class in sorted(by_class, key=lambda value: value.value)
            if asset_class not in class_order
        )
        selected: list[UniversalInstrument] = []
        class_turns: dict[AssetClass, int] = Counter()
        while len(selected) < self._batch_size and class_order:
            next_order: list[AssetClass] = []
            for asset_class in class_order:
                lanes = by_class[asset_class]
                prefer_verified = class_turns[asset_class] % 2 == 0
                bucket = lanes[prefer_verified] or lanes[not prefer_verified]
                selected.append(bucket.pop(0))
                class_turns[asset_class] += 1
                if len(selected) >= self._batch_size:
                    break
                if lanes[True] or lanes[False]:
                    next_order.append(asset_class)
            class_order = next_order
        attempted = tuple(selected)
        if attempted:
            latest_before = {item.key: self._latest(item) for item in attempted}
            with ThreadPoolExecutor(max_workers=min(self._concurrency, len(attempted))) as pool:
                fetched = tuple(
                    pool.map(
                        lambda item: self._fetch(item, as_of, latest_before[item.key]), attempted
                    )
                )
            for result in fetched:
                fetched_instrument = result.pop("instrument")
                assert isinstance(fetched_instrument, UniversalInstrument)
                completed = result.pop("completed_bars", ())
                if isinstance(completed, tuple) and completed:
                    data_provider = str(result.pop("data_provider", fetched_instrument.broker))
                    stats = self._cache.upsert_bars_with_stats(
                        provider=data_provider,
                        bars=completed,
                        fetched_at=as_of,
                        mapping=_mapping(fetched_instrument),
                    )
                    latest = max(bar.timestamp for bar in completed)
                    result["latest_valid_completed_bar"] = latest.isoformat()
                    result["outcome"] = (
                        MarketDataAcquisitionOutcome.UPDATED.value
                        if latest_before[fetched_instrument.key] is None
                        or latest > latest_before[fetched_instrument.key]
                        or int(stats["updated"]) > 0
                        else MarketDataAcquisitionOutcome.ALREADY_CURRENT.value
                    )
                self._persist_fetch(
                    instrument=fetched_instrument, as_of=as_of, result=result, prior=prior
                )
                results[fetched_instrument.broker_instrument_id] = result

        attempted_ids = {instrument.broker_instrument_id for instrument in attempted}
        for instrument in candidates:
            if instrument.broker_instrument_id in attempted_ids:
                continue
            instrument_id = instrument.broker_instrument_id
            state = prior.get(instrument_id)
            outcome = MarketDataAcquisitionOutcome.NOT_ATTEMPTED
            results[instrument_id] = self._result(
                instrument,
                outcome,
                self._latest(instrument),
                detail_code="BATCH_BUDGET_EXHAUSTED",
            )
            previous = state or {}
            self._persist_state(
                instrument=instrument,
                outcome=outcome,
                last_attempt_at=_timestamp(previous.get("last_attempt_at")),
                last_success_at=_timestamp(previous.get("last_success_at")),
                latest=self._latest(instrument),
                consecutive_failures=int(str(previous.get("consecutive_failures", 0))),
                retry_after=_timestamp(previous.get("retry_after")),
                detail_code="BATCH_BUDGET_EXHAUSTED",
            )

        current_states = self._store.market_acquisition_states(TimeFrame.ONE_HOUR.value)
        rows = tuple(
            _telemetry_row(
                instrument=instrument,
                result=results[instrument.broker_instrument_id],
                state=current_states.get(instrument.broker_instrument_id),
                as_of=as_of,
            )
            for instrument in ordered
        )
        counts = Counter(str(row["outcome"]) for row in rows)
        if sum(counts.values()) != len(ordered):
            raise RuntimeError("market acquisition classification did not reconcile")
        bars = {instrument.symbol: self._bars(instrument, as_of) for instrument in ordered}
        newest = max(
            (bar.timestamp for values in bars.values() for bar in values),
            default=None,
        )
        pending_count = (
            counts[MarketDataAcquisitionOutcome.NOT_ATTEMPTED.value]
            + counts[MarketDataAcquisitionOutcome.RETRY_BACKOFF.value]
        )
        usable_count = sum(
            counts[outcome.value]
            for outcome in (
                MarketDataAcquisitionOutcome.UPDATED,
                MarketDataAcquisitionOutcome.ALREADY_CURRENT,
                MarketDataAcquisitionOutcome.MARKET_CLOSED_NO_NEW_BAR,
                MarketDataAcquisitionOutcome.SESSION_NOT_EXPECTED,
            )
        )
        if pending_count:
            acquisition_status = "IN_PROGRESS"
        elif usable_count == len(ordered):
            acquisition_status = "COMPLETE"
        elif usable_count:
            acquisition_status = "PARTIAL"
        else:
            acquisition_status = "PROVIDER_UNAVAILABLE"
        telemetry: dict[str, object] = {
            "acquisition_attempted": bool(attempted),
            "acquisition_provider": "etoro",
            "acquisition_instruments_requested": len(ordered),
            "acquisition_instruments_attempted": len(attempted),
            "acquisition_instruments_not_attempted": counts[
                MarketDataAcquisitionOutcome.NOT_ATTEMPTED.value
            ],
            "acquisition_instruments_in_backoff": counts[
                MarketDataAcquisitionOutcome.RETRY_BACKOFF.value
            ],
            "acquisition_instruments_updated": counts[MarketDataAcquisitionOutcome.UPDATED.value],
            "acquisition_newest_completed_bar": None if newest is None else newest.isoformat(),
            "acquisition_outcome_counts": {
                outcome.value: counts[outcome.value] for outcome in MarketDataAcquisitionOutcome
            },
            "acquisition_results": rows,
            "acquisition_status": acquisition_status,
        }
        return bars, telemetry

    def _fetch(
        self,
        instrument: UniversalInstrument,
        as_of: datetime,
        latest_before: datetime | None,
    ) -> dict[str, object]:
        instrument_id = instrument.numeric_instrument_id
        if instrument_id is None:
            return {
                "instrument": instrument,
                **self._result(
                    instrument,
                    MarketDataAcquisitionOutcome.OTHER_ERROR,
                    None,
                    detail_code="NUMERIC_INSTRUMENT_ID_REQUIRED",
                ),
            }
        try:
            raw = self._client.candle_history(
                instrument_id=instrument_id,
                direction="asc",
                interval="OneHour",
                candles_count=61,
            )
        except EtoroApiError as exc:
            fallback = self._fetch_crypto_fallback(
                instrument=instrument,
                as_of=as_of,
                latest_before=latest_before,
                etoro_error=exc,
            )
            if fallback is not None:
                return fallback
            return {
                "instrument": instrument,
                **self._result(
                    instrument,
                    _classify_error(exc),
                    latest_before,
                    detail_code=exc.category.value,
                ),
            }
        except TimeoutError:
            return {
                "instrument": instrument,
                **self._result(
                    instrument,
                    MarketDataAcquisitionOutcome.TIMEOUT,
                    latest_before,
                    detail_code="TIMEOUT",
                ),
            }
        except Exception as exc:
            return {
                "instrument": instrument,
                **self._result(
                    instrument,
                    MarketDataAcquisitionOutcome.OTHER_ERROR,
                    latest_before,
                    detail_code=type(exc).__name__,
                ),
            }
        if not isinstance(raw, dict) or not isinstance(raw.get("candles"), list):
            return {
                "instrument": instrument,
                **self._result(
                    instrument,
                    MarketDataAcquisitionOutcome.PARSE_ERROR,
                    latest_before,
                    detail_code="INVALID_CANDLE_ENVELOPE",
                ),
            }
        try:
            normalized = _normalize_etoro_candles(
                raw, instrument=instrument, timeframe=TimeFrame.ONE_HOUR
            )
        except (TypeError, ValueError):
            return {
                "instrument": instrument,
                **self._result(
                    instrument,
                    MarketDataAcquisitionOutcome.PARSE_ERROR,
                    latest_before,
                    detail_code="INVALID_CANDLE_PAYLOAD",
                ),
            }
        if not normalized:
            return {
                "instrument": instrument,
                **self._result(instrument, MarketDataAcquisitionOutcome.NO_DATA, None),
            }
        completed_cutoff = _expected_completed_one_hour_bar_timestamp(as_of)
        visible = tuple(bar for bar in normalized if bar.timestamp <= completed_cutoff)
        completed = causal_completed_bars(
            bars_by_symbol=visible,
            instrument=instrument,
            as_of=as_of,
            timeframe=TimeFrame.ONE_HOUR,
        )
        if not completed:
            if all(bar.timestamp > as_of for bar in normalized):
                outcome = MarketDataAcquisitionOutcome.FUTURE_BAR_REJECTED
            elif any(bar.timestamp <= as_of for bar in normalized):
                outcome = MarketDataAcquisitionOutcome.INCOMPLETE_BAR_REJECTED
            else:
                outcome = MarketDataAcquisitionOutcome.CAUSALITY_REJECTED
            return {"instrument": instrument, **self._result(instrument, outcome, None)}
        return {
            "instrument": instrument,
            "outcome": MarketDataAcquisitionOutcome.ALREADY_CURRENT.value,
            "latest_valid_completed_bar": _iso(latest_before),
            "detail_code": None,
            "completed_bars": completed,
        }

    def _fetch_crypto_fallback(
        self,
        *,
        instrument: UniversalInstrument,
        as_of: datetime,
        latest_before: datetime | None,
        etoro_error: EtoroApiError,
    ) -> dict[str, object] | None:
        """Use Alpaca only when eToro's crypto candle read is unavailable."""
        if (
            instrument.asset_class is not AssetClass.CRYPTO
            or self._crypto_fallback_provider is None
            or etoro_error.category.value
            not in {"NETWORK_TRANSPORT_ERROR", "EDGE_WAF_BLOCK"}
        ):
            return None
        try:
            bars = self._crypto_fallback_provider.get_bars(
                instrument,
                TimeFrame.ONE_HOUR,
                as_of=as_of,
                limit=61,
            )
        except (DataProviderError, TimeoutError, OSError, ValueError, TypeError):
            return None
        completed = causal_completed_bars(
            bars_by_symbol=bars,
            instrument=instrument,
            as_of=as_of,
            timeframe=TimeFrame.ONE_HOUR,
        )
        if not completed:
            return None
        return {
            "instrument": instrument,
            "outcome": MarketDataAcquisitionOutcome.UPDATED.value,
            "latest_valid_completed_bar": _iso(latest_before),
            "detail_code": "FALLBACK_ALPACA_CRYPTO",
            "data_provider": "alpaca",
            "completed_bars": completed,
        }

    def _persist_fetch(
        self,
        *,
        instrument: UniversalInstrument,
        as_of: datetime,
        result: dict[str, object],
        prior: dict[str, dict[str, object]],
    ) -> None:
        outcome = MarketDataAcquisitionOutcome(str(result["outcome"]))
        previous = prior.get(instrument.broker_instrument_id, {})
        success = outcome in {
            MarketDataAcquisitionOutcome.UPDATED,
            MarketDataAcquisitionOutcome.ALREADY_CURRENT,
            MarketDataAcquisitionOutcome.MARKET_CLOSED_NO_NEW_BAR,
        }
        failures = 0 if success else int(str(previous.get("consecutive_failures", 0))) + 1
        self._persist_state(
            instrument=instrument,
            outcome=outcome,
            last_attempt_at=as_of,
            last_success_at=as_of if success else _timestamp(previous.get("last_success_at")),
            latest=_timestamp(result.get("latest_valid_completed_bar")),
            consecutive_failures=failures,
            retry_after=(
                None if success else as_of + timedelta(seconds=_backoff_seconds(outcome, failures))
            ),
            detail_code=(None if result.get("detail_code") is None else str(result["detail_code"])),
        )

    def _persist_state(
        self,
        *,
        instrument: UniversalInstrument,
        outcome: MarketDataAcquisitionOutcome,
        last_attempt_at: datetime | None,
        last_success_at: datetime | None,
        latest: datetime | None,
        consecutive_failures: int,
        retry_after: datetime | None,
        detail_code: str | None,
    ) -> None:
        self._store.upsert_market_acquisition_state(
            timeframe=TimeFrame.ONE_HOUR.value,
            state={
                "instrument_id": instrument.broker_instrument_id,
                "symbol": instrument.symbol,
                "last_attempt_at": _iso(last_attempt_at),
                "last_success_at": _iso(last_success_at),
                "latest_valid_completed_bar": _iso(latest),
                "classification": outcome.value,
                "consecutive_failures": consecutive_failures,
                "retry_after": _iso(retry_after),
                "detail_code": detail_code,
            },
        )

    def _persist_cached_classification(
        self,
        *,
        instrument: UniversalInstrument,
        outcome: MarketDataAcquisitionOutcome,
        latest: datetime | None,
        prior: dict[str, object] | None,
    ) -> None:
        state = prior or {}
        self._persist_state(
            instrument=instrument,
            outcome=outcome,
            last_attempt_at=_timestamp(state.get("last_attempt_at")),
            last_success_at=_timestamp(state.get("last_success_at")),
            latest=latest,
            consecutive_failures=0,
            retry_after=None,
            detail_code=None,
        )

    def _latest(self, instrument: UniversalInstrument) -> datetime | None:
        providers = (
            (instrument.broker, "alpaca")
            if instrument.asset_class is AssetClass.CRYPTO
            else (instrument.broker,)
        )
        timestamps = tuple(
            timestamp
            for provider in providers
            if (
                timestamp := self._cache.last_timestamp(
                    provider=provider,
                    broker=instrument.broker,
                    broker_instrument_id=instrument.broker_instrument_id,
                    timeframe=TimeFrame.ONE_HOUR,
                )
            ) is not None
        )
        return max(timestamps, default=None)

    def _bars(self, instrument: UniversalInstrument, as_of: datetime) -> tuple[MarketBar, ...]:
        providers = (
            (instrument.broker, "alpaca")
            if instrument.asset_class is AssetClass.CRYPTO
            else (instrument.broker,)
        )
        candidates = tuple(
            self._cache.get_bars(
                provider=provider,
                instrument_key=(instrument.broker, instrument.broker_instrument_id),
                timeframe=TimeFrame.ONE_HOUR,
                as_of=as_of,
                limit=60,
                instrument_factory=instrument.model_dump(mode="json"),
            )
            for provider in providers
        )
        return max(
            candidates,
            key=lambda bars: max(
                (_comparable_timestamp(bar.timestamp) for bar in bars),
                default=datetime.min.replace(tzinfo=timezone.utc),
            ),
        )

    @staticmethod
    def _result(
        instrument: UniversalInstrument,
        outcome: MarketDataAcquisitionOutcome,
        latest: datetime | None,
        *,
        detail_code: str | None = None,
    ) -> dict[str, object]:
        return {
            "outcome": outcome.value,
            "latest_valid_completed_bar": _iso(latest),
            "detail_code": detail_code,
        }


def build_coherent_one_hour_snapshot(
    *,
    instruments: tuple[UniversalInstrument, ...],
    bars_by_symbol: dict[str, tuple[MarketBar, ...]],
    as_of: datetime,
    minimum_coverage_ratio: Decimal,
    minimum_bars: int = 60,
    include_closed_for_ranking: bool = False,
) -> CoherentMarketSnapshot:
    """Build a causal snapshot for ranking and/or execution.

    Closed markets remain excluded by default for the execution-oriented
    snapshot.  The global Demo scanner can opt in to cached, causal bars from
    closed sessions so one currently open exchange cannot monopolise the
    cross-region comparison.
    """
    target = _expected_completed_one_hour_bar_timestamp(as_of)
    eligible: list[UniversalInstrument] = []
    eligible_bars: dict[str, tuple[MarketBar, ...]] = {}
    stale = 0
    unavailable = 0
    session_not_expected = 0
    eligible_groups: Counter[str] = Counter()
    excluded_groups: Counter[str] = Counter()
    completed_by_instrument: dict[str, tuple[MarketBar, ...]] = {}
    session_targets: dict[str, datetime] = {}
    for instrument in instruments:
        group = _session_group(instrument)
        if _market_is_explicitly_closed(instrument) and not include_closed_for_ranking:
            session_not_expected += 1
            excluded_groups[group] += 1
            continue
        if _market_is_not_eligible_for_session(instrument) or (
            instrument.market_status is MarketStatus.UNKNOWN and not include_closed_for_ranking
        ):
            unavailable += 1
            excluded_groups[group] += 1
            continue
        completed = causal_completed_bars(
            bars_by_symbol=bars_by_symbol.get(instrument.symbol, ()),
            instrument=instrument,
            as_of=as_of,
            timeframe=TimeFrame.ONE_HOUR,
        )
        if len(completed) < minimum_bars:
            unavailable += 1
            excluded_groups[group] += 1
            continue
        completed_by_instrument[instrument.key] = completed
        current_target = session_targets.get(group)
        if current_target is None or completed[-1].timestamp > current_target:
            session_targets[group] = completed[-1].timestamp

    for instrument in instruments:
        session_completed = completed_by_instrument.get(instrument.key)
        if session_completed is None:
            continue
        group = _session_group(instrument)
        if session_completed[-1].timestamp != session_targets[group]:
            stale += 1
            excluded_groups[group] += 1
            continue
        eligible.append(instrument)
        eligible_bars[instrument.symbol] = session_completed[-minimum_bars:]
        eligible_groups[group] += 1
    total = len(instruments)
    coverage_denominator = total - session_not_expected
    ratio = (
        Decimal(len(eligible)) / Decimal(coverage_denominator)
        if coverage_denominator
        else Decimal("0")
    )
    return CoherentMarketSnapshot(
        target_completed_bar=target,
        instruments=tuple(eligible),
        bars_by_symbol=eligible_bars,
        total_universe=total,
        eligible_for_target_bar=len(eligible),
        stale=stale,
        unavailable=unavailable,
        session_not_expected=session_not_expected,
        coverage_denominator=coverage_denominator,
        coverage_ratio=ratio,
        minimum_coverage_ratio=minimum_coverage_ratio,
        eligible_by_session_group=dict(sorted(eligible_groups.items())),
        excluded_by_session_group=dict(sorted(excluded_groups.items())),
        target_completed_bars_by_session_group=dict(sorted(session_targets.items())),
    )


def _mapping(instrument: UniversalInstrument) -> ProviderInstrumentReference:
    return ProviderInstrumentReference(
        provider=instrument.broker,
        provider_symbol=instrument.symbol,
        broker=instrument.broker,
        broker_symbol=instrument.symbol,
        broker_instrument_id=instrument.broker_instrument_id,
        exchange=instrument.exchange,
        asset_class=instrument.asset_class,
        currency=instrument.currency,
        mapping_confidence=Decimal("1"),
        mapping_source="etoro-native-instrument-id",
        verified=True,
    )


def _progress_key(
    state: dict[str, object] | None, instrument: UniversalInstrument
) -> tuple[bool, bool, str, str]:
    """Prioritize unattempted work, then the oldest failed/unfinished work."""
    last_success = None if state is None else state.get("last_success_at")
    last_attempt = None if state is None else state.get("last_attempt_at")
    failures = 0 if state is None else int(str(state.get("consecutive_failures", 0)))
    if last_success is not None:
        return (True, False, str(last_success), instrument.key)
    return (
        False,
        failures > 0,
        str(last_attempt or ""),
        instrument.key,
    )


def _market_is_explicitly_closed(instrument: UniversalInstrument) -> bool:
    """Use broker/session evidence; do not apply a US clock to a global catalog."""
    return instrument.market_status in {
        MarketStatus.CLOSED,
        MarketStatus.HALTED,
    }


def _market_is_not_eligible_for_session(instrument: UniversalInstrument) -> bool:
    """Reject only explicit non-tradability at the data-snapshot layer.

    A broker session refresh can leave a global catalog as UNKNOWN outside
    local trading hours. Those instruments may still be ranked when they have
    causal cached bars; fresh broker resolution and Demo preflight remain the
    execution gate later in the pipeline.
    """
    return instrument.tradeable is False


def _session_group(instrument: UniversalInstrument) -> str:
    if instrument.asset_class is AssetClass.CRYPTO or (
        instrument.market_status is MarketStatus.CONTINUOUS_24_7
    ):
        return "CONTINUOUS_24_7"
    venue = instrument.exchange or instrument.market
    return f"{venue or 'UNVERIFIED_VENUE'}::{instrument.market_status.value}"


def _telemetry_row(
    *,
    instrument: UniversalInstrument,
    result: dict[str, object],
    state: dict[str, object] | None,
    as_of: datetime,
) -> dict[str, object]:
    persisted = state or {}
    latest = _timestamp(result.get("latest_valid_completed_bar")) or _timestamp(
        persisted.get("latest_valid_completed_bar")
    )
    staleness = None if latest is None else max(0, int((as_of - latest).total_seconds()))
    return {
        "instrument_id": instrument.broker_instrument_id,
        "symbol": instrument.symbol,
        "session_group": _session_group(instrument),
        "outcome": str(result["outcome"]),
        "detail_code": result.get("detail_code"),
        "consecutive_failures": int(str(persisted.get("consecutive_failures", 0))),
        "last_success_at": persisted.get("last_success_at"),
        "last_attempt_at": persisted.get("last_attempt_at"),
        "retry_after": persisted.get("retry_after"),
        "staleness_age_seconds": staleness,
    }


def _after(as_of: datetime, raw: object) -> bool:
    value = _timestamp(raw)
    return value is not None and value > as_of


def _timestamp(raw: object) -> datetime | None:
    if raw is None or isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw))


def _comparable_timestamp(value: datetime) -> datetime:
    """Normalize cache/provider timestamps before comparing them.

    SQLite-backed cache rows can contain naive UTC datetimes while external
    providers normally return timezone-aware UTC datetimes.  Treat naive
    values as UTC so a provider fallback cannot fail while selecting the
    newest cached bar.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _crypto_fallback_can_bypass_backoff(
    *,
    instrument: UniversalInstrument,
    state: dict[str, object],
    fallback_provider: AlpacaHistoricalMarketDataProvider | None,
) -> bool:
    """Allow a configured crypto fallback to recover from stale eToro backoff.

    A previous eToro transport failure must not suppress the first attempt to
    use a healthy alternate provider.  Other classifications keep their
    normal retry policy so this does not turn the acquisition loop into a
    busy retry cycle.
    """
    return (
        fallback_provider is not None
        and instrument.asset_class is AssetClass.CRYPTO
        and str(state.get("classification", "")) == MarketDataAcquisitionOutcome.PROVIDER_UNAVAILABLE.value
        and str(state.get("detail_code", "")) in {"NETWORK_TRANSPORT_ERROR", "EDGE_WAF_BLOCK"}
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
