"""Step 8.0A real historical validation and evidence-building tests."""

import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest

import app.data.runtime
import app.validation.datasets
import app.validation.diagnostics
import app.validation.runtime
from app.brokers.etoro.client import EtoroReadClient
from app.brokers.etoro.mapping import (
    asset_class_from_etoro_instrument_type,
    classify_etoro_instrument_metadata,
    safe_instrument_classification_metadata,
)
from app.brokers.models import InstrumentResolution
from app.config.loader import load_config, load_runtime_values
from app.config.models import ApplicationConfig, RiskPolicyConfig
from app.data.historical.alpaca import AlpacaHistoricalMarketDataProvider
from app.data.historical.cache import HistoricalDataCache
from app.data.historical.polygon import PolygonHistoricalMarketDataProvider
from app.data.historical.providers import _http_transport_category, _sanitize_url
from app.data.models import DataProviderError, DataProviderStatus, ProviderInstrumentReference
from app.data.quality import HistoricalDataQualityAnalyzer
from app.data.registry import HistoricalDataProviderRegistry, HistoricalProviderEntry
from app.data.runtime import (
    build_alpaca_core_4h_backfill_report,
    build_alpaca_full_backfill_report,
    build_alpaca_provider_pilot_report,
    build_etoro_instrument_schema_report,
    build_exit_evidence_acquisition_report,
    build_lifecycle_walkforward_readiness_report,
    build_lifecycle_zero_trade_forensics_report,
    build_policy_strategy_diagnostics,
    build_polygon_provider_pilot_report,
    build_real_strategy_validation_report,
    build_runtime_forensics_report,
)
from app.domain.enums import (
    AssetClass,
    Currency,
    HoldingPeriod,
    MarketStatus,
    RiskDecisionStatus,
    RiskViolationCode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem, InstrumentMetadata, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.domain.universe import UniversalInstrument
from app.intelligence.confidence import compute_v2b_signal_reliability_confidence
from app.intelligence.models import (
    AegisDecision,
    MarketBar,
    RegimeLabel,
    ScoreBand,
    StrategyDirection,
    TimeFrame,
)
from app.intelligence.profiles import (
    asset_strategy_profiles_for_confidence_profile,
    guarded_v2b_asset_strategy_profiles,
    legacy_profile_for,
    legacy_v1_asset_strategy_profiles,
    profile_for,
)
from app.main.__main__ import main
from app.policies.defaults import DEFAULT_POLICY_VERSION, default_asset_policy_engine
from app.policies.engine import AssetPolicyEngine
from app.policies.models import AssetPolicy
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SqliteRecordStore
from app.validation.confidence import (
    build_confidence_ablation_study,
    build_confidence_v2_research_study,
)
from app.validation.datasets import build_real_historical_validation_datasets
from app.validation.diagnostics import (
    build_decision_funnel,
    build_threshold_sensitivity,
    diagnose_zero_or_low_trades,
)
from app.validation.metrics import NOT_ENOUGH_DATA
from app.validation.models import (
    EvidenceRequirements,
    ReplayDecision,
    ReplayDecisionStatus,
)
from app.validation.storage import StrategyValidationStore


def _now() -> datetime:
    return datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _delta(timeframe: TimeFrame) -> timedelta:
    return {
        TimeFrame.INTRADAY: timedelta(minutes=1),
        TimeFrame.ONE_HOUR: timedelta(hours=1),
        TimeFrame.FOUR_HOUR: timedelta(hours=4),
        TimeFrame.ONE_DAY: timedelta(days=1),
        TimeFrame.ONE_WEEK: timedelta(days=7),
    }[timeframe]


def _instrument(
    symbol: str = "AAPL",
    *,
    instrument_id: int = 10_001,
    asset_class: AssetClass = AssetClass.EQUITY,
    broker: str = "research",
) -> UniversalInstrument:
    return UniversalInstrument(
        broker=broker,
        broker_instrument_id=str(instrument_id),
        symbol=symbol,
        display_name=f"{symbol} Test",
        asset_class=asset_class,
        currency=Currency.USD,
        exchange="US" if asset_class in {AssetClass.EQUITY, AssetClass.ETF} else None,
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
        metadata_timestamp=_now(),
    )


def _bars(
    instrument: UniversalInstrument,
    *,
    timeframe: TimeFrame = TimeFrame.ONE_DAY,
    count: int = 120,
    end_at: datetime | None = None,
    start_price: Decimal = Decimal("90"),
    step: Decimal = Decimal("0.20"),
    source: str = "fixture-real-history",
) -> tuple[MarketBar, ...]:
    interval = _delta(timeframe)
    end = end_at or (_now() - interval)
    bars: list[MarketBar] = []
    for index in range(count):
        timestamp = end - interval * (count - index - 1)
        close = start_price + Decimal(index) * step
        open_price = close - Decimal("0.10")
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=timestamp,
                timeframe=timeframe,
                open=open_price,
                high=close + Decimal("0.25"),
                low=open_price - Decimal("0.25"),
                close=close,
                volume=Decimal("1000000") + Decimal(index),
                currency=Currency.USD,
                source=source,
            )
        )
    return tuple(bars)


class StaticHistoricalProvider:
    def __init__(
        self,
        provider_name: str,
        payloads: Mapping[tuple[str, TimeFrame], tuple[MarketBar, ...]],
    ) -> None:
        self._provider_name = provider_name
        self._payloads = dict(payloads)
        self.calls: list[tuple[str, TimeFrame]] = []

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def supported_timeframes(self) -> tuple[TimeFrame, ...]:
        return tuple({timeframe for _, timeframe in self._payloads})

    def get_bars(
        self,
        instrument: UniversalInstrument,
        timeframe: TimeFrame,
        *,
        as_of: datetime,
        limit: int,
    ) -> tuple[MarketBar, ...]:
        self.calls.append((instrument.symbol, timeframe))
        return tuple(
            bar
            for bar in self._payloads.get((instrument.key, timeframe), ())[-limit:]
            if bar.timestamp <= as_of
        )


class FakeEtoroHistoryClient:
    def __init__(self, *, search_has_classification: bool = True) -> None:
        self.calls: list[str] = []
        self.search_has_classification = search_has_classification

    def resolve_instrument(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> InstrumentResolution:
        self.calls.append(f"resolve:{symbol}")
        asset_type = {
            "AAPL": "Stock",
            "MSFT": "Stock",
            "NVDA": "Stock",
            "SPY": "ETF",
            "QQQ": "Exchange Traded Fund",
            "VTI": "ETF",
            "BTC": "Crypto",
            "ETH": "Crypto Asset",
            "SOL": "Crypto Asset",
        }[symbol]
        instrument_id = {
            "AAPL": 1001,
            "MSFT": 1002,
            "NVDA": 1003,
            "SPY": 1004,
            "QQQ": 1005,
            "VTI": 1006,
            "BTC": 1007,
            "ETH": 1008,
            "SOL": 1009,
        }[symbol]
        return InstrumentResolution(
            instrument_id=instrument_id,
            symbol=symbol,
            internal_symbol_full=symbol,
            display_name=f"{symbol} resolved",
            instrument_type=asset_type if self.search_has_classification else None,
            classification_metadata=(
                {"instrumentType": asset_type} if self.search_has_classification else {}
            ),
            classification_evidence_source="search",
            classification_status="RAW_METADATA_RETAINED",
            market_status=MarketStatus.CONTINUOUS_24_7 if symbol == "BTC" else MarketStatus.OPEN,
            is_currently_tradable=True,
            is_buy_enabled=True,
            is_hidden_from_client=False,
            is_delisted=False,
            is_active_in_platform=True,
            current_rate=Decimal("100"),
            resolved=True,
            structurally_supported=True,
            structural_status="SUPPORTED",
            verified=True,
            as_of=as_of or _now(),
        )

    def raw_instrument_search(self, symbol: str) -> object:
        self.calls.append(f"raw-search:{symbol}")
        resolution = self.resolve_instrument(symbol, as_of=_now())
        return {
            "items": [
                {
                    "instrumentId": resolution.instrument_id,
                    "internalSymbolFull": resolution.internal_symbol_full,
                    "displayname": resolution.display_name,
                    "instrumentType": resolution.instrument_type,
                }
            ]
        }

    def instrument_metadata(self, instrument_ids: tuple[int, ...]) -> dict[int, dict[str, object]]:
        self.calls.append(f"metadata:{','.join(str(item) for item in instrument_ids)}")
        return {
            instrument_id: {
                "instrumentId": instrument_id,
                "instrumentType": {
                    1001: "Stocks",
                    1002: "Stocks",
                    1003: "Stocks",
                    1004: "ETF",
                    1005: "ETF",
                    1006: "ETF",
                    1007: "Crypto",
                    1008: "Crypto",
                    1009: "Crypto",
                }[instrument_id],
            }
            for instrument_id in instrument_ids
        }

    def instrument_type_names(self) -> dict[int, str]:
        self.calls.append("instrument-types")
        return {1: "Stocks", 2: "ETF", 3: "Crypto"}

    def candle_history(
        self,
        *,
        instrument_id: int,
        direction: str,
        interval: str,
        candles_count: int,
    ) -> object:
        self.calls.append(f"candles:{instrument_id}:{interval}:{direction}:{candles_count}")
        symbol = {
            1001: "AAPL",
            1002: "MSFT",
            1003: "NVDA",
            1004: "SPY",
            1005: "QQQ",
            1006: "VTI",
            1007: "BTC",
            1008: "ETH",
            1009: "SOL",
        }[instrument_id]
        asset_class = {
            1001: AssetClass.EQUITY,
            1002: AssetClass.EQUITY,
            1003: AssetClass.EQUITY,
            1004: AssetClass.ETF,
            1005: AssetClass.ETF,
            1006: AssetClass.ETF,
            1007: AssetClass.CRYPTO,
            1008: AssetClass.CRYPTO,
            1009: AssetClass.CRYPTO,
        }[instrument_id]
        timeframe = {
            "OneHour": TimeFrame.ONE_HOUR,
            "FourHours": TimeFrame.FOUR_HOUR,
            "OneDay": TimeFrame.ONE_DAY,
            "OneWeek": TimeFrame.ONE_WEEK,
        }.get(interval, TimeFrame.ONE_DAY)
        instrument = _instrument(
            symbol,
            instrument_id=instrument_id,
            asset_class=asset_class,
            broker="etoro",
        )
        bars = _bars(instrument, timeframe=timeframe, count=120, source="etoro")
        return {
            "candles": [
                {
                    "candles": [
                        {
                            "fromDate": bar.timestamp.isoformat().replace("+00:00", "Z"),
                            "open": str(bar.open),
                            "high": str(bar.high),
                            "low": str(bar.low),
                            "close": str(bar.close),
                            "volume": str(bar.volume) if bar.volume is not None else None,
                        }
                        for bar in bars
                    ]
                }
            ]
        }


def _decision(
    *,
    score: Decimal,
    confidence: Decimal,
    status: ReplayDecisionStatus = ReplayDecisionStatus.HOLD,
    blocker_reasons: tuple[str, ...] = (),
    strategy_directions: tuple[StrategyDirection, ...] = (),
) -> ReplayDecision:
    return ReplayDecision(
        timestamp=_now(),
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        score=score,
        score_band=ScoreBand.STRONG,
        confidence=confidence,
        regime=RegimeLabel.UPTREND,
        aegis_decision=AegisDecision.HOLD,
        status=status,
        ensemble_direction=StrategyDirection.WATCH,
        strategy_directions=strategy_directions,
        forward_return=Decimal("0.01"),
        blocker_reasons=blocker_reasons,
    )


def _decision_with_confidence_decomposition(
    *,
    score: Decimal,
    confidence: Decimal,
    forward_return: Decimal,
    asset_class: AssetClass = AssetClass.EQUITY,
    regime: RegimeLabel = RegimeLabel.UPTREND,
    quality: str = "PARTIAL",
) -> ReplayDecision:
    data_quality_score = Decimal("65") if quality == "PARTIAL" else Decimal("90")
    return _decision(
        score=score,
        confidence=confidence,
        strategy_directions=(StrategyDirection.BUY, StrategyDirection.HOLD),
    ).model_copy(
        update={
            "asset_class": asset_class,
            "regime": regime,
            "forward_return": forward_return,
            "confidence_decomposition": {
                "strategy_confidences": (),
                "mean_base_strategy_confidence": "0.7000",
                "agreement_ratio": "0.60",
                "agreement_multiplier": "0.80",
                "feature_quality_state": quality,
                "feature_quality_multiplier": "0.70" if quality == "PARTIAL" else "1",
                "insufficient_feature_names": ("macd", "spread") if quality == "PARTIAL" else (),
                "regime_confidence": "0.55",
                "regime_multiplier": "0.55",
                "strong_disagreement_multiplier": "1",
                "portfolio_fit_status": "POSITIVE",
                "portfolio_fit_score": "75",
                "news_status": "NEWS_NOT_CONFIGURED",
                "news_confidence": "0",
                "liquidity_score": "45",
                "data_quality_score": str(data_quality_score),
                "ensemble_confidence_before_clamp": "0.2156",
                "ensemble_confidence_after_rounding": "0.22",
                "opportunity_confidence_formula": (
                    "(ensemble_confidence + regime_confidence + data_quality_score/100) / 3"
                ),
                "opportunity_confidence_before_rounding": str(confidence),
                "normalized_final_confidence": str(confidence),
            },
        }
    )


def _risk_proposal(asset_class: AssetClass, settlement_type: SettlementType) -> TradeProposal:
    return TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-0000000080AA"),
        idempotency_key="step80a-policy-proposal",
        created_at=_now(),
        instrument_id=80_100,
        symbol="POLICY",
        asset_class=asset_class,
        side=TradeSide.BUY,
        intent=TradeIntent.OPEN,
        amount=Decimal("50"),
        currency=Currency.USD,
        target_weight=Decimal("0.05"),
        current_weight=Decimal("0"),
        leverage=1,
        settlement_type=settlement_type,
        reason="policy wiring regression",
        evidence=(
            EvidenceItem(
                source="test",
                timestamp=_now(),
                summary="deterministic evidence",
                confidence=Decimal("0.90"),
            ),
        ),
        confidence=Decimal("0.90"),
        risk_factors=("market risk",),
        invalidation_conditions=("test invalidation",),
        expected_holding_period=HoldingPeriod.MONTHS,
    )


def _risk_context(asset_class: AssetClass, settlement_type: SettlementType) -> RiskContext:
    portfolio = PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.USD,
        cash=Decimal("1000"),
        positions=(),
        reported_total_value=Decimal("1000"),
        peak_value=Decimal("1000"),
    )
    return RiskContext(
        evaluated_at=_now(),
        portfolio=portfolio,
        price=PriceSnapshot(
            instrument_id=80_100,
            symbol="POLICY",
            price=Decimal("100"),
            as_of=_now(),
            source="test",
        ),
        instrument=InstrumentMetadata(
            instrument_id=80_100,
            symbol="POLICY",
            asset_class=asset_class,
            settlement_type=settlement_type,
            is_valid=True,
            is_tradable=True,
            allows_long=True,
            allows_short=False,
            allowed_leverages=(1,),
            min_position_amount=Decimal("1"),
            metadata_as_of=_now(),
            source="test",
        ),
        market_data_available=True,
        news_data_available=True,
        daily_new_trade_count=0,
    )


def test_real_dataset_builder_records_coverage_and_never_fabricates_missing_timeframes(
    tmp_path: Path,
) -> None:
    aapl = _instrument("AAPL", asset_class=AssetClass.EQUITY)
    spy = _instrument("SPY", instrument_id=10_002, asset_class=AssetClass.ETF)
    provider = StaticHistoricalProvider(
        "fixture-real-history",
        {
            (aapl.key, TimeFrame.ONE_DAY): _bars(aapl),
            (spy.key, TimeFrame.ONE_DAY): _bars(spy),
            (aapl.key, TimeFrame.ONE_HOUR): _bars(aapl, timeframe=TimeFrame.ONE_HOUR, count=10),
        },
    )
    registry = HistoricalDataProviderRegistry(
        (HistoricalProviderEntry(provider, priority=1),),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
    )

    build = build_real_historical_validation_datasets(
        registry=registry,
        instruments=(aapl, spy),
        timeframes=(TimeFrame.ONE_DAY, TimeFrame.ONE_HOUR),
        as_of=_now(),
        limit=120,
        requirements=EvidenceRequirements(),
    )

    assert TimeFrame.ONE_DAY in build.datasets_by_timeframe
    assert TimeFrame.ONE_HOUR not in build.datasets_by_timeframe
    assert len(build.datasets_by_timeframe[TimeFrame.ONE_DAY].bars_by_instrument) == 2
    assert any(item.timeframe is TimeFrame.ONE_HOUR for item in build.coverage)
    assert "SPY:1H" in build.rejected_symbols
    assert build.broker_write_calls == 0
    assert provider.calls


def test_real_strategy_validation_runtime_with_injected_read_client_is_sanitized(
    tmp_path: Path,
) -> None:
    client = FakeEtoroHistoryClient()

    payload = build_real_strategy_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        store=StrategyValidationStore(SqliteRecordStore(tmp_path / "strategy-validation.sqlite3")),
        clock=_now,
        timeframe="1D",
        max_instruments=9,
    )

    assert payload["status"] == "REAL_DATA_RESEARCH_COMPLETE"
    assert payload["real_dataset"] is True
    assert payload["broker_write"] is False
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert payload["validation_runs"]
    assert payload["decision_funnel"]
    assert payload["research_matrix"]
    assert all(str(call).startswith(("resolve:", "candles:")) for call in client.calls)
    serialized = json.dumps(payload, sort_keys=True)
    assert "ETORO_API_KEY" not in serialized
    assert "ETORO_USER_KEY" not in serialized
    assert "x-api-key" not in serialized
    assert "x-user-key" not in serialized


def test_real_validation_forensics_separate_strategy_votes_from_final_decisions(
    tmp_path: Path,
) -> None:
    payload = build_real_strategy_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, FakeEtoroHistoryClient()),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        store=StrategyValidationStore(SqliteRecordStore(tmp_path / "strategy-validation.sqlite3")),
        clock=_now,
        timeframe="1D",
        max_instruments=9,
    )

    run = cast(tuple[dict[str, Any], ...], payload["validation_runs"])[0]
    funnel = cast(dict[str, Any], run["decision_funnel"])
    forensics = cast(dict[str, Any], run["decision_forensics"])

    assert "buy_signals" in funnel
    assert "final_buy_decisions" in funnel
    assert "final_hold_decisions" in funnel
    assert forensics["confidence_scale"] == "0..1"
    assert forensics["score_scale"] == "0..100"
    assert forensics["strategy_vote_funnel"]
    assert forensics["final_decision_funnel"]
    assert cast(dict[str, Any], forensics["gate_failure_counts"])
    trace = cast(tuple[dict[str, Any], ...], forensics["representative_traces"])[0]
    assert trace["proposal_gates"]
    confidence_gate = next(
        gate
        for gate in cast(tuple[dict[str, Any], ...], trace["proposal_gates"])
        if gate["gate"] == "confidence"
    )
    assert confidence_gate["scale"] == "0..1"
    assert trace["confidence_decomposition"]
    assert "normalized_final_confidence" in cast(dict[str, Any], trace["confidence_decomposition"])
    assert "cumulative_gate_funnel" in forensics
    assert "root_gate_failures" in forensics
    assert "downstream_decision_outcomes" in forensics
    cumulative = cast(dict[str, int], forensics["cumulative_gate_funnel"])
    assert cumulative["total_candidates"] == funnel["candidates_analyzed"]
    assert cumulative["score_pass"] + cumulative["score_fail"] == cumulative["total_candidates"]


def test_risk_manager_forensics_trace_v2b_confidence_and_thresholds() -> None:
    decision = _decision(
        score=Decimal("80"),
        confidence=Decimal("0.5550"),
        status=ReplayDecisionStatus.RISK_REJECTED,
        strategy_directions=(StrategyDirection.BUY,),
    ).model_copy(
        update={
            "proposal_id": "00000000-0000-0000-0000-0000000080aa",
            "risk_reasons": (),
            "proposal_gate_trace": (
                {
                    "gate": "confidence",
                    "actual": "0.5550",
                    "threshold": "0.5475",
                    "passed": True,
                    "confidence_model_version": "V2_B_GUARDED_V1",
                    "confidence_semantics_version": "SIGNAL_RELIABILITY_V2",
                },
                {
                    "gate": "risk_manager_confidence",
                    "actual": "0.5550",
                    "threshold": "0.5475",
                    "passed": True,
                    "scale": "0..1",
                    "field_read": "TradeProposal.confidence",
                    "threshold_provenance": "EMPIRICALLY_CALIBRATED_GUARDED",
                    "risk_policy_profile_version": "RiskPolicyConfig",
                    "asset_policy_version": "asset-policy-v1",
                },
                {
                    "gate": "risk_manager_stale_price",
                    "actual": "0.0",
                    "threshold": "300",
                    "passed": True,
                    "price_timestamp": _now().isoformat(),
                    "reference_timestamp": _now().isoformat(),
                },
                {
                    "gate": "risk_manager_authorization",
                    "actual": "APPROVED",
                    "threshold": "APPROVED",
                    "passed": True,
                },
            ),
        }
    )
    forensics = app.data.runtime._decision_forensics_payload((decision,))
    matrix = cast(tuple[dict[str, Any], ...], forensics["risk_manager_proposal_matrix"])
    aggregates = cast(dict[str, Any], forensics["risk_manager_proposal_aggregates"])

    assert matrix
    row = matrix[0]
    assert row["confidence_model_version"] == "V2_B_GUARDED_V1"
    assert row["confidence_semantics_version"] == "SIGNAL_RELIABILITY_V2"
    assert row["risk_manager_confidence_field"] == "TradeProposal.confidence"
    assert row["risk_manager_confidence_input"] == row["v2_b_confidence"]
    assert row["risk_manager_minimum_threshold"] == "0.5475"
    assert row["risk_manager_threshold_provenance"] == "EMPIRICALLY_CALIBRATED_GUARDED"
    assert row["risk_manager_confidence_scale"] == "0..1"
    assert row["risk_manager_authorized"] is True
    assert aggregates["proposal_count"] == len(matrix)
    sequential = cast(dict[str, int], aggregates["sequential_risk_funnel"])
    assert sequential["risk_manager_reached"] == len(matrix)
    assert sequential["risk_authorization_created"] == 1


def test_historical_risk_freshness_uses_replay_reference_time() -> None:
    proposal = _risk_proposal(AssetClass.EQUITY, SettlementType.REAL).model_copy(
        update={"confidence": Decimal("0.90")}
    )
    context = _risk_context(AssetClass.EQUITY, SettlementType.REAL)
    risk_manager = RiskManager(RiskPolicyConfig(), KillSwitch(active=False))
    fresh = risk_manager.evaluate(proposal, context)

    stale_context = context.model_copy(
        update={
            "evaluated_at": context.evaluated_at + timedelta(seconds=301),
        }
    )
    stale = risk_manager.evaluate(proposal, stale_context)

    assert fresh.decision.status is RiskDecisionStatus.APPROVED
    assert RiskViolationCode.STALE_PRICE not in {
        violation.code for violation in fresh.decision.violations
    }
    assert stale.decision.status is RiskDecisionStatus.REJECTED
    assert RiskViolationCode.STALE_PRICE in {
        violation.code for violation in stale.decision.violations
    }


def test_zero_trade_diagnostics_use_gate_failures_not_passed_quality_notes() -> None:
    decisions = (
        _decision(
            score=Decimal("80"),
            confidence=Decimal("0.44"),
            blocker_reasons=(
                "confidence is below the asset profile threshold",
                "feature quality limits conviction",
            ),
            strategy_directions=(StrategyDirection.BUY,),
        ).model_copy(
            update={
                "proposal_gate_trace": (
                    {
                        "gate": "opportunity_score",
                        "actual": "80",
                        "threshold": "70",
                        "passed": True,
                    },
                    {
                        "gate": "confidence",
                        "actual": "0.44",
                        "threshold": "0.65",
                        "passed": False,
                    },
                    {
                        "gate": "data_quality",
                        "actual": "PARTIAL",
                        "threshold": "GOOD preferred; PARTIAL allowed",
                        "passed": True,
                    },
                    {
                        "gate": "final_opportunity_action",
                        "actual": "HOLD",
                        "threshold": "BUY",
                        "passed": False,
                    },
                )
            }
        ),
    )

    diagnostics = diagnose_zero_or_low_trades(decisions)

    assert diagnostics[0].blocker == "Confidence:below threshold"
    assert diagnostics[0].candidate_incidence_count == 1
    assert diagnostics[0].candidate_incidence_rate == Decimal("1.0000")
    assert diagnostics[0].root_gate_failure is True
    assert all(item.blocker != "DataQuality:insufficient conviction" for item in diagnostics)


def test_confidence_forensics_are_sensitive_and_deterministic(tmp_path: Path) -> None:
    payload = build_real_strategy_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, FakeEtoroHistoryClient()),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        store=StrategyValidationStore(SqliteRecordStore(tmp_path / "strategy-validation.sqlite3")),
        clock=_now,
        timeframe="1D",
        max_instruments=3,
    )
    repeat = build_real_strategy_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, FakeEtoroHistoryClient()),
        cache=HistoricalDataCache(tmp_path / "market-data-cache-2.sqlite3"),
        store=StrategyValidationStore(
            SqliteRecordStore(tmp_path / "strategy-validation-2.sqlite3")
        ),
        clock=_now,
        timeframe="1D",
        max_instruments=3,
    )

    run = cast(tuple[dict[str, Any], ...], payload["validation_runs"])[0]
    repeat_run = cast(tuple[dict[str, Any], ...], repeat["validation_runs"])[0]
    forensics = cast(dict[str, Any], run["decision_forensics"])
    repeat_forensics = cast(dict[str, Any], repeat_run["decision_forensics"])
    confidence_distribution = cast(dict[str, str], forensics["confidence_distribution"])

    assert confidence_distribution == repeat_forensics["confidence_distribution"]
    assert Decimal(confidence_distribution["maximum"]) >= Decimal(
        confidence_distribution["minimum"]
    )
    compression = cast(dict[str, Any], forensics["confidence_compression_diagnostics"])
    assert compression["formula"]
    assert compression["ensemble_confidence_distribution"]
    assert compression["regime_multiplier_distribution"]
    traces = cast(tuple[dict[str, Any], ...], forensics["representative_traces"])
    decompositions = [cast(dict[str, Any], trace["confidence_decomposition"]) for trace in traces]
    assert {item["feature_quality_state"] for item in decompositions}
    assert all(item["opportunity_confidence_formula"] for item in decompositions)


def test_zero_trade_metrics_keep_trade_and_research_provenance_distinct(
    tmp_path: Path,
) -> None:
    payload = build_real_strategy_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, FakeEtoroHistoryClient()),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        store=StrategyValidationStore(SqliteRecordStore(tmp_path / "strategy-validation.sqlite3")),
        clock=_now,
        timeframe="1D",
        max_instruments=3,
    )

    run = cast(tuple[dict[str, Any], ...], payload["validation_runs"])[0]
    assert run["trade_count"] == 0
    assert run["profit_factor"] == NOT_ENOUGH_DATA
    assert run["expectancy"] == NOT_ENOUGH_DATA
    assert cast(dict[str, Any], run["monte_carlo"])["sample_provenance"] == (
        "forward_outcome_research"
    )
    matrix = cast(tuple[dict[str, Any], ...], payload["research_matrix"])
    assert matrix
    assert {row["metric_provenance"] for row in matrix} == {"forward_outcome_research"}
    assert payload["qualified_configurations"] == ()


def test_runtime_forensics_reports_active_repository_modules_and_markers() -> None:
    payload = build_runtime_forensics_report()

    assert payload["status"] == "RUNTIME_FORENSICS"
    assert payload["validation_runtime_version"] == "real-validation-runtime-v3"
    assert payload["policy_admission_source"] == "canonical-asset-policy-engine"
    assert payload["broker_write_calls"] == 0
    modules = cast(tuple[dict[str, Any], ...], payload["modules"])
    fingerprints = cast(tuple[dict[str, Any], ...], payload["function_fingerprints"])
    enum_identity = cast(dict[str, Any], payload["enum_identity"])
    assert all(item["inside_current_repository"] for item in modules)
    assert all(item["source_sha256_short"] for item in fingerprints)
    assert enum_identity["equity_key_equal"] is True
    assert enum_identity["etf_key_equal"] is True
    assert enum_identity["crypto_key_equal"] is True


def test_real_data_runtime_admission_path_admits_aapl_spy_and_btc_with_traces(
    tmp_path: Path,
) -> None:
    client = FakeEtoroHistoryClient()

    payload = build_real_strategy_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        store=StrategyValidationStore(SqliteRecordStore(tmp_path / "strategy-validation.sqlite3")),
        clock=_now,
        timeframe="1D",
        max_instruments=9,
    )

    assert payload["status"] == "REAL_DATA_RESEARCH_COMPLETE"
    assert payload["validation_runtime_version"] == "real-validation-runtime-v3"
    assert payload["policy_admission_source"] == "canonical-asset-policy-engine"
    admission_traces = cast(tuple[dict[str, Any], ...], payload["admission_traces"])
    traces = {item["symbol"]: item for item in admission_traces}
    assert traces["AAPL"]["normalized_asset_class"] == "EQUITY"
    assert traces["AAPL"]["policy_enabled"] is True
    assert traces["AAPL"]["strategy_enabled"] is True
    assert traces["AAPL"]["admission_result"] is True
    assert traces["SPY"]["normalized_asset_class"] == "ETF"
    assert traces["SPY"]["admission_result"] is True
    assert traces["BTC"]["normalized_asset_class"] == "CRYPTO"
    assert traces["BTC"]["admission_result"] is True
    rejected_symbols = cast(tuple[str, ...], payload["rejected_symbols"])
    assert not any("POLICY_DISABLED_ASSET_CLASS" in item for item in rejected_symbols)
    assert any(str(call).startswith("candles:") for call in client.calls)


def test_runtime_enriches_classification_when_search_metadata_is_missing(
    tmp_path: Path,
) -> None:
    client = FakeEtoroHistoryClient(search_has_classification=False)

    payload = build_real_strategy_validation_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        store=StrategyValidationStore(SqliteRecordStore(tmp_path / "strategy-validation.sqlite3")),
        clock=_now,
        timeframe="1D",
        max_instruments=9,
    )

    assert payload["status"] == "REAL_DATA_RESEARCH_COMPLETE"
    traces = {
        item["symbol"]: item
        for item in cast(tuple[dict[str, Any], ...], payload["admission_traces"])
    }
    assert traces["AAPL"]["normalized_asset_class"] == "EQUITY"
    assert traces["AAPL"]["classification_evidence_source"] == "instruments.instrumentType"
    assert traces["SPY"]["normalized_asset_class"] == "ETF"
    assert traces["BTC"]["normalized_asset_class"] == "CRYPTO"
    assert any(str(call).startswith("metadata:") for call in client.calls)


def test_exit_evidence_acquisition_requires_canonical_etoro_read_only() -> None:
    payload = build_exit_evidence_acquisition_report(ApplicationConfig())

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "ETORO_READ_ONLY_NOT_CONFIGURED"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert payload["requested_timeframes"] == ("1D", "4H")
    requested = cast(dict[str, tuple[str, ...]], payload["requested_symbols"])
    assert "AMZN" in requested["EQUITY"]
    assert "GLD" in requested["ETF"]
    assert "DOT" in requested["CRYPTO"]
    assert "acquire-exit-evidence" in str(payload["windows_cmd"])


def test_exit_evidence_acquisition_is_read_only_and_caches_expanded_dataset(
    tmp_path: Path,
) -> None:
    class ExpandedFakeEtoroHistoryClient(FakeEtoroHistoryClient):
        _assets: dict[str, tuple[int, str]] = {
            "AAPL": (1001, "Stock"),
            "MSFT": (1002, "Stock"),
            "NVDA": (1003, "Stock"),
            "AMZN": (1004, "Stock"),
            "GOOGL": (1005, "Stock"),
            "META": (1006, "Stock"),
            "TSLA": (1007, "Stock"),
            "AMD": (1008, "Stock"),
            "JPM": (1009, "Stock"),
            "UNH": (1010, "Stock"),
            "XOM": (1011, "Stock"),
            "COST": (1012, "Stock"),
            "SPY": (2001, "ETF"),
            "QQQ": (2002, "ETF"),
            "VTI": (2003, "ETF"),
            "IWM": (2004, "ETF"),
            "DIA": (2005, "ETF"),
            "XLK": (2006, "ETF"),
            "XLF": (2007, "ETF"),
            "XLE": (2008, "ETF"),
            "XLV": (2009, "ETF"),
            "XLU": (2010, "ETF"),
            "TLT": (2011, "ETF"),
            "GLD": (2012, "ETF"),
            "BTC": (3001, "Crypto"),
            "ETH": (3002, "Crypto"),
            "SOL": (3003, "Crypto"),
            "XRP": (3004, "Crypto"),
            "ADA": (3005, "Crypto"),
            "AVAX": (3006, "Crypto"),
            "LINK": (3007, "Crypto"),
            "LTC": (3008, "Crypto"),
            "BCH": (3009, "Crypto"),
            "DOT": (3010, "Crypto"),
        }

        def resolve_instrument(
            self, symbol: str, *, as_of: datetime | None = None
        ) -> InstrumentResolution:
            self.calls.append(f"resolve:{symbol}")
            instrument_id, asset_type = self._assets[symbol]
            return InstrumentResolution(
                instrument_id=instrument_id,
                symbol=symbol,
                internal_symbol_full=symbol,
                display_name=f"{symbol} resolved",
                instrument_type=asset_type,
                classification_metadata={"instrumentType": asset_type},
                classification_evidence_source="search",
                classification_status="RAW_METADATA_RETAINED",
                market_status=MarketStatus.CONTINUOUS_24_7
                if asset_type == "Crypto"
                else MarketStatus.OPEN,
                is_currently_tradable=True,
                is_buy_enabled=True,
                is_hidden_from_client=False,
                is_delisted=False,
                is_active_in_platform=True,
                current_rate=Decimal("100"),
                resolved=True,
                structurally_supported=True,
                structural_status="SUPPORTED",
                verified=True,
                as_of=as_of or _now(),
            )

        def candle_history(
            self,
            *,
            instrument_id: int,
            direction: str,
            interval: str,
            candles_count: int,
        ) -> object:
            self.calls.append(f"candles:{instrument_id}:{interval}:{direction}:{candles_count}")
            symbol = next(
                symbol
                for symbol, (candidate_id, _asset_type) in self._assets.items()
                if candidate_id == instrument_id
            )
            asset_type = self._assets[symbol][1]
            asset_class = {
                "Stock": AssetClass.EQUITY,
                "ETF": AssetClass.ETF,
                "Crypto": AssetClass.CRYPTO,
            }[asset_type]
            timeframe = {
                "OneHour": TimeFrame.ONE_HOUR,
                "FourHours": TimeFrame.FOUR_HOUR,
                "OneDay": TimeFrame.ONE_DAY,
                "OneWeek": TimeFrame.ONE_WEEK,
            }[interval]
            instrument = _instrument(
                symbol,
                instrument_id=instrument_id,
                asset_class=asset_class,
                broker="etoro",
            )
            bars = _bars(instrument, timeframe=timeframe, count=120, source="etoro")
            return {
                "candles": [
                    {
                        "candles": [
                            {
                                "fromDate": bar.timestamp.isoformat().replace("+00:00", "Z"),
                                "open": str(bar.open),
                                "high": str(bar.high),
                                "low": str(bar.low),
                                "close": str(bar.close),
                                "volume": str(bar.volume) if bar.volume is not None else None,
                            }
                            for bar in bars
                        ]
                    }
                ]
            }

    client = ExpandedFakeEtoroHistoryClient()
    payload = build_exit_evidence_acquisition_report(
        ApplicationConfig(etoro_api_enabled=True),
        client=cast(EtoroReadClient, client),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        clock=_now,
    )

    assert payload["status"] == "EXIT_EVIDENCE_DATASET_READY_FOR_VALIDATION"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert payload["successfully_mapped_instruments"] == 34
    assert payload["rejected_instrument_count"] == 0
    assert payload["total_1d_bars"] == 34 * 120
    assert payload["total_4h_bars"] == 34 * 120
    coverage = cast(tuple[dict[str, Any], ...], payload["coverage"])
    assert len(coverage) == 68
    assert {item["timestamp_ordering"] for item in coverage} == {"ORDERED"}
    assert all(item["mapping_status"] == "RESOLVED" for item in coverage)
    assert all(item["cache_status"] == "UPDATED" for item in coverage)
    assert all(str(call).startswith(("resolve:", "candles:")) for call in client.calls)


def test_exit_evidence_rejects_cross_asset_symbol_substitution() -> None:
    class CryptoDiaClient(FakeEtoroHistoryClient):
        def resolve_instrument(
            self, symbol: str, *, as_of: datetime | None = None
        ) -> InstrumentResolution:
            self.calls.append(f"resolve:{symbol}")
            return InstrumentResolution(
                instrument_id=100580,
                symbol=symbol,
                internal_symbol_full=symbol,
                display_name="DIA resolved as digital currency",
                instrument_type="Crypto",
                classification_metadata={
                    "instrumentType": "Crypto",
                    "internalExchangeName": "Digital Currency",
                },
                classification_evidence_source="search",
                classification_status="RAW_METADATA_RETAINED",
                market_status=MarketStatus.CONTINUOUS_24_7,
                is_currently_tradable=True,
                is_buy_enabled=True,
                is_hidden_from_client=False,
                is_delisted=False,
                is_active_in_platform=True,
                current_rate=Decimal("1"),
                resolved=True,
                structurally_supported=True,
                structural_status="SUPPORTED",
                verified=True,
                as_of=as_of or _now(),
            )

    instruments, rejected, _provider = app.data.runtime._resolve_etoro_research_universe(
        client=cast(EtoroReadClient, CryptoDiaClient()),
        symbols=("DIA",),
        as_of=_now(),
        asset_class_filter=None,
        policy_engine=default_asset_policy_engine(),
        expected_asset_classes={"DIA": AssetClass.ETF},
    )

    assert instruments == ()
    assert rejected == ("DIA:REQUESTED_ASSET_CLASS_MISMATCH:ETF!=CRYPTO",)


def test_historical_cache_quarantines_contaminated_cross_asset_rows(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    instrument = _instrument(
        "DIA",
        instrument_id=100580,
        asset_class=AssetClass.CRYPTO,
        broker="etoro",
    )
    mapping = ProviderInstrumentReference(
        provider="etoro",
        provider_symbol="100580",
        broker="etoro",
        broker_symbol="DIA",
        broker_instrument_id="100580",
        exchange="Digital Currency",
        asset_class=AssetClass.CRYPTO,
        currency=Currency.USD,
        mapping_confidence=Decimal("1"),
        mapping_source="official broker instrument id",
        verified=True,
    )
    cache.upsert_bars(
        provider="etoro",
        bars=_bars(instrument, timeframe=TimeFrame.ONE_DAY, count=3, source="etoro"),
        fetched_at=_now(),
        mapping=mapping,
    )

    result = cache.quarantine_asset_class_mismatches(
        expected_asset_classes={"DIA": AssetClass.ETF},
        quarantined_at=_now(),
    )
    remaining = cache.get_bars(
        provider="etoro",
        instrument_key=("etoro", "100580"),
        timeframe=TimeFrame.ONE_DAY,
        as_of=_now(),
        limit=10,
        instrument_factory=instrument.model_dump(mode="json"),
    )

    assert result["quarantined_rows"] == 3
    assert remaining == ()


def test_equity_daily_weekend_gap_is_not_missing_bars_but_crypto_is() -> None:
    equity = _instrument("AAPL", asset_class=AssetClass.EQUITY)
    crypto = _instrument("BTC", instrument_id=100000, asset_class=AssetClass.CRYPTO)
    friday = datetime(2026, 8, 28, tzinfo=UTC)
    monday = datetime(2026, 8, 31, tzinfo=UTC)
    equity_bars = (
        MarketBar(
            instrument=equity,
            timestamp=friday,
            timeframe=TimeFrame.ONE_DAY,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            currency=Currency.USD,
            source="fixture",
        ),
        MarketBar(
            instrument=equity,
            timestamp=monday,
            timeframe=TimeFrame.ONE_DAY,
            open=Decimal("101"),
            high=Decimal("101"),
            low=Decimal("101"),
            close=Decimal("101"),
            currency=Currency.USD,
            source="fixture",
        ),
    )
    crypto_bars = (
        MarketBar(
            instrument=crypto,
            timestamp=friday,
            timeframe=TimeFrame.ONE_DAY,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            currency=Currency.USD,
            source="fixture",
        ),
        MarketBar(
            instrument=crypto,
            timestamp=monday,
            timeframe=TimeFrame.ONE_DAY,
            open=Decimal("101"),
            high=Decimal("101"),
            low=Decimal("101"),
            close=Decimal("101"),
            currency=Currency.USD,
            source="fixture",
        ),
    )
    analyzer = HistoricalDataQualityAnalyzer(minimum_bars=2)

    equity_report = analyzer.evaluate(
        provider="fixture",
        instrument=equity,
        timeframe=TimeFrame.ONE_DAY,
        bars=equity_bars,
        as_of=monday,
        expected_currency=Currency.USD,
    )
    crypto_report = analyzer.evaluate(
        provider="fixture",
        instrument=crypto,
        timeframe=TimeFrame.ONE_DAY,
        bars=crypto_bars,
        as_of=monday,
        expected_currency=Currency.USD,
    )

    assert equity_report.missing_bars_estimate == 0
    assert crypto_report.missing_bars_estimate > 0


class PolygonPilotTransport:
    def __init__(self, *, conflict_duplicate: bool = False) -> None:
        self.urls: list[str] = []
        self.saw_next_page = False
        self._conflict_duplicate = conflict_duplicate

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        assert headers["User-Agent"] == "AegisInvestAI/0.7"
        self.urls.append(url)
        if "cursor=pilot" in url:
            return json.dumps({"status": "OK", "results": self._bars(url, page=2)})
        payload: dict[str, object] = {"status": "OK", "results": self._bars(url, page=1)}
        if "AAPL" in url and "/range/1/day/" in url:
            self.saw_next_page = True
            payload["next_url"] = (
                "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2026-08-01/2026-08-29?cursor=pilot"
            )
        return json.dumps(payload)

    def _bars(self, url: str, *, page: int) -> list[dict[str, object]]:
        is_four_hour = "/range/4/hour/" in url
        if "X:BTCUSD" in url and "2006-" in url:
            return []
        if "2004-" in url:
            start = datetime(2004, 1, 5, tzinfo=UTC)
            step = timedelta(hours=4) if is_four_hour else timedelta(days=1)
            count = 31
        elif "2014-" in url:
            start = datetime(2014, 1, 5, tzinfo=UTC)
            step = timedelta(hours=4) if is_four_hour else timedelta(days=1)
            count = 31
        elif "2016-" in url:
            start = datetime(2016, 1, 5, tzinfo=UTC)
            step = timedelta(hours=4) if is_four_hour else timedelta(days=1)
            count = 31
        elif "2021-" in url:
            start = datetime(2021, 8, 30, tzinfo=UTC)
            step = timedelta(hours=4) if is_four_hour else timedelta(days=1)
            count = 31
        elif "2024-" in url:
            start = datetime(2024, 8, 29, tzinfo=UTC)
            step = timedelta(hours=4) if is_four_hour else timedelta(days=1)
            count = 31
        elif "2006-" in url:
            start = datetime(2006, 9, 3, tzinfo=UTC)
            step = timedelta(hours=4) if is_four_hour else timedelta(days=1)
            count = 31
        elif is_four_hour:
            start = datetime(2026, 8, 24, tzinfo=UTC)
            step = timedelta(hours=4)
            count = 36
        else:
            start = datetime(2026, 7, 21, tzinfo=UTC)
            step = timedelta(days=1)
            count = 40
        if page == 2:
            start += step * count
            count = 2
        bars = []
        for index in range(count):
            timestamp = start + step * index
            value = Decimal("100") + Decimal(index)
            bars.append(
                {
                    "t": int(timestamp.timestamp() * 1000),
                    "o": str(value),
                    "h": str(value + Decimal("1")),
                    "l": str(value - Decimal("1")),
                    "c": str(value + Decimal("0.5")),
                    "v": str(1000 + index),
                }
            )
        if self._conflict_duplicate:
            duplicate = dict(bars[0])
            duplicate["c"] = "100.75"
            bars.append(duplicate)
        elif bars:
            bars.append(dict(bars[0]))
        return bars


class FailingPolygonTransport:
    def __init__(
        self,
        *,
        http_status: int,
        transport_category: str,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        self.http_status = http_status
        self.transport_category = transport_category
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        self.retry_after = retry_after

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        assert headers["User-Agent"] == "AegisInvestAI/0.7"
        raise DataProviderError(
            "synthetic Polygon/Massive HTTP failure",
            status=(
                DataProviderStatus.RATE_LIMITED
                if self.http_status == 429
                else DataProviderStatus.PROVIDER_UNAVAILABLE
            ),
            http_status=self.http_status,
            sanitized_endpoint=_sanitize_url(url),
            transport_category=self.transport_category,
            provider_error_code=self.provider_error_code,
            provider_error_message=self.provider_error_message,
            retry_after=self.retry_after,
        )


class PlanAwarePolygonTransport(PolygonPilotTransport):
    def get_text(self, url: str, headers: dict[str, str]) -> str:
        if any(year in url for year in ("2021-", "2016-", "2006-")):
            raise DataProviderError(
                "synthetic plan limitation",
                status=DataProviderStatus.PROVIDER_UNAVAILABLE,
                http_status=403,
                sanitized_endpoint=_sanitize_url(url),
                transport_category="HTTP_403_FORBIDDEN",
                provider_error_code="NOT_AUTHORIZED",
                provider_error_message=(
                    "current plan does not include requested historical timeframe"
                ),
            )
        return super().get_text(url, headers)


class AlpacaPilotTransport:
    def __init__(self, *, sol_has_data: bool = True) -> None:
        self.urls: list[str] = []
        self.stock_headers: list[dict[str, str]] = []
        self.crypto_headers: list[dict[str, str]] = []
        self.saw_next_page = False
        self.sol_has_data = sol_has_data

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        parsed = urlparse(url)
        assert parsed.netloc == "data.alpaca.markets"
        assert parsed.path in {"/v2/stocks/bars", "/v1beta3/crypto/us/bars"}
        assert "/orders" not in parsed.path
        assert "/account" not in parsed.path
        assert headers["User-Agent"] == "AegisInvestAI/0.7"
        self.urls.append(url)
        query = parse_qs(parsed.query)
        symbol = query["symbols"][0]
        timeframe = query["timeframe"][0]
        if parsed.path == "/v2/stocks/bars":
            assert query["feed"][0] == "sip"
            assert headers["APCA-API-KEY-ID"] == "alpaca-key-id"
            assert headers["APCA-API-SECRET-KEY"] == "alpaca-secret-key"
            self.stock_headers.append(headers)
        else:
            self.crypto_headers.append(headers)
        if symbol == "SOL/USD" and not self.sol_has_data:
            return json.dumps({"bars": {symbol: []}, "next_page_token": None})
        if query.get("page_token") == ["pilot"]:
            return json.dumps({"bars": {symbol: self._bars(symbol, timeframe, page=2)}})
        payload: dict[str, object] = {"bars": {symbol: self._bars(symbol, timeframe, page=1)}}
        if query.get("limit") == ["2"] and symbol == "AAPL":
            self.saw_next_page = True
            payload["next_page_token"] = "pilot"
        else:
            payload["next_page_token"] = None
        return json.dumps(payload)

    def _bars(self, symbol: str, timeframe: str, *, page: int) -> list[dict[str, object]]:
        is_four_hour = timeframe == "4Hour"
        query = parse_qs(urlparse(self.urls[-1]).query)
        start_raw = query["start"][0].replace("Z", "+00:00")
        start = datetime.fromisoformat(start_raw).astimezone(UTC)
        if symbol == "SOL/USD" and start < datetime(2020, 4, 1, tzinfo=UTC):
            return []
        step = timedelta(hours=4) if is_four_hour else timedelta(days=1)
        if page == 2:
            start += step * 2
        bars: list[dict[str, object]] = []
        for index in range(2):
            value = Decimal("100") + Decimal(index)
            timestamp = start + step * index
            bars.append(
                {
                    "t": timestamp.isoformat().replace("+00:00", "Z"),
                    "o": str(value),
                    "h": str(value + Decimal("1")),
                    "l": str(value - Decimal("1")),
                    "c": str(value + Decimal("0.5")),
                    "v": str(1000 + index),
                }
            )
        return bars


class AlpacaFullBackfillTransport:
    def __init__(self, *, unsupported_symbol: str | None = None) -> None:
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.unsupported_symbol = unsupported_symbol

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        parsed = urlparse(url)
        assert parsed.netloc == "data.alpaca.markets"
        assert parsed.path in {"/v2/stocks/bars", "/v1beta3/crypto/us/bars"}
        assert "/orders" not in parsed.path
        assert "/account" not in parsed.path
        assert headers["User-Agent"] == "AegisInvestAI/0.7"
        query = parse_qs(parsed.query)
        symbol = query["symbols"][0]
        if self.unsupported_symbol and symbol.startswith(self.unsupported_symbol):
            return json.dumps({"bars": {symbol: []}, "next_page_token": None})
        self.urls.append(url)
        self.headers.append(headers)
        page = 2 if query.get("page_token") == ["full"] else 1
        payload: dict[str, object] = {"bars": {symbol: self._bars(symbol, query, page=page)}}
        payload["next_page_token"] = "full" if page == 1 else None
        return json.dumps(payload)

    def _bars(
        self,
        symbol: str,
        query: Mapping[str, list[str]],
        *,
        page: int,
    ) -> list[dict[str, object]]:
        timeframe = query["timeframe"][0]
        start = datetime.fromisoformat(query["start"][0].replace("Z", "+00:00")).astimezone(UTC)
        step = timedelta(hours=4) if timeframe == "4Hour" else timedelta(days=1)
        if page == 2:
            start += step * 2
        base = Decimal(len(symbol) * 10)
        return [
            {
                "t": (start + step * index).isoformat().replace("+00:00", "Z"),
                "o": str(base + Decimal(index)),
                "h": str(base + Decimal(index) + Decimal("1")),
                "l": str(base + Decimal(index) - Decimal("1")),
                "c": str(base + Decimal(index) + Decimal("0.5")),
                "v": str(10_000 + index),
            }
            for index in range(2)
        ]


class AlpacaCore4HTransport:
    def __init__(self, *, always_truncated: bool = False) -> None:
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.always_truncated = always_truncated

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        parsed = urlparse(url)
        assert parsed.netloc == "data.alpaca.markets"
        assert parsed.path in {"/v2/stocks/bars", "/v1beta3/crypto/us/bars"}
        assert "/orders" not in parsed.path
        assert "/account" not in parsed.path
        query = parse_qs(parsed.query)
        assert query["timeframe"] == ["4Hour"]
        self.urls.append(url)
        self.headers.append(headers)
        symbol = query["symbols"][0]
        return json.dumps(
            {
                "bars": {symbol: self._bars(symbol, query)},
                "next_page_token": "more" if self.always_truncated else None,
            }
        )

    def _bars(self, symbol: str, query: Mapping[str, list[str]]) -> list[dict[str, object]]:
        start = datetime.fromisoformat(query["start"][0].replace("Z", "+00:00")).astimezone(UTC)
        end = datetime.fromisoformat(query["end"][0].replace("Z", "+00:00")).astimezone(UTC)
        timestamp = start
        bars: list[dict[str, object]] = []
        base = Decimal(len(symbol) * 10)
        index = 0
        while timestamp <= end:
            value = base + Decimal(index) / Decimal("100")
            bars.append(
                {
                    "t": timestamp.isoformat().replace("+00:00", "Z"),
                    "o": str(value),
                    "h": str(value + Decimal("1")),
                    "l": str(value - Decimal("1")),
                    "c": str(value + Decimal("0.5")),
                    "v": str(10_000 + index),
                }
            )
            timestamp += timedelta(hours=4)
            index += 1
        return bars


class StaticTextTransport:
    def __init__(self, text: str) -> None:
        self.text = text
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.urls.append(url)
        self.headers.append(headers)
        return self.text


class RaisingTextTransport:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def get_text(self, _url: str, _headers: dict[str, str]) -> str:
        raise self.exc


def _seed_core_4h_etoro_mappings(cache: HistoricalDataCache) -> None:
    ids = {
        "AAPL": (1001, AssetClass.EQUITY),
        "MSFT": (1004, AssetClass.EQUITY),
        "SPY": (3000, AssetClass.ETF),
        "QQQ": (3006, AssetClass.ETF),
        "GLD": (3025, AssetClass.ETF),
        "BTC": (100000, AssetClass.CRYPTO),
        "ETH": (100001, AssetClass.CRYPTO),
        "SOL": (100063, AssetClass.CRYPTO),
    }
    for symbol, (instrument_id, asset_class) in ids.items():
        instrument = _instrument(
            symbol,
            instrument_id=instrument_id,
            asset_class=asset_class,
            broker="etoro",
        )
        cache.upsert_bars(
            provider="etoro",
            bars=_bars(instrument, timeframe=TimeFrame.ONE_DAY, count=1, source="etoro"),
            fetched_at=_now(),
            mapping=ProviderInstrumentReference(
                provider="etoro",
                provider_symbol=symbol,
                broker="etoro",
                broker_symbol=symbol,
                broker_instrument_id=str(instrument_id),
                exchange=instrument.exchange,
                asset_class=asset_class,
                currency=Currency.USD,
                mapping_confidence=Decimal("1"),
                mapping_source="verified test eToro reference",
                verified=True,
            ),
        )


def _seed_core_1d_alpaca_cache(cache: HistoricalDataCache) -> None:
    ids = {
        "AAPL": (1001, AssetClass.EQUITY),
        "MSFT": (1004, AssetClass.EQUITY),
        "SPY": (3000, AssetClass.ETF),
        "QQQ": (3006, AssetClass.ETF),
        "GLD": (3025, AssetClass.ETF),
        "BTC": (100000, AssetClass.CRYPTO),
        "ETH": (100001, AssetClass.CRYPTO),
        "SOL": (100063, AssetClass.CRYPTO),
    }
    for symbol, (instrument_id, asset_class) in ids.items():
        instrument = _instrument(
            symbol,
            instrument_id=instrument_id,
            asset_class=asset_class,
            broker="etoro",
        )
        mapping = ProviderInstrumentReference(
            provider="alpaca",
            provider_symbol=f"{symbol}/USD" if asset_class is AssetClass.CRYPTO else symbol,
            broker="etoro",
            broker_symbol=symbol,
            broker_instrument_id=str(instrument_id),
            exchange=instrument.exchange,
            asset_class=asset_class,
            currency=Currency.USD,
            mapping_confidence=Decimal("1"),
            mapping_source="verified test Alpaca core 1D mapping",
            verified=True,
        )
        cache.upsert_bars(
            provider="alpaca",
            bars=_bars(
                instrument,
                timeframe=TimeFrame.ONE_DAY,
                count=180,
                end_at=_now() - timedelta(days=1),
                source="alpaca",
            ),
            fetched_at=_now(),
            mapping=mapping,
        )


def test_alpaca_provider_adapter_fetches_stock_range_with_credentials() -> None:
    instrument = _instrument("AAPL", instrument_id=1001, asset_class=AssetClass.EQUITY)
    transport = AlpacaPilotTransport()
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id=" alpaca-key-id ",
        api_secret_key=" alpaca-secret-key ",
        transport=transport,
        max_pages=2,
    )

    bars = provider.get_bars_range(
        instrument,
        TimeFrame.ONE_DAY,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 10, tzinfo=UTC),
        limit=2,
    )

    assert len(bars) == 4
    assert {bar.source for bar in bars} == {"alpaca"}
    assert "feed=sip" in transport.urls[0]
    assert transport.stock_headers[0]["APCA-API-KEY-ID"] == "alpaca-key-id"


def test_alpaca_provider_adapter_exposes_real_pagination_state() -> None:
    instrument = _instrument("AAPL", instrument_id=1001, asset_class=AssetClass.EQUITY)
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id="alpaca-key-id",
        api_secret_key="alpaca-secret-key",
        transport=AlpacaPilotTransport(),
    )

    bars = provider.get_bars_range(
        instrument,
        TimeFrame.ONE_DAY,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 10, tzinfo=UTC),
        limit=2,
        max_pages=2,
    )

    assert len(bars) == 4
    assert provider.last_pagination_state == {
        "pagination_requested": True,
        "pagination_token_observed": True,
        "second_page_fetched": True,
        "pagination_verified": True,
        "pagination_truncated": False,
        "pages_fetched": 2,
    }


def test_alpaca_provider_adapter_fetches_crypto_without_stock_credentials() -> None:
    instrument = _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO)
    transport = AlpacaPilotTransport()
    provider = AlpacaHistoricalMarketDataProvider(transport=transport)

    bars = provider.get_bars(
        instrument,
        TimeFrame.FOUR_HOUR,
        as_of=datetime(2026, 1, 10, tzinfo=UTC),
        limit=2,
    )

    assert len(bars) == 2
    assert "/v1beta3/crypto/us/bars" in transport.urls[0]
    assert "symbols=BTC%2FUSD" in transport.urls[0]
    assert "APCA-API-KEY-ID" not in transport.crypto_headers[0]


def test_alpaca_provider_adapter_rejects_invalid_range_and_missing_stock_credentials() -> None:
    instrument = _instrument("SPY", instrument_id=3000, asset_class=AssetClass.ETF)
    provider = AlpacaHistoricalMarketDataProvider(transport=AlpacaPilotTransport())

    with pytest.raises(DataProviderError, match="require API key"):
        provider.get_bars_range(
            instrument,
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )

    with pytest.raises(DataProviderError, match="start must be before end"):
        provider.get_bars_range(
            _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 2, tzinfo=UTC),
            end=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_alpaca_provider_adapter_returns_empty_for_unsupported_timeframe() -> None:
    provider = AlpacaHistoricalMarketDataProvider(transport=AlpacaPilotTransport())

    bars = provider.get_bars_range(
        _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
        TimeFrame.ONE_WEEK,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert bars == ()


def test_alpaca_provider_adapter_rejects_unsupported_asset_class() -> None:
    provider = AlpacaHistoricalMarketDataProvider(transport=AlpacaPilotTransport())

    with pytest.raises(DataProviderError, match="supports only equity"):
        provider.get_bars_range(
            _instrument("EURUSD", instrument_id=4000, asset_class=AssetClass.FOREX),
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_alpaca_provider_adapter_rejects_malformed_provider_payload() -> None:
    provider = AlpacaHistoricalMarketDataProvider(
        transport=StaticTextTransport(json.dumps({"bars": {"BTC/USD": {"bad": "shape"}}}))
    )

    with pytest.raises(DataProviderError, match="malformed"):
        provider.get_bars_range(
            _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_alpaca_provider_adapter_rejects_non_object_payload() -> None:
    provider = AlpacaHistoricalMarketDataProvider(transport=StaticTextTransport("[]"))

    with pytest.raises(DataProviderError, match="not an object"):
        provider.get_bars_range(
            _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_alpaca_provider_adapter_classifies_transport_failures() -> None:
    timeout_provider = AlpacaHistoricalMarketDataProvider(
        transport=RaisingTextTransport(TimeoutError("slow"))
    )
    os_provider = AlpacaHistoricalMarketDataProvider(
        transport=RaisingTextTransport(OSError("socket closed"))
    )

    with pytest.raises(DataProviderError) as timeout_error:
        timeout_provider.get_bars_range(
            _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )
    with pytest.raises(DataProviderError) as os_error:
        os_provider.get_bars_range(
            _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )

    assert timeout_error.value.status is DataProviderStatus.TIMEOUT
    assert os_error.value.status is DataProviderStatus.PROVIDER_UNAVAILABLE


def test_alpaca_provider_adapter_preserves_permission_error_diagnostics() -> None:
    provider = AlpacaHistoricalMarketDataProvider(
        transport=RaisingTextTransport(PermissionError(13, "Access denied by OS policy"))
    )

    with pytest.raises(DataProviderError) as error:
        provider.get_bars_range(
            _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
            TimeFrame.ONE_DAY,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )

    diagnostics = error.value.safe_diagnostics()
    assert diagnostics["provider_status"] == "PROVIDER_UNAVAILABLE"
    assert diagnostics["transport_category"] == "PermissionError"
    assert diagnostics["exception_type"] == "PermissionError"
    assert diagnostics["exception_message"] == "[Errno 13] Access denied by OS policy"
    assert diagnostics["errno"] == 13
    assert diagnostics["sanitized_endpoint"] is not None
    assert "data.alpaca.markets" in str(diagnostics["sanitized_endpoint"])


def test_alpaca_provider_adapter_accepts_crypto_list_payload_and_skips_bad_rows() -> None:
    payload = {
        "bars": [
            {"bad": "row"},
            {
                "t": "2026-01-01T00:00:00Z",
                "o": "1",
                "h": "2",
                "l": "0.5",
                "c": "1.5",
            },
            {
                "t": "2026-01-02T00:00:00Z",
                "o": "2",
                "h": "3",
                "l": "1.5",
                "c": "2.5",
                "v": None,
            },
        ]
    }
    provider = AlpacaHistoricalMarketDataProvider(
        transport=StaticTextTransport(json.dumps(payload))
    )

    bars = provider.get_bars_range(
        _instrument("ETH", instrument_id=2002, asset_class=AssetClass.CRYPTO),
        TimeFrame.ONE_DAY,
        start=datetime(2025, 12, 31, tzinfo=UTC),
        end=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert len(bars) == 2
    assert bars[0].volume is None


def test_alpaca_provider_adapter_deduplicates_identical_bars() -> None:
    timestamp = "2026-01-01T00:00:00Z"
    bar = {"t": timestamp, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "100"}
    provider = AlpacaHistoricalMarketDataProvider(
        transport=StaticTextTransport(json.dumps({"bars": {"BTC/USD": [bar, bar]}}))
    )

    bars = provider.get_bars_range(
        _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
        TimeFrame.ONE_DAY,
        start=datetime(2025, 12, 31, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert len(bars) == 1


def test_alpaca_provider_adapter_rejects_conflicting_duplicate_bars() -> None:
    timestamp = "2026-01-01T00:00:00Z"
    first = {"t": timestamp, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "100"}
    second = {"t": timestamp, "o": "9", "h": "10", "l": "8.5", "c": "9.5", "v": "100"}
    provider = AlpacaHistoricalMarketDataProvider(
        transport=StaticTextTransport(json.dumps({"bars": {"BTC/USD": [first, second]}}))
    )

    with pytest.raises(DataProviderError, match="conflicting OHLCV"):
        provider.get_bars_range(
            _instrument("BTC", instrument_id=2001, asset_class=AssetClass.CRYPTO),
            TimeFrame.ONE_DAY,
            start=datetime(2025, 12, 31, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_polygon_provider_adapter_supports_range_pagination_and_deduplication() -> None:
    instrument = _instrument("AAPL", instrument_id=1001, broker="etoro")
    provider = PolygonHistoricalMarketDataProvider(
        api_key="secret-polygon-key",
        transport=PolygonPilotTransport(),
        max_pages=2,
    )

    bars = provider.get_bars_range(
        instrument,
        TimeFrame.ONE_DAY,
        start=datetime(2026, 7, 21, tzinfo=UTC),
        end=datetime(2026, 9, 5, tzinfo=UTC),
        limit=100,
        max_pages=2,
    )

    assert len(bars) == 42
    assert tuple(bar.timestamp for bar in bars) == tuple(sorted(bar.timestamp for bar in bars))
    assert len({bar.timestamp for bar in bars}) == len(bars)


def test_polygon_pagination_replaces_existing_secret_query_parameter() -> None:
    provider = PolygonHistoricalMarketDataProvider(
        api_key="fresh-secret-key",
        transport=PolygonPilotTransport(),
    )

    url = provider._with_api_key(  # noqa: SLF001
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2026-01-01/2026-01-02"
        "?apiKey=old-secret&cursor=abc"
    )

    assert "old-secret" not in url
    assert url.count("apiKey=") == 1
    assert "cursor=abc" in url


def test_polygon_provider_rejects_conflicting_duplicate_timestamp() -> None:
    instrument = _instrument("AAPL", instrument_id=1001, broker="etoro")
    provider = PolygonHistoricalMarketDataProvider(
        api_key="secret-polygon-key",
        transport=PolygonPilotTransport(conflict_duplicate=True),
        max_pages=1,
    )

    with pytest.raises(Exception, match="conflicting OHLCV"):
        provider.get_bars_range(
            instrument,
            TimeFrame.ONE_DAY,
            start=datetime(2026, 7, 21, tzinfo=UTC),
            end=datetime(2026, 9, 5, tzinfo=UTC),
            limit=100,
            max_pages=1,
        )


def test_polygon_provider_pilot_fails_closed_when_key_missing(tmp_path: Path) -> None:
    payload = build_polygon_provider_pilot_report(
        ApplicationConfig(),
        values={},
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        clock=_now,
    )

    assert payload["status"] == "HISTORICAL_PROVIDER_INTEGRATION_BLOCKER"
    assert payload["category"] == "POLYGON_MASSIVE_API_KEY_NOT_CONFIGURED"
    assert payload["broker_write_calls"] == 0


@pytest.mark.parametrize(
    ("http_status", "category"),
    (
        (401, "HTTP_401_UNAUTHORIZED"),
        (403, "HTTP_403_FORBIDDEN"),
        (404, "HTTP_404_NOT_FOUND"),
        (429, "HTTP_429_RATE_LIMITED"),
        (500, "HTTP_5XX_PROVIDER_ERROR"),
    ),
)
def test_polygon_provider_pilot_reports_secret_safe_http_diagnostics(
    tmp_path: Path,
    http_status: int,
    category: str,
) -> None:
    secret = "secret-polygon-key"
    payload = build_polygon_provider_pilot_report(
        ApplicationConfig(),
        values={"AEGIS_POLYGON_API_KEY": secret},
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=FailingPolygonTransport(
            http_status=http_status,
            transport_category=category,
            provider_error_code="NOT_AUTHORIZED",
            provider_error_message="plan does not include this endpoint",
            retry_after="60" if http_status == 429 else None,
        ),
        clock=_now,
    )
    serialized = json.dumps(payload, sort_keys=True)
    depth = cast(tuple[dict[str, object], ...], payload["historical_depth_accessible"])
    diagnostic_probe = next(item for item in depth if item["http_status"] == http_status)

    assert payload["status"] == "HISTORICAL_PROVIDER_PLAN_LIMITATION"
    assert diagnostic_probe["http_status"] == http_status
    assert diagnostic_probe["transport_category"] == category
    assert diagnostic_probe["provider_error_code"] == "NOT_AUTHORIZED"
    assert diagnostic_probe["provider_error_message"] == "plan does not include this endpoint"
    assert secret not in serialized
    if http_status == 429:
        assert diagnostic_probe["retry_after"] == "60"


def test_provider_http_diagnostic_helpers_classify_expected_statuses() -> None:
    assert _http_transport_category(401) == "HTTP_401_UNAUTHORIZED"
    assert _http_transport_category(403) == "HTTP_403_FORBIDDEN"
    assert _http_transport_category(404) == "HTTP_404_NOT_FOUND"
    assert _http_transport_category(429) == "HTTP_429_RATE_LIMITED"
    assert _http_transport_category(503) == "HTTP_5XX_PROVIDER_ERROR"
    sanitized = _sanitize_url(
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2026-01-01/2026-01-02"
        "?adjusted=true&apiKey=secret&token=secret2"
    )
    assert "secret" not in sanitized
    assert "apiKey=<redacted>" in sanitized
    assert "token=<redacted>" in sanitized


def test_polygon_provider_pilot_reports_plan_aware_entitlement_matrix(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    payload = build_polygon_provider_pilot_report(
        ApplicationConfig(),
        values={"AEGIS_POLYGON_API_KEY": "secret-polygon-key"},
        cache=cache,
        transport=PlanAwarePolygonTransport(),
        clock=_now,
    )
    serialized = json.dumps(payload, sort_keys=True)
    depth = cast(tuple[dict[str, object], ...], payload["historical_depth_accessible"])

    assert payload["status"] == "HISTORICAL_PROVIDER_PLAN_LIMITATION"
    assert payload["plan_recommendation"] == "MIXED_PLAN_REQUIRED"
    assert payload["deepest_verified_stock_etf_entitlement"] == "2Y_VERIFIED"
    assert payload["deepest_verified_crypto_entitlement"] == "2Y_VERIFIED"
    assert payload["four_hour_exit_validation_depth_adequate"] is False
    assert any(
        item["symbol"] == "AAPL"
        and item["timeframe"] == "1D"
        and item["probe_label"] == "5y"
        and item["authorization_status"] == "NOT_AUTHORIZED"
        and item["http_status"] == 403
        for item in depth
    )
    assert "secret-polygon-key" not in serialized


def test_polygon_provider_pilot_does_not_treat_no_data_as_plan_limitation(
    tmp_path: Path,
) -> None:
    payload = build_polygon_provider_pilot_report(
        ApplicationConfig(),
        values={"AEGIS_POLYGON_API_KEY": "secret-polygon-key"},
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=PolygonPilotTransport(),
        clock=_now,
    )
    depth = cast(tuple[dict[str, object], ...], payload["historical_depth_accessible"])

    assert payload["status"] == "HISTORICAL_PROVIDER_ADAPTER_READY_FOR_BACKFILL"
    assert payload["plan_recommendation"] == "CURRENT_MASSIVE_PLAN_SUFFICIENT"
    assert any(
        item["symbol"] == "BTC"
        and item["probe_label"] == "20y"
        and item["authorization_status"] == "NO_DATA"
        for item in depth
    )
    assert any(
        item["symbol"] == "AAPL"
        and item["probe_label"] == "20y"
        and item["authorization_status"] == "AUTHORIZED"
        for item in depth
    )
    assert any(
        item["symbol"] == "SPY"
        and item["probe_label"] == "20y"
        and item["authorization_status"] == "AUTHORIZED"
        for item in depth
    )


def test_polygon_provider_pilot_is_secret_free_and_keeps_dia_reference_unavailable(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    for symbol, instrument_id, asset_class in (
        ("AAPL", 1001, AssetClass.EQUITY),
        ("SPY", 3000, AssetClass.ETF),
        ("BTC", 100000, AssetClass.CRYPTO),
    ):
        instrument = _instrument(
            symbol,
            instrument_id=instrument_id,
            asset_class=asset_class,
            broker="etoro",
        )
        mapping = ProviderInstrumentReference(
            provider="etoro",
            provider_symbol=str(instrument_id),
            broker="etoro",
            broker_symbol=symbol,
            broker_instrument_id=str(instrument_id),
            exchange=instrument.exchange,
            asset_class=asset_class,
            currency=Currency.USD,
            mapping_confidence=Decimal("1"),
            mapping_source="official broker instrument id",
            verified=True,
        )
        cache.upsert_bars(
            provider="etoro",
            bars=_bars(instrument, timeframe=TimeFrame.ONE_DAY, count=40, source="etoro"),
            fetched_at=_now(),
            mapping=mapping,
        )
        cache.upsert_bars(
            provider="etoro",
            bars=_bars(instrument, timeframe=TimeFrame.FOUR_HOUR, count=40, source="etoro"),
            fetched_at=_now(),
            mapping=mapping,
        )

    secret = "secret-polygon-key"
    payload = build_polygon_provider_pilot_report(
        ApplicationConfig(),
        values={"AEGIS_POLYGON_API_KEY": secret},
        cache=cache,
        transport=PolygonPilotTransport(),
        clock=_now,
    )
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "HISTORICAL_PROVIDER_ADAPTER_READY_FOR_BACKFILL"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    dia_status = cast(dict[str, object], payload["dia_status"])
    assert dia_status["provider_mapping"] == "DIA_PROVIDER_MAPPING_VERIFIED"
    assert dia_status["etoro_reference"] == "ETORO_DIA_REFERENCE_UNAVAILABLE"
    assert payload["cache_duplicate_count"] == 0
    assert secret not in serialized


def test_alpaca_provider_pilot_requires_env_only_stock_credentials(tmp_path: Path) -> None:
    payload = build_alpaca_provider_pilot_report(
        ApplicationConfig(),
        values={},
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        clock=_now,
    )

    assert payload["status"] == "ALPACA_ADAPTER_BLOCKER"
    assert payload["category"] == "ALPACA_API_KEYS_NOT_CONFIGURED"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False


def test_alpaca_provider_pilot_uses_only_historical_bar_endpoints_and_is_secret_free(
    tmp_path: Path,
) -> None:
    transport = AlpacaPilotTransport()
    payload = build_alpaca_provider_pilot_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=transport,
        clock=_now,
    )
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "ALPACA_FREE_PILOT_READY_FOR_WINDOWS"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert payload["pagination_verified"] is True
    assert "alpaca-key-id" not in serialized
    assert "alpaca-secret-key" not in serialized
    assert all("/bars" in url for url in transport.urls)
    assert all("/orders" not in url and "/account" not in url for url in transport.urls)
    assert payload["pagination_requested"] is True
    assert payload["pagination_token_observed"] is True
    assert payload["second_page_fetched"] is True


def test_alpaca_provider_pilot_preserves_provider_isolation_and_observed_sip_feed(
    tmp_path: Path,
) -> None:
    payload = build_alpaca_provider_pilot_report(
        ApplicationConfig(),
        values={
            "APCA_API_KEY_ID": "alpaca-key-id",
            "APCA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=AlpacaPilotTransport(),
        clock=_now,
    )
    coverage = cast(tuple[dict[str, object], ...], payload["sample_coverage"])
    summary = cast(dict[str, object], payload["data_quality_summary"])
    mappings = cast(tuple[dict[str, object], ...], payload["provider_mappings"])

    assert {item["provider"] for item in coverage} == {"alpaca"}
    assert summary["stock_etf_feed_observed"] == ("sip",)
    assert summary["provider_isolation"] == "alpaca bars are cached under provider=alpaca only"
    assert any(
        item["broker_symbol"] == "DIA"
        and item["provider_symbol"] == "DIA"
        and item["expected_asset_class"] == "ETF"
        for item in mappings
    )
    dia_status = cast(dict[str, object], payload["dia_status"])
    assert dia_status["invalid_etoro_crypto_mapping"] == "REMAINS_QUARANTINED"


def test_alpaca_provider_pilot_quarantines_incorrect_sol_broker_reference(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    etoro_sol = _instrument(
        "SOL",
        instrument_id=100063,
        asset_class=AssetClass.CRYPTO,
        broker="etoro",
    )
    old_alpaca_sol = _instrument(
        "SOL",
        instrument_id=100002,
        asset_class=AssetClass.CRYPTO,
        broker="etoro",
    )
    cache.upsert_bars(
        provider="etoro",
        bars=_bars(etoro_sol, timeframe=TimeFrame.ONE_DAY, count=2, source="etoro"),
        fetched_at=_now(),
        mapping=ProviderInstrumentReference(
            provider="etoro",
            provider_symbol="100063",
            broker="etoro",
            broker_symbol="SOL",
            broker_instrument_id="100063",
            exchange="Digital Currency",
            asset_class=AssetClass.CRYPTO,
            currency=Currency.USD,
            mapping_confidence=Decimal("1"),
            mapping_source="official broker instrument id",
            verified=True,
        ),
    )
    cache.upsert_bars(
        provider="alpaca",
        bars=_bars(
            old_alpaca_sol,
            timeframe=TimeFrame.ONE_DAY,
            count=2,
            source="alpaca",
        ),
        fetched_at=_now(),
        mapping=ProviderInstrumentReference(
            provider="alpaca",
            provider_symbol="SOL/USD",
            broker="etoro",
            broker_symbol="SOL",
            broker_instrument_id="100002",
            exchange="ALPACA_CRYPTO_US",
            asset_class=AssetClass.CRYPTO,
            currency=Currency.USD,
            mapping_confidence=Decimal("1"),
            mapping_source="obsolete pilot mapping",
            verified=True,
        ),
    )

    payload = build_alpaca_provider_pilot_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=AlpacaPilotTransport(),
        clock=_now,
    )
    quarantine = cast(dict[str, object], payload["mapping_quarantine"])
    reconciliation = cast(
        tuple[dict[str, object], ...],
        payload["backfill_mapping_reconciliation"],
    )
    sol = next(item for item in reconciliation if item["symbol"] == "SOL")

    assert quarantine["quarantined_rows"] == 2
    assert sol["canonical_broker_instrument_id"] == "100063"
    assert sol["mapping_state"] == "VERIFIED_CROSS_PROVIDER"
    assert "100002" not in json.dumps(sol, sort_keys=True)


def test_alpaca_backfill_mapping_does_not_fabricate_missing_etoro_reference(
    tmp_path: Path,
) -> None:
    payload = build_alpaca_provider_pilot_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=AlpacaPilotTransport(),
        clock=_now,
    )
    reconciliation = cast(
        tuple[dict[str, object], ...],
        payload["backfill_mapping_reconciliation"],
    )
    dia = next(item for item in reconciliation if item["symbol"] == "DIA")

    assert dia["mapping_state"] == "ALPACA_ONLY_VERIFIED"
    assert dia["canonical_broker_instrument_id"] is None
    assert dia["ticker_equality_sufficient"] is False


def test_alpaca_cross_provider_report_uses_provider_neutral_names(tmp_path: Path) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    instrument = _instrument(
        "AAPL",
        instrument_id=1001,
        asset_class=AssetClass.EQUITY,
        broker="etoro",
    )
    cache.upsert_bars(
        provider="etoro",
        bars=_bars(
            instrument,
            timeframe=TimeFrame.ONE_DAY,
            count=30,
            end_at=datetime(2026, 8, 14, tzinfo=UTC),
            source="etoro",
        ),
        fetched_at=_now(),
        mapping=ProviderInstrumentReference(
            provider="etoro",
            provider_symbol="1001",
            broker="etoro",
            broker_symbol="AAPL",
            broker_instrument_id="1001",
            exchange="NASDAQ",
            asset_class=AssetClass.EQUITY,
            currency=Currency.USD,
            mapping_confidence=Decimal("1"),
            mapping_source="official broker instrument id",
            verified=True,
        ),
    )

    payload = build_alpaca_provider_pilot_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=AlpacaPilotTransport(),
        clock=_now,
    )
    overlaps = cast(tuple[dict[str, object], ...], payload["cross_provider_integrity"])
    alpaca_to_etoro = next(
        item
        for item in overlaps
        if item["symbol"] == "AAPL"
        and item["timeframe"] == "1D"
        and item["reference_provider"] == "etoro"
    )
    serialized = json.dumps(alpaca_to_etoro, sort_keys=True)

    assert alpaca_to_etoro["left_provider"] == "alpaca"
    assert "left_provider_missing_overlap_bars" in alpaca_to_etoro
    assert "reference_provider_missing_overlap_bars" in alpaca_to_etoro
    assert "polygon_missing_overlap_bars" not in serialized


def test_alpaca_provider_pilot_verifies_crypto_pairs_only_from_provider_bars(
    tmp_path: Path,
) -> None:
    payload = build_alpaca_provider_pilot_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=AlpacaPilotTransport(sol_has_data=False),
        clock=_now,
    )
    summary = cast(dict[str, object], payload["data_quality_summary"])

    assert payload["status"] == "ALPACA_ADAPTER_BLOCKER"
    assert summary["crypto_pairs_verified_by_response"] == ("BTC", "ETH")
    assert "SOL" not in summary["crypto_pairs_verified_by_response"]


def test_alpaca_full_backfill_requires_credentials_and_stays_read_only(tmp_path: Path) -> None:
    payload = build_alpaca_full_backfill_report(
        ApplicationConfig(),
        values={},
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        clock=_now,
    )

    assert payload["status"] == "ALPACA_API_KEYS_NOT_CONFIGURED"
    assert payload["category"] == "ALPACA_API_KEYS_NOT_CONFIGURED"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert "alpaca-full-backfill" in str(payload["windows_cmd"])


def test_alpaca_full_backfill_uses_same_apca_alias_credentials_as_pilot(
    tmp_path: Path,
) -> None:
    transport = AlpacaFullBackfillTransport()

    payload = build_alpaca_full_backfill_report(
        ApplicationConfig(),
        values={
            "APCA_API_KEY_ID": "alpaca-key-id",
            "APCA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=transport,
        clock=_now,
    )
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "ALPACA_FULL_BACKFILL_COMPLETE"
    assert payload["broker_write_calls"] == 0
    assert "alpaca-key-id" not in serialized
    assert "alpaca-secret-key" not in serialized
    assert transport.headers[0]["APCA-API-KEY-ID"] == "alpaca-key-id"
    assert transport.headers[0]["APCA-API-SECRET-KEY"] == "alpaca-secret-key"


def test_alpaca_full_backfill_uses_runtime_dotenv_loader_path(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "APCA_API_KEY_ID=alpaca-key-id",
                "APCA_API_SECRET_KEY=alpaca-secret-key",
            )
        ),
        encoding="utf-8",
    )
    runtime_values = load_runtime_values(values={}, env_file=env_file)
    transport = AlpacaFullBackfillTransport()

    payload = build_alpaca_full_backfill_report(
        load_config(runtime_values),
        values=runtime_values,
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=transport,
        clock=_now,
    )
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "ALPACA_FULL_BACKFILL_COMPLETE"
    assert "alpaca-key-id" not in serialized
    assert "alpaca-secret-key" not in serialized
    assert transport.headers[0]["APCA-API-KEY-ID"] == "alpaca-key-id"


def test_alpaca_full_backfill_fetches_all_symbol_timeframes_with_pagination(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    transport = AlpacaFullBackfillTransport()

    payload = build_alpaca_full_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=transport,
        clock=_now,
    )
    rows = cast(tuple[dict[str, object], ...], payload["backfill"])
    summary = cast(dict[str, object], payload["summary"])
    pagination = cast(dict[str, object], payload["pagination"])
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "ALPACA_FULL_BACKFILL_COMPLETE"
    assert len(rows) == 34 * 2
    assert summary["successful_symbol_timeframes"] == 68
    assert summary["total_bars"] == 68 * 4
    assert pagination["pagination_requested"] is True
    assert pagination["pagination_token_observed"] is True
    assert pagination["second_page_fetched"] is True
    assert pagination["pagination_verified"] is True
    assert pagination["rows_with_second_page"] == 68
    assert {item["pages_fetched"] for item in rows} == {2}
    assert payload["cache_duplicate_count"] == 0
    assert payload["safe_for_lifecycle_validation"] is True
    assert "alpaca-key-id" not in serialized
    assert "alpaca-secret-key" not in serialized
    assert all("etoro" not in url for url in transport.urls)
    assert all(
        "/orders" not in url and "/account" not in url and "/trading" not in url
        for url in transport.urls
    )


def test_alpaca_full_backfill_is_idempotent_and_does_not_rewrite_identical_cache(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")

    build_alpaca_full_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=AlpacaFullBackfillTransport(),
        clock=_now,
    )
    second = build_alpaca_full_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=AlpacaFullBackfillTransport(),
        clock=_now,
    )
    summary = cast(dict[str, object], second["summary"])

    assert second["status"] == "ALPACA_FULL_BACKFILL_COMPLETE"
    assert summary["cache_inserted"] == 0
    assert summary["cache_updated"] == 0
    assert summary["cache_unchanged"] == 68 * 4


def test_alpaca_full_backfill_reports_unsupported_symbol_without_fallback(
    tmp_path: Path,
) -> None:
    transport = AlpacaFullBackfillTransport(unsupported_symbol="SOL")
    payload = build_alpaca_full_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=transport,
        clock=_now,
    )
    summary = cast(dict[str, object], payload["summary"])
    rows = cast(tuple[dict[str, object], ...], payload["backfill"])
    unsupported = cast(tuple[str, ...], summary["unsupported_or_blocked"])

    assert payload["status"] == "ALPACA_MAPPING_RECONCILIATION_REQUIRED"
    assert "SOL:1D:NO_DATA" in unsupported
    assert "SOL:4H:NO_DATA" in unsupported
    assert all(item["provider"] == "alpaca" for item in rows)
    assert all("etoro" not in url for url in transport.urls)


def test_alpaca_full_backfill_permission_error_is_provider_blocker(
    tmp_path: Path,
) -> None:
    payload = build_alpaca_full_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=RaisingTextTransport(PermissionError(13, "Access denied by OS policy")),
        clock=_now,
    )
    rows = cast(tuple[dict[str, object], ...], payload["backfill"])
    first = rows[0]

    assert payload["status"] == "ALPACA_PROVIDER_BLOCKER"
    assert first["final_status"] == "PROVIDER_UNAVAILABLE"
    assert first["transport_category"] == "PermissionError"
    assert first["exception_type"] == "PermissionError"
    assert first["exception_message"] == "[Errno 13] Access denied by OS policy"
    assert first["errno"] == 13
    assert first["http_status"] is None
    assert "data.alpaca.markets" in str(first["sanitized_endpoint"])
    assert payload["broker_write_calls"] == 0


def test_alpaca_core_4h_requires_verified_canonical_mappings(tmp_path: Path) -> None:
    payload = build_alpaca_core_4h_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        transport=AlpacaCore4HTransport(),
        clock=_now,
    )

    assert payload["status"] == "ALPACA_MAPPING_RECONCILIATION_REQUIRED"
    assert set(cast(tuple[str, ...], payload["missing_canonical_mappings"])) == {
        "AAPL",
        "MSFT",
        "SPY",
        "QQQ",
        "GLD",
        "BTC",
        "ETH",
        "SOL",
    }
    assert payload["backfill"] == ()
    assert payload["broker_write_calls"] == 0


def test_alpaca_core_4h_ready_for_fixed_universe_with_existing_cache(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    _seed_core_4h_etoro_mappings(cache)
    transport = AlpacaCore4HTransport()

    payload = build_alpaca_core_4h_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=transport,
        clock=_now,
    )
    rows = cast(tuple[dict[str, object], ...], payload["backfill"])
    summary = cast(dict[str, object], payload["summary"])

    assert payload["status"] == "ALPACA_CORE_4H_READY"
    assert len(rows) == 8
    assert {item["symbol"] for item in rows} == {
        "AAPL",
        "MSFT",
        "SPY",
        "QQQ",
        "GLD",
        "BTC",
        "ETH",
        "SOL",
    }
    assert {item["timeframe"] for item in rows} == {"4H"}
    assert {item["final_status"] for item in rows} == {"SUCCESS"}
    assert summary["successful_symbol_timeframes"] == 8
    assert summary["total_bars"] == 8 * 6571
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False


def test_alpaca_core_4h_is_resumable_without_refetching_current_cache(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    _seed_core_4h_etoro_mappings(cache)
    build_alpaca_core_4h_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=AlpacaCore4HTransport(),
        clock=_now,
    )
    second_transport = AlpacaCore4HTransport()

    second = build_alpaca_core_4h_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=second_transport,
        clock=_now,
    )
    rows = cast(tuple[dict[str, object], ...], second["backfill"])

    assert second["status"] == "ALPACA_CORE_4H_READY"
    assert second_transport.urls == []
    assert {item["cache_status"] for item in rows} == {"CACHE_HIT"}
    assert {item["fetched_bars"] for item in rows} == {0}


def test_alpaca_core_4h_rejects_truncated_pagination(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    _seed_core_4h_etoro_mappings(cache)

    payload = build_alpaca_core_4h_backfill_report(
        ApplicationConfig(),
        values={
            "ALPACA_API_KEY_ID": "alpaca-key-id",
            "ALPACA_API_SECRET_KEY": "alpaca-secret-key",
        },
        cache=cache,
        transport=AlpacaCore4HTransport(always_truncated=True),
        clock=_now,
    )
    rows = cast(tuple[dict[str, object], ...], payload["backfill"])

    assert payload["status"] == "ALPACA_CORE_4H_INCOMPLETE"
    assert {item["final_status"] for item in rows} == {"PAGINATION_TRUNCATED"}
    assert {item["pagination_truncated"] for item in rows} == {True}


def test_lifecycle_walkforward_readiness_blocks_missing_core_1d_cache(
    tmp_path: Path,
) -> None:
    payload = build_lifecycle_walkforward_readiness_report(
        ApplicationConfig(),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        clock=_now,
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "CORE_1D_CACHE_INSUFFICIENT"
    assert set(cast(tuple[str, ...], payload["missing_or_insufficient_symbols"])) == {
        "AAPL",
        "MSFT",
        "SPY",
        "QQQ",
        "GLD",
        "BTC",
        "ETH",
        "SOL",
    }
    assert payload["broker_write_calls"] == 0


def test_lifecycle_walkforward_readiness_uses_cached_1d_path_without_broker_writes(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    _seed_core_4h_etoro_mappings(cache)
    _seed_core_1d_alpaca_cache(cache)

    payload = build_lifecycle_walkforward_readiness_report(
        ApplicationConfig(),
        cache=cache,
        clock=_now,
    )
    run = cast(dict[str, object], payload["validation_run"])
    anti_lookahead = cast(dict[str, object], payload["anti_lookahead"])
    lifecycle = cast(dict[str, object], payload["lifecycle"])

    assert payload["status"] == "LIFECYCLE_WALKFORWARD_READY"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert payload["missing_pieces"] == ()
    assert run["timeframe"] == "1D"
    assert anti_lookahead["future_records_never_visible"] is True
    assert anti_lookahead["future_records_ignored_observed"] is True
    assert "entry_count" in lifecycle
    assert "completed_trade_count" in lifecycle


def test_lifecycle_zero_trade_forensics_blocks_missing_core_1d_cache(
    tmp_path: Path,
) -> None:
    payload = build_lifecycle_zero_trade_forensics_report(
        ApplicationConfig(),
        cache=HistoricalDataCache(tmp_path / "market-data-cache.sqlite3"),
        clock=_now,
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "CORE_1D_CACHE_INSUFFICIENT"
    assert payload["broker_write_calls"] == 0


def test_lifecycle_zero_trade_forensics_traces_cached_1d_pipeline_without_writes(
    tmp_path: Path,
) -> None:
    cache = HistoricalDataCache(tmp_path / "market-data-cache.sqlite3")
    _seed_core_4h_etoro_mappings(cache)
    _seed_core_1d_alpaca_cache(cache)

    payload = build_lifecycle_zero_trade_forensics_report(
        ApplicationConfig(),
        cache=cache,
        clock=_now,
    )
    forensics = cast(dict[str, object], payload["forensics"])
    stage_counts = cast(dict[str, int], forensics["stage_counts"])
    first_divergence = cast(dict[str, object], forensics["first_divergence"])

    assert payload["status"] == "LIFECYCLE_ZERO_TRADE_ROOT_CAUSE_IDENTIFIED"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert stage_counts["observations"] > 0
    assert stage_counts["proposals_created"] >= 0
    assert stage_counts["risk_manager_approvals"] >= 0
    assert stage_counts["risk_manager_rejections"] >= 0
    assert stage_counts["simulated_entries"] >= 0
    assert first_divergence["reason"]


def test_etoro_classifier_handles_provider_text_and_numeric_type_names() -> None:
    assert (
        classify_etoro_instrument_metadata({"internalAssetClassName": "Stocks"}).asset_class
        is AssetClass.EQUITY
    )
    assert (
        classify_etoro_instrument_metadata({"assetType": "Exchange Traded Fund"}).asset_class
        is AssetClass.ETF
    )
    assert (
        classify_etoro_instrument_metadata(
            {"instrumentTypeId": 3},
            instrument_type_names={3: "Crypto"},
        ).asset_class
        is AssetClass.CRYPTO
    )
    assert (
        classify_etoro_instrument_metadata({"internalCryptoTypeId": 7}).asset_class
        is AssetClass.CRYPTO
    )
    assert (
        classify_etoro_instrument_metadata({"instrumentClass": "Forex"}).asset_class
        is AssetClass.FOREX
    )
    assert (
        classify_etoro_instrument_metadata({"securityType": "Indices"}).asset_class
        is AssetClass.INDEX
    )
    assert (
        classify_etoro_instrument_metadata({"marketType": "Commodities"}).asset_class
        is AssetClass.COMMODITY
    )
    assert classify_etoro_instrument_metadata({"category": "Bond"}).asset_class is AssetClass.BOND
    assert (
        classify_etoro_instrument_metadata({"subCategory": "Fund"}).asset_class is AssetClass.FUND
    )
    assert (
        classify_etoro_instrument_metadata({"underlying": "Stock CFD"}).asset_class
        is AssetClass.CFD
    )


def test_etoro_classifier_distinguishes_missing_from_unknown_metadata() -> None:
    missing = classify_etoro_instrument_metadata({})
    unknown = classify_etoro_instrument_metadata({"instrumentType": "Mystery Product"})

    assert missing.asset_class is AssetClass.UNKNOWN
    assert missing.status == "CLASSIFICATION_INSUFFICIENT"
    assert unknown.asset_class is AssetClass.UNKNOWN
    assert unknown.status == "CLASSIFICATION_UNKNOWN"


def test_safe_etoro_metadata_retains_classification_fields_without_secret_fields() -> None:
    metadata = safe_instrument_classification_metadata(
        {
            "instrumentTypeID": 1,
            "internalAssetClassName": "Stocks",
            "x-api-key": "secret",
            "x-user-key": "secret",
            "Authorization": "secret",
        }
    )

    assert metadata == {"instrumentTypeID": 1, "internalAssetClassName": "Stocks"}


def test_etoro_instrument_schema_report_is_sanitized_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSchemaClient(FakeEtoroHistoryClient):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            super().__init__(search_has_classification=False)

    monkeypatch.setattr(app.data.runtime, "EtoroReadClient", FakeSchemaClient)

    payload = build_etoro_instrument_schema_report(
        ApplicationConfig(etoro_api_enabled=True),
        values={"ETORO_API_KEY": "api-secret", "ETORO_USER_KEY": "user-secret"},
        symbols=("AAPL", "SPY", "BTC"),
    )

    assert payload["status"] == "ETORO_INSTRUMENT_SCHEMA_PROBE"
    assert payload["validation_runtime_version"] == "real-validation-runtime-v3"
    assert payload["broker_write_calls"] == 0
    symbols = {
        item["symbol"]: item for item in cast(tuple[dict[str, Any], ...], payload["symbols"])
    }
    assert symbols["AAPL"]["canonical_asset_class"] == "EQUITY"
    assert symbols["SPY"]["canonical_asset_class"] == "ETF"
    assert symbols["BTC"]["canonical_asset_class"] == "CRYPTO"
    serialized = json.dumps(payload, sort_keys=True)
    assert "api-secret" not in serialized
    assert "user-secret" not in serialized
    assert "x-api-key" not in serialized
    assert "x-user-key" not in serialized


def test_etoro_instrument_schema_report_fails_closed_without_credentials() -> None:
    payload = build_etoro_instrument_schema_report(
        ApplicationConfig(etoro_api_enabled=True),
        values={},
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "CREDENTIALS"
    assert payload["broker_write_calls"] == 0


def test_etoro_instrument_schema_report_fails_closed_when_api_disabled() -> None:
    payload = build_etoro_instrument_schema_report(
        ApplicationConfig(etoro_api_enabled=False),
        values={"ETORO_API_KEY": "api-secret", "ETORO_USER_KEY": "user-secret"},
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "API_DISABLED"
    assert payload["broker_write_calls"] == 0


def test_etoro_instrument_schema_report_fails_closed_when_demo_execution_enabled() -> None:
    payload = build_etoro_instrument_schema_report(
        ApplicationConfig(
            operating_mode="ETORO_DEMO",
            etoro_api_enabled=True,
            etoro_demo_execution_enabled=True,
        ),
        values={"ETORO_API_KEY": "api-secret", "ETORO_USER_KEY": "user-secret"},
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "DEMO_EXECUTION_ENABLED"
    assert payload["broker_write_calls"] == 0


def test_schema_probe_reports_unresolved_without_classification() -> None:
    class UnresolvedSchemaClient(FakeEtoroHistoryClient):
        def raw_instrument_search(self, symbol: str) -> object:
            self.calls.append(f"raw-search:{symbol}")
            return {
                "items": [
                    {
                        "instrumentId": 999,
                        "internalSymbolFull": "OTHER",
                        "displayname": "Other market",
                    }
                ]
            }

        def instrument_metadata(
            self,
            instrument_ids: tuple[int, ...],
        ) -> dict[int, dict[str, object]]:
            raise AssertionError("metadata should not be read without a winning instrument")

    result = app.data.runtime._safe_schema_probe(
        cast(EtoroReadClient, UnresolvedSchemaClient()),
        "AAPL",
    )

    assert result["resolved"] is False
    assert result["broker_instrument_id"] is None
    assert result["canonical_asset_class"] == "UNKNOWN"
    assert result["classification_status"] == "CLASSIFICATION_INSUFFICIENT"


def test_schema_probe_reports_unknown_provider_type() -> None:
    class UnknownSchemaClient(FakeEtoroHistoryClient):
        def raw_instrument_search(self, symbol: str) -> object:
            self.calls.append(f"raw-search:{symbol}")
            return {
                "items": [
                    {
                        "instrumentId": 1001,
                        "internalSymbolFull": symbol,
                        "displayname": "Mystery market",
                        "instrumentType": "Mystery Product",
                    }
                ]
            }

        def instrument_metadata(
            self,
            instrument_ids: tuple[int, ...],
        ) -> dict[int, dict[str, object]]:
            self.calls.append(f"metadata:{instrument_ids[0]}")
            return {}

    result = app.data.runtime._safe_schema_probe(
        cast(EtoroReadClient, UnknownSchemaClient()),
        "AAPL",
    )

    assert result["resolved"] is True
    assert result["canonical_asset_class"] == "UNKNOWN"
    assert result["classification_status"] == "CLASSIFICATION_UNKNOWN"


def test_unknown_research_asset_class_reports_classification_not_policy_disabled() -> None:
    admitted, reason = app.data.runtime._research_asset_admission(
        AssetClass.UNKNOWN,
        default_asset_policy_engine(),
    )

    assert admitted is False
    assert reason == "CLASSIFICATION_INSUFFICIENT"


def test_canonical_policy_diagnostics_admit_supported_defaults_and_block_derivatives() -> None:
    diagnostics = {row["asset_class"]: row for row in build_policy_strategy_diagnostics()}

    for asset_class in ("EQUITY", "ETF", "CRYPTO"):
        assert diagnostics[asset_class]["policy_enabled"] is True
        assert diagnostics[asset_class]["policy_long_allowed"] is True
        assert diagnostics[asset_class]["policy_leverage_allowed"] is False
        assert diagnostics[asset_class]["strategy_profile_enabled"] is True
        assert diagnostics[asset_class]["policy_version"] == DEFAULT_POLICY_VERSION
    for asset_class in ("CFD", "FUTURE", "OPTION", "UNKNOWN"):
        assert diagnostics[asset_class]["policy_enabled"] is False


def test_real_data_validator_and_scanner_share_canonical_policy_source() -> None:
    scanner_policy = default_asset_policy_engine()
    validation_diagnostics = build_policy_strategy_diagnostics(default_asset_policy_engine())
    equity = next(row for row in validation_diagnostics if row["asset_class"] == "EQUITY")
    crypto = next(row for row in validation_diagnostics if row["asset_class"] == "CRYPTO")

    assert equity["policy_version"] == scanner_policy.policy_version
    assert crypto["policy_version"] == scanner_policy.policy_version
    assert scanner_policy.policy_for(AssetClass.EQUITY).enabled is True
    assert scanner_policy.policy_for(AssetClass.CRYPTO).enabled is True


def test_asset_strategy_profile_is_not_confused_with_asset_policy() -> None:
    policy_engine = AssetPolicyEngine(
        (
            AssetPolicy(
                asset_class=AssetClass.EQUITY,
                enabled=False,
                long_allowed=False,
                require_broker_eligibility=False,
            ),
        ),
        policy_version="test-policy-disabled",
    )
    diagnostics = build_policy_strategy_diagnostics(policy_engine)
    equity = next(row for row in diagnostics if row["asset_class"] == "EQUITY")

    assert equity["policy_enabled"] is False
    assert equity["strategy_profile_enabled"] is True
    assert equity["policy_version"] == "test-policy-disabled"
    assert equity["policy_missing"] is False


def test_etoro_asset_class_mapping_uses_metadata_aliases_not_ticker_text() -> None:
    assert asset_class_from_etoro_instrument_type("US Listed Stock") is AssetClass.EQUITY
    assert asset_class_from_etoro_instrument_type("Exchange_Traded_Fund") is AssetClass.ETF
    assert asset_class_from_etoro_instrument_type("Crypto Asset") is AssetClass.CRYPTO
    assert asset_class_from_etoro_instrument_type("Stock CFD") is AssetClass.CFD
    assert asset_class_from_etoro_instrument_type("Mystery") is AssetClass.UNKNOWN


def test_real_data_cli_without_credentials_blocks_crypto_without_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            (
                "validate-strategies",
                "--real-data",
                "--asset-class",
                "CRYPTO",
                "--timeframe",
                "1D",
            ),
            values={},
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "NO_RESEARCH_UNIVERSE"
    assert payload["broker_write"] is False
    assert "validate-strategies --real-data" in payload["windows_cmd"]


def test_decision_funnel_zero_trade_diagnostics_and_threshold_sensitivity() -> None:
    decisions = (
        _decision(
            score=Decimal("65"),
            confidence=Decimal("0.80"),
            blocker_reasons=("opportunity score is below the asset profile threshold",),
            strategy_directions=(StrategyDirection.BUY, StrategyDirection.WATCH),
        ),
        _decision(
            score=Decimal("80"),
            confidence=Decimal("0.50"),
            blocker_reasons=("confidence is below the asset profile threshold",),
            strategy_directions=(StrategyDirection.AVOID, StrategyDirection.HOLD),
        ),
        _decision(
            score=Decimal("82"),
            confidence=Decimal("0.80"),
            status=ReplayDecisionStatus.SIMULATED_EXECUTED,
            strategy_directions=(StrategyDirection.STRONG_BUY,),
        ),
    )

    funnel = build_decision_funnel(decisions, ())
    diagnostics = diagnose_zero_or_low_trades(decisions)
    sensitivity = build_threshold_sensitivity(decisions)

    assert funnel.buy_signals == 2
    assert funnel.watch_signals == 1
    assert funnel.avoid_signals == 1
    assert funnel.simulated_executed == 1
    assert diagnostics[0].blocker == "OpportunityScore:below threshold"
    assert any(point.score_delta == Decimal("-5") for point in sensitivity)
    assert all("research-only" in point.note for point in sensitivity)


def test_real_data_runtime_blocks_when_demo_execution_enabled() -> None:
    payload = build_real_strategy_validation_report(
        ApplicationConfig(
            operating_mode="ETORO_DEMO",
            etoro_api_enabled=True,
            etoro_demo_execution_enabled=True,
        ),
        clock=_now,
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "DEMO_EXECUTION_ENABLED"
    assert payload["broker_write_calls"] == 0


def test_risk_manager_behavior_is_unchanged_for_disabled_derivatives() -> None:
    manager = RiskManager(
        ApplicationConfig().risk,
        KillSwitch(active=False, reason="test", clock=_now),
        authorization_key=b"step80a-policy-wiring-risk-key-32",
        clock=_now,
    )

    evaluation = manager.evaluate(
        _risk_proposal(AssetClass.CFD, SettlementType.CFD),
        _risk_context(AssetClass.CFD, SettlementType.CFD),
    )

    assert evaluation.decision.status is RiskDecisionStatus.REJECTED


def test_step80a_validation_modules_remain_broker_write_and_secret_free() -> None:
    for module in (
        app.validation.datasets,
        app.validation.diagnostics,
        app.validation.runtime,
    ):
        source = inspect.getsource(module)
        assert "submit_demo" not in source
        assert "post_once" not in source
        assert "market-open-orders" not in source
        assert "ETORO_API_KEY" not in source
        assert "ETORO_USER_KEY" not in source


def test_no_policy_bypass_switch_exists_in_real_data_runtime() -> None:
    import app.data.runtime

    source = inspect.getsource(app.data.runtime)
    assert "research_ignore_policy" not in source
    assert "force_enable_asset_class" not in source
    assert "allow_all_assets" not in source


def test_confidence_ablation_is_deterministic_and_research_only() -> None:
    decisions = (
        _decision_with_confidence_decomposition(
            score=Decimal("82"),
            confidence=Decimal("0.47"),
            forward_return=Decimal("0.03"),
            asset_class=AssetClass.EQUITY,
        ),
        _decision_with_confidence_decomposition(
            score=Decimal("76"),
            confidence=Decimal("0.44"),
            forward_return=Decimal("-0.01"),
            asset_class=AssetClass.CRYPTO,
            regime=RegimeLabel.RANGE,
        ),
    )

    study = build_confidence_ablation_study(decisions)
    repeat = build_confidence_ablation_study(decisions)

    assert study == repeat
    assert study["status"] == "RESEARCH_COUNTERFACTUAL_ONLY"
    assert study["production_formula_changed"] is False
    variants = cast(tuple[dict[str, Any], ...], study["variants"])
    assert {variant["variant"] for variant in variants} >= {
        "BASELINE",
        "ABLATION_A_REMOVE_REGIME_MULTIPLIER_FROM_ENSEMBLE",
        "ABLATION_G_ENSEMBLE_ONLY_CURRENT_INTERNAL_PENALTIES",
    }
    assert all(variant["scope"] == "RESEARCH_COUNTERFACTUAL_ONLY" for variant in variants)
    baseline = next(variant for variant in variants if variant["variant"] == "BASELINE")
    assert baseline["forward_outcome_calibration"]
    assert baseline["asset_class_breakdown"]
    assert baseline["regime_breakdown"]


def test_confidence_ablation_measures_duplicate_evidence_and_rounding() -> None:
    decisions = (
        _decision_with_confidence_decomposition(
            score=Decimal("82"),
            confidence=Decimal("0.47"),
            forward_return=Decimal("0.03"),
        ),
        _decision_with_confidence_decomposition(
            score=Decimal("62"),
            confidence=Decimal("0.43"),
            forward_return=Decimal("-0.02"),
            quality="GOOD",
        ),
    )

    study = build_confidence_ablation_study(decisions)
    deltas = cast(tuple[dict[str, str], ...], study["component_marginal_deltas"])
    rounding = cast(dict[str, Any], study["rounding_sensitivity"])
    overlap = cast(dict[str, str], study["score_confidence_overlap"])
    quality = cast(dict[str, Any], study["quality_penalty_semantics"])

    assert any(item["component"] == "without_final_data_quality_term" for item in deltas)
    assert rounding["production_rounding_changed"] is False
    assert "ranking_correlation_unrounded_vs_rounded" in rounding
    assert "pearson_score_confidence" in overlap
    assert quality["quality_states"]
    assert "macd" in quality["most_common_missing_or_insufficient_features"]


def test_strategy_profile_threshold_metadata_remains_safety_default() -> None:
    for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO):
        profile = profile_for(asset_class)
        assert profile.confidence_threshold_provenance == "SAFETY_DEFAULT"
        assert profile.calibration_dataset_id is None
        assert profile.calibration_version is None
        assert profile.calibration_timestamp is None
        assert profile.validation_status == "NOT_EMPIRICALLY_CALIBRATED"


def test_guarded_v2b_confidence_profile_is_explicit_versioned_and_reversible() -> None:
    legacy_profiles = legacy_v1_asset_strategy_profiles()
    guarded_profiles = guarded_v2b_asset_strategy_profiles()
    default_profiles = asset_strategy_profiles_for_confidence_profile("V1_LEGACY")
    opt_in_profiles = asset_strategy_profiles_for_confidence_profile("V2_B_GUARDED")

    assert default_profiles == legacy_profiles
    assert opt_in_profiles == guarded_profiles
    for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO):
        legacy = legacy_profile_for(asset_class)
        guarded = next(
            profile for profile in guarded_profiles if profile.asset_class is asset_class
        )

        assert legacy.confidence_model_version == "V1_LEGACY"
        assert legacy.confidence_threshold_provenance == "SAFETY_DEFAULT"
        assert guarded.confidence_model_version == "V2_B_GUARDED_V1"
        assert guarded.confidence_semantics_version == "SIGNAL_RELIABILITY_V2"
        assert guarded.minimum_confidence_for_buy == Decimal("0.5475")
        assert guarded.confidence_threshold_provenance == "EMPIRICALLY_CALIBRATED_GUARDED"
        assert guarded.calibration_dataset_id == "cached-etoro-step80a-1d"
        assert guarded.calibration_version == "STEP80A_V2B_2026_08_29"
        assert guarded.validation_status == "GUARDED_EMPIRICALLY_CALIBRATED_STEP80A"
        assert guarded.evidence_warning is not None

    assert legacy_profile_for(AssetClass.EQUITY).minimum_confidence_for_buy == Decimal("0.65")
    assert legacy_profile_for(AssetClass.ETF).minimum_confidence_for_buy == Decimal("0.62")
    assert legacy_profile_for(AssetClass.CRYPTO).minimum_confidence_for_buy == Decimal("0.72")


def test_guarded_v2b_profile_keeps_asset_specific_evidence_warning_metadata() -> None:
    guarded_profiles = {
        profile.asset_class: profile for profile in guarded_v2b_asset_strategy_profiles()
    }

    assert guarded_profiles[AssetClass.EQUITY].evidence_warning == "EQUITY_OOS_EVIDENCE_WEAK"
    assert guarded_profiles[AssetClass.ETF].evidence_warning == "ETF_OOS_EVIDENCE_BETTER"
    assert guarded_profiles[AssetClass.CRYPTO].evidence_warning == "CRYPTO_OOS_EVIDENCE_BETTER"


def test_ablation_calibration_methodology_preserves_oos_separation() -> None:
    study = build_confidence_ablation_study(
        (
            _decision_with_confidence_decomposition(
                score=Decimal("82"),
                confidence=Decimal("0.47"),
                forward_return=Decimal("0.03"),
            ),
        )
    )
    methodology = cast(tuple[str, ...], study["calibration_methodology"])
    joined = " ".join(methodology)

    assert "TRAIN" in joined
    assert "VALIDATION" in joined
    assert "OUT_OF_SAMPLE" in joined
    assert "Freeze" in joined
    assert "maximizing return on the full historical dataset" in joined


def test_v2_confidence_research_separates_signal_and_execution_semantics() -> None:
    decisions = tuple(
        _decision_with_confidence_decomposition(
            score=Decimal("82"),
            confidence=Decimal("0.47"),
            forward_return=Decimal("0.03") if index % 2 else Decimal("-0.01"),
            quality="PARTIAL",
        ).model_copy(
            update={
                "proposal_gate_trace": (
                    {
                        "gate": "opportunity_score",
                        "actual": "82",
                        "threshold": "70",
                        "passed": True,
                    },
                )
            }
        )
        for index in range(80)
    )

    study = build_confidence_v2_research_study(decisions)

    assert study["status"] == "RESEARCH_COUNTERFACTUAL_ONLY"
    assert study["production_changed"] is False
    matrix = cast(tuple[dict[str, str], ...], study["input_classification_matrix"])
    assert any(
        item["component"] == "spread" and item["classification"] == "EXECUTION_READINESS"
        for item in matrix
    )
    feature_matrix = cast(tuple[dict[str, str], ...], study["feature_matrix"])
    assert any(
        item["feature"] == "spread" and item["category"] == "EXECUTION_MICROSTRUCTURE_FEATURE"
        for item in feature_matrix
    )
    variants = cast(tuple[dict[str, Any], ...], study["variants"])
    assert all("calibration_error" in item for item in variants)
    assert all("precision_high_confidence" in item for item in variants)
    assert all("false_discovery_rate_above_median" in item for item in variants)
    assert {item["variant"] for item in variants} >= {
        "V1_BASELINE",
        "V2_A_SIGNAL_RELIABILITY_AGREEMENT_REGIME_REQUIRED_FEATURES",
        "V2_B_WEIGHTED_RELIABILITY_COMPONENTS",
        "V2_C_TRAIN_CALIBRATED_RELIABILITY_MAPPING",
    }


def test_v2b_research_and_production_calculators_have_formula_parity() -> None:
    decision = _decision_with_confidence_decomposition(
        score=Decimal("82"),
        confidence=Decimal("0.44"),
        forward_return=Decimal("0.03"),
    ).model_copy(
        update={
            "strategy_signal_counts": {"BUY": 2, "HOLD": 1, "AVOID": 1},
            "confidence_decomposition": {
                "strategy_confidences": (
                    {"confidence": "0.70"},
                    {"confidence": "0.65"},
                    {"confidence": "0.55"},
                    {"confidence": "0.40"},
                ),
                "mean_base_strategy_confidence": "0.5750",
                "agreement_ratio": "0.50",
                "regime_confidence": "0.60",
                "insufficient_feature_names": (),
            },
        }
    )
    production = compute_v2b_signal_reliability_confidence(
        strategy_directions=(
            StrategyDirection.BUY,
            StrategyDirection.BUY,
            StrategyDirection.HOLD,
            StrategyDirection.AVOID,
        ),
        strategy_confidences=(
            Decimal("0.70"),
            Decimal("0.65"),
            Decimal("0.55"),
            Decimal("0.40"),
        ),
        agreement_ratio=Decimal("0.50"),
        regime_confidence=Decimal("0.60"),
        required_feature_sufficiency=Decimal("1"),
    )
    manual = (
        Decimal("0.25") * Decimal("0.30")
        + Decimal("0.5750") * Decimal("0.25")
        + Decimal("0.50") * Decimal("0.20")
        + Decimal("0.60") * Decimal("0.15")
        + Decimal("1") * Decimal("0.10")
    ).quantize(Decimal("0.0001"))

    study = build_confidence_v2_research_study(
        (
            decision.model_copy(
                update={"proposal_gate_trace": ({"gate": "opportunity_score", "passed": True},)}
            ),
        )
    )
    v2b = next(
        item
        for item in cast(tuple[dict[str, Any], ...], study["variants"])
        if item["variant"] == "V2_B_WEIGHTED_RELIABILITY_COMPONENTS"
    )

    assert production.confidence == manual
    assert Decimal(str(v2b["mean"])) == production.confidence


def test_v2b_confidence_has_no_forward_outcome_leakage() -> None:
    base = _decision_with_confidence_decomposition(
        score=Decimal("82"),
        confidence=Decimal("0.44"),
        forward_return=Decimal("0.03"),
    ).model_copy(
        update={
            "proposal_gate_trace": ({"gate": "opportunity_score", "passed": True},),
        }
    )
    changed_outcome = base.model_copy(update={"forward_return": Decimal("-0.50")})

    baseline_study = build_confidence_v2_research_study((base,))
    changed_study = build_confidence_v2_research_study((changed_outcome,))
    baseline_v2b = next(
        item
        for item in cast(tuple[dict[str, Any], ...], baseline_study["variants"])
        if item["variant"] == "V2_B_WEIGHTED_RELIABILITY_COMPONENTS"
    )
    changed_v2b = next(
        item
        for item in cast(tuple[dict[str, Any], ...], changed_study["variants"])
        if item["variant"] == "V2_B_WEIGHTED_RELIABILITY_COMPONENTS"
    )

    assert baseline_v2b["mean"] == changed_v2b["mean"]


def test_false_positive_rate_and_false_discovery_rate_are_distinct() -> None:
    decisions = tuple(
        _decision_with_confidence_decomposition(
            score=Decimal("82"),
            confidence=Decimal("0.44"),
            forward_return=forward_return,
        ).model_copy(
            update={
                "proposal_gate_trace": ({"gate": "opportunity_score", "passed": True},),
                "confidence_decomposition": {
                    "strategy_confidences": ({"confidence": "0.70"},),
                    "mean_base_strategy_confidence": "0.70",
                    "agreement_ratio": "0.60",
                    "regime_confidence": "0.55",
                    "insufficient_feature_names": (),
                },
                "strategy_signal_counts": signal_counts,
            }
        )
        for forward_return, signal_counts in (
            (Decimal("0.01"), {"BUY": 1}),
            (Decimal("0.01"), {"BUY": 1}),
            (Decimal("-0.01"), {"BUY": 1}),
            (Decimal("-0.01"), {"BUY": 1}),
            (Decimal("0.01"), {"AVOID": 1}),
            (Decimal("-0.01"), {"AVOID": 1}),
            (Decimal("-0.01"), {"AVOID": 1}),
        )
    )

    study = build_confidence_v2_research_study(decisions)
    v2b = next(
        item
        for item in cast(tuple[dict[str, Any], ...], study["variants"])
        if item["variant"] == "V2_B_WEIGHTED_RELIABILITY_COMPONENTS"
    )

    assert v2b["false_positive_rate_above_median"] == "0.5000"
    assert v2b["false_discovery_rate_above_median"] == "0.5000"


def test_v2_confidence_research_preserves_train_validation_oos_discipline() -> None:
    decisions = tuple(
        _decision_with_confidence_decomposition(
            score=Decimal("80"),
            confidence=Decimal("0.44"),
            forward_return=Decimal("0.02") if index % 3 else Decimal("-0.02"),
        ).model_copy(
            update={
                "timestamp": _now() + timedelta(days=index),
                "proposal_gate_trace": (
                    {
                        "gate": "opportunity_score",
                        "actual": "80",
                        "threshold": "70",
                        "passed": True,
                    },
                ),
            }
        )
        for index in range(120)
    )

    study = build_confidence_v2_research_study(decisions)
    selected = cast(tuple[dict[str, Any], ...], study["selected_thresholds_from_validation"])
    walk_forward = cast(tuple[dict[str, Any], ...], study["walk_forward"])

    assert selected
    assert all(item["selection_source"] == "VALIDATION_ONLY" for item in selected)
    assert all(item["oos_used_for_selection"] is False for item in selected)
    assert walk_forward
    assert all(window["oos_metrics"] for window in walk_forward)
    assert study["windows_command_required"] is False


def test_v2_confidence_research_reports_no_score_pass_candidates() -> None:
    decisions = (
        _decision_with_confidence_decomposition(
            score=Decimal("48"),
            confidence=Decimal("0.44"),
            forward_return=Decimal("0.01"),
        ),
    )

    study = build_confidence_v2_research_study(decisions)

    assert study["status"] == "NO_SCORE_PASS_DECISIONS"
    assert study["scope"] == "RESEARCH_COUNTERFACTUAL_ONLY"
    assert study["production_changed"] is False


def test_v2_confidence_research_keeps_insufficient_evidence_conservative() -> None:
    decisions = tuple(
        _decision_with_confidence_decomposition(
            score=Decimal("80"),
            confidence=Decimal("0.44"),
            forward_return=Decimal("0.02") if index % 2 else Decimal("-0.02"),
        ).model_copy(
            update={
                "timestamp": _now() + timedelta(days=index),
                "proposal_gate_trace": (
                    {
                        "gate": "opportunity_score",
                        "actual": "80",
                        "threshold": "70",
                        "passed": True,
                    },
                ),
            }
        )
        for index in range(20)
    )

    study = build_confidence_v2_research_study(decisions)

    assert study["recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert study["closure_status"] == "STEP_8_0A_REQUIRES_MORE_CONFIDENCE_WORK"


def test_v2_confidence_research_penalizes_missing_required_signal_features() -> None:
    complete_decisions = tuple(
        _decision_with_confidence_decomposition(
            score=Decimal("80"),
            confidence=Decimal("0.44"),
            forward_return=Decimal("0.02") if index % 2 else Decimal("-0.02"),
            quality="GOOD",
        ).model_copy(
            update={
                "timestamp": _now() + timedelta(days=index),
                "proposal_gate_trace": (
                    {
                        "gate": "opportunity_score",
                        "actual": "80",
                        "threshold": "70",
                        "passed": True,
                    },
                ),
            }
        )
        for index in range(80)
    )
    required_missing_decisions = tuple(
        _decision_with_confidence_decomposition(
            score=Decimal("80"),
            confidence=Decimal("0.44"),
            forward_return=Decimal("0.02") if index % 2 else Decimal("-0.02"),
        ).model_copy(
            update={
                "timestamp": _now() + timedelta(days=index),
                "proposal_gate_trace": (
                    {
                        "gate": "opportunity_score",
                        "actual": "80",
                        "threshold": "70",
                        "passed": True,
                    },
                ),
                "confidence_decomposition": {
                    "mean_base_strategy_confidence": "0.70",
                    "agreement_ratio": "0.60",
                    "regime_confidence": "0.55",
                    "strong_disagreement_multiplier": "1",
                    "insufficient_feature_names": ("short_term_momentum", "spread"),
                },
            }
        )
        for index in range(80)
    )

    complete_study = build_confidence_v2_research_study(complete_decisions)
    missing_study = build_confidence_v2_research_study(required_missing_decisions)
    complete_variants = cast(tuple[dict[str, Any], ...], complete_study["variants"])
    missing_variants = cast(tuple[dict[str, Any], ...], missing_study["variants"])
    complete_v2_a = next(
        item
        for item in complete_variants
        if item["variant"] == "V2_A_SIGNAL_RELIABILITY_AGREEMENT_REGIME_REQUIRED_FEATURES"
    )
    missing_v2_a = next(
        item
        for item in missing_variants
        if item["variant"] == "V2_A_SIGNAL_RELIABILITY_AGREEMENT_REGIME_REQUIRED_FEATURES"
    )

    assert Decimal(str(missing_v2_a["mean"])) < Decimal(str(complete_v2_a["mean"]))
    assert missing_study["quality_partitions"] == {"multiple_or_other": 80}
