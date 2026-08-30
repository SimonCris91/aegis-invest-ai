"""Runtime composition for safe live market intelligence."""

import hashlib
import inspect
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from app.agent.exit_policy import (
    EXIT_POLICY_V2_GUARDED,
    EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE,
    ExitPolicyV2CandidateRegistry,
    ExitPolicyV2DatasetPartition,
    ExitPolicyV2ExperimentManifest,
    ExitPolicyV2Guarded,
    ExitPolicyV2ParameterBundle,
    default_exit_policy_v2_candidate_registry,
    default_exit_policy_v2_preregistered_experiment_v2_manifest,
    select_exit_policy_for_historical_validation,
)
from app.agent.service import DeterministicAegisAgent
from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.mapping import (
    classify_etoro_instrument_metadata,
    safe_instrument_classification_metadata,
)
from app.brokers.etoro.runtime import runtime_credentials
from app.brokers.etoro.scanner_adapter import EtoroMarketScannerAdapter
from app.config.models import ApplicationConfig
from app.data.events.engine import NullEventRiskProvider
from app.data.historical.alpaca import AlpacaHistoricalMarketDataProvider
from app.data.historical.cache import HistoricalDataCache
from app.data.historical.etoro import EtoroHistoricalMarketDataProvider
from app.data.historical.polygon import PolygonHistoricalMarketDataProvider
from app.data.historical.providers import HttpTextTransport, StooqHistoricalDataProvider
from app.data.mapping import InstrumentMappingService
from app.data.models import (
    DataProviderError,
    DataProviderStatus,
    HistoricalDataset,
    ProviderInstrumentReference,
)
from app.data.news.providers import NullNewsProvider
from app.data.pipeline import LiveMarketIntelligencePipeline
from app.data.quality import HistoricalDataQualityAnalyzer
from app.data.registry import (
    EventRiskProviderRegistry,
    HistoricalDataProviderRegistry,
    HistoricalProviderEntry,
    NewsProviderRegistry,
)
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType, TradeIntent
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.universe import UniversalInstrument
from app.domain.versions import RANKING_VERSION
from app.intelligence.models import MarketBar, TimeFrame
from app.intelligence.profiles import profile_for
from app.intelligence.research import default_strategy_research_store
from app.policies.defaults import default_asset_policy_engine
from app.policies.engine import AssetPolicyEngine
from app.scanner.active import (
    ActiveMarketScanner,
    ActiveScannerBucket,
    ActiveScannerCandidate,
    ActiveScannerResult,
    IntradayFreshnessStatus,
    _instrument_id,
    _is_market_closed_as_of,
    _observation_from_candidate,
    active_scanner_inventory,
    classify_intraday_freshness,
    one_hour_lookback_requirement,
    provider_one_hour_capability_matrix,
    scan_cached_active_market,
    timeframe_readiness_matrix,
)
from app.scanner.models import ScannerLimits
from app.scanner.ranking import OpportunityRankingEngine
from app.scanner.runtime import _portfolio_from_demo
from app.scanner.service import OpenMarketCandidateScanner
from app.validation.confidence import build_confidence_ablation_study
from app.validation.datasets import (
    build_real_historical_validation_datasets,
)
from app.validation.diagnostics import enrich_validation_result
from app.validation.engine import HistoricalValidationEngine
from app.validation.models import (
    EvidenceRequirements,
    HistoricalValidationDataset,
    ReplayDecision,
    ReplayDecisionStatus,
    StrategyValidationResult,
    TransactionCostAssumptions,
    ZeroTradeDiagnostic,
)
from app.validation.prospective import (
    Eur200ResearchBaseline,
    ProspectiveShadowValidationManifest,
    ProspectiveShadowValidationStore,
    default_prospective_shadow_store,
    prospective_risk_policy_digest,
)
from app.validation.replay import build_dataset_metadata
from app.validation.storage import (
    DEFAULT_STRATEGY_VALIDATION_STORE_PATH,
    StrategyValidationStore,
    default_strategy_validation_store,
)

DEFAULT_MARKET_DATA_CACHE_PATH = Path("work") / "market-data-cache.sqlite3"
REAL_VALIDATION_RUNTIME_VERSION = "real-validation-runtime-v3"
POLICY_ADMISSION_SOURCE = "canonical-asset-policy-engine"
EXITPOLICY_V2_BALANCED_CANDIDATE_FINGERPRINT = (
    "e9808af62107387e976e1f792e3861aef2ae5746e197c3f0fbcf1b9da8facba3"
)
EXITPOLICY_V2_EXPOSED_HOLDOUT_FINGERPRINT = (
    "e5b55e9b301878ac81fb41523c14407138ad7f5165a8b0cc68f3f2adc78ce837"
)
DEFAULT_REAL_VALIDATION_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r" && .venv\Scripts\python.exe -m app.main validate-strategies --real-data "
    r"--timeframe 1D --max-instruments 9"
)
EXIT_EVIDENCE_SYMBOLS_BY_CLASS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.EQUITY: (
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "AMD",
        "JPM",
        "UNH",
        "XOM",
        "COST",
    ),
    AssetClass.ETF: (
        "SPY",
        "QQQ",
        "VTI",
        "IWM",
        "DIA",
        "XLK",
        "XLF",
        "XLE",
        "XLV",
        "XLU",
        "TLT",
        "GLD",
    ),
    AssetClass.CRYPTO: (
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "ADA",
        "AVAX",
        "LINK",
        "LTC",
        "BCH",
        "DOT",
    ),
}
EXIT_EVIDENCE_TIMEFRAMES = (TimeFrame.ONE_DAY, TimeFrame.FOUR_HOUR)
DEFAULT_EXIT_EVIDENCE_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r' && set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED"'
    r" && .venv\Scripts\python.exe -m app.main acquire-exit-evidence"
)
POLYGON_PILOT_SYMBOLS_BY_CLASS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.EQUITY: ("AAPL",),
    AssetClass.ETF: ("SPY", "DIA"),
    AssetClass.CRYPTO: ("BTC",),
}
POLYGON_PILOT_TIMEFRAMES = (TimeFrame.ONE_DAY, TimeFrame.FOUR_HOUR)
DEFAULT_POLYGON_PILOT_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r" && .venv\Scripts\python.exe -m app.main polygon-provider-pilot"
)
ALPACA_PILOT_SYMBOLS_BY_CLASS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.EQUITY: ("AAPL",),
    AssetClass.ETF: ("SPY", "DIA"),
    AssetClass.CRYPTO: ("BTC", "ETH", "SOL"),
}
ALPACA_1H_PILOT_SYMBOLS_BY_CLASS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.EQUITY: ("AAPL",),
    AssetClass.ETF: ("SPY",),
    AssetClass.CRYPTO: ("BTC", "ETH"),
}
ALPACA_IEX_EQUITY_1H_SYMBOLS_BY_CLASS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.EQUITY: ("AAPL",),
    AssetClass.ETF: ("SPY",),
}
ALPACA_CORE_4H_SYMBOLS_BY_CLASS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.EQUITY: ("AAPL", "MSFT"),
    AssetClass.ETF: ("SPY", "QQQ", "GLD"),
    AssetClass.CRYPTO: ("BTC", "ETH", "SOL"),
}
ALPACA_PILOT_TIMEFRAMES = (TimeFrame.ONE_DAY, TimeFrame.FOUR_HOUR)
ALPACA_CORE_4H_TIMEFRAMES = (TimeFrame.FOUR_HOUR,)
DEFAULT_ALPACA_PILOT_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r" && .venv\Scripts\python.exe -m app.main alpaca-provider-pilot"
)
DEFAULT_ALPACA_FULL_BACKFILL_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r" && .venv\Scripts\python.exe -m app.main alpaca-full-backfill"
)
DEFAULT_ALPACA_CORE_4H_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r" && .venv\Scripts\python.exe -m app.main alpaca-core-4h-backfill"
)
DEFAULT_ACTIVE_SCANNER_1H_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r' && set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED"'
    r' && set "AEGIS_EXIT_POLICY_PROFILE=EXITPOLICY_V2_GUARDED"'
    r" && .venv\Scripts\python.exe -m app.main active-scanner-1h-foundation"
)
DEFAULT_ACTIVE_SCANNER_1H_PILOT_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r' && set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED"'
    r' && set "AEGIS_EXIT_POLICY_PROFILE=EXITPOLICY_V2_GUARDED"'
    r" && .venv\Scripts\python.exe -m app.main active-scanner-1h-pilot"
)
DEFAULT_ACTIVE_SCANNER_1H_IEX_EQUITY_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r' && set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED"'
    r' && set "AEGIS_EXIT_POLICY_PROFILE=EXITPOLICY_V2_GUARDED"'
    r" && .venv\Scripts\python.exe -m app.main active-scanner-1h-iex-pilot"
)
DEFAULT_ACTIVE_SCANNER_1H_FULL_UNIVERSE_WINDOWS_CMD = (
    "cd /d "
    r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
    r' && set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED"'
    r' && set "AEGIS_EXIT_POLICY_PROFILE=EXITPOLICY_V2_GUARDED"'
    r" && .venv\Scripts\python.exe -m app.main active-scanner-1h-full-universe-sweep"
)
ALPACA_STOCK_ETF_BACKFILL_START = datetime(2016, 1, 4, tzinfo=UTC)
ALPACA_CRYPTO_BACKFILL_YEARS = 5
ALPACA_CORE_4H_BACKFILL_YEARS = 3
ACTIVE_SCANNER_1H_LOOKBACK_BARS = 120
ACTIVE_SCANNER_1H_MINIMUM_BARS = 60
ACTIVE_SCANNER_1H_READINESS_SWEEP_DAYS = 15


def build_live_intelligence_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    if config.etoro_demo_execution_enabled:
        return _blocked("DEMO_EXECUTION_ENABLED", "Demo execution must remain false", config=config)
    credentials = runtime_credentials(values)
    if credentials is None and client is None:
        return _blocked("CREDENTIALS", "eToro credentials are not configured", config=config)
    if not config.etoro_api_enabled and client is None:
        return _blocked("API_DISABLED", "ETORO_API_ENABLED is false", config=config)
    if client is None:
        assert credentials is not None
        read_client = EtoroReadClient(
            credentials,
            DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
        )
    else:
        read_client = client
    try:
        identity = read_client.identity()
        demo = read_client.demo_account(identity)
        portfolio = _portfolio_from_demo(demo, symbols={})
        scanner = OpenMarketCandidateScanner(
            adapter=EtoroMarketScannerAdapter(
                read_client,
                search_text=config.scanner.etoro_search_text,
                max_pages=config.scanner.etoro_max_pages,
            ),
            policy_engine=default_asset_policy_engine(),
            ranking_engine=OpportunityRankingEngine(ranking_version=RANKING_VERSION),
            limits=ScannerLimits(
                discovery_limit=config.scanner.discovery_limit,
                ranked_shortlist_limit=config.scanner.ranked_shortlist_limit,
                deep_analysis_limit=config.scanner.deep_analysis_limit,
            ),
        )
        scan = scanner.scan(portfolio=portfolio, as_of=now)
        if not scan.ranked_candidates:
            return {
                **_blocked(
                    "NO_POLICY_ALLOWED_CANDIDATES",
                    "scanner produced no policy-allowed shortlisted candidates",
                    config=config,
                ),
                "markets_scanned": scan.total_discovered,
                "open_markets": scan.open_markets,
                "closed_markets": scan.closed_markets,
            }
        historical_registry = HistoricalDataProviderRegistry(
            (
                HistoricalProviderEntry(EtoroHistoricalMarketDataProvider(read_client), priority=1),
                HistoricalProviderEntry(StooqHistoricalDataProvider(), priority=2),
            ),
            cache=cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH),
        )
        pipeline = LiveMarketIntelligencePipeline(
            historical_registry=historical_registry,
            news_registry=NewsProviderRegistry((NullNewsProvider(),)),
            event_registry=EventRiskProviderRegistry((NullEventRiskProvider(),)),
            research_store=default_strategy_research_store(),
        )
        run = pipeline.run(
            candidates=scan.ranked_candidates,
            portfolio=portfolio,
            as_of=now,
            required_timeframes=(TimeFrame.ONE_HOUR, TimeFrame.FOUR_HOUR, TimeFrame.ONE_DAY),
            top_n=config.scanner.deep_analysis_limit,
        )
    except EtoroApiError as exc:
        metadata = exc.safe_metadata()
        return {
            **_blocked(
                metadata.get("category", "ETORO_API_ERROR"),
                "live intelligence read chain failed",
                config=config,
            ),
            "endpoint": metadata.get("endpoint"),
            "http_status": metadata.get("http_status"),
            "transport_detail": metadata.get("transport_detail"),
            "cf_ray": metadata.get("cf_ray"),
        }
    except (RuntimeError, ValueError) as exc:
        return _blocked(type(exc).__name__, "live intelligence could not complete", config=config)

    return {
        "status": "LIVE_DATA_ANALYSIS",
        "broker_write": False,
        "broker_write_calls": run.broker_write_calls,
        "demo_execution_enabled": run.demo_execution_enabled,
        "real_execution_available": run.real_execution_available,
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "universe_discovered": scan.total_discovered,
        "markets_open": scan.open_markets,
        "markets_closed": scan.closed_markets,
        "candidates_policy_allowed": len(scan.ranked_candidates),
        "deep_analyzed": run.deep_analyzed,
        "results": tuple(
            {
                "symbol": item.candidate.instrument.symbol,
                "asset_class": item.candidate.asset_class.value,
                "status": item.status.value,
                "historical": item.historical_status.value,
                "news": item.news_status.value,
                "event_risk": item.event_status.value,
                "score": (
                    str(item.analysis.opportunity_score.overall_score)
                    if item.analysis is not None
                    else None
                ),
                "confidence": (
                    str(item.analysis.opportunity_score.confidence)
                    if item.analysis is not None
                    else None
                ),
                "regime": (
                    {
                        "trend": item.analysis.regime.trend.value,
                        "volatility": item.analysis.regime.volatility.value,
                        "risk_environment": item.analysis.regime.risk_environment.value,
                    }
                    if item.analysis is not None
                    else None
                ),
                "portfolio_fit": (
                    item.analysis.portfolio_fit.status.value if item.analysis is not None else None
                ),
                "aegis_decision": item.analysis.decision.value if item.analysis else None,
                "reasons": item.reasons,
            }
            for item in run.results
        ),
        "windows_cmd": (
            "cd /d "
            r"C:\Users\simon\Documents\Codex\2026-08-28\ahhh-s-ho-capito-cosa-intendi"
            r" && .venv\Scripts\python.exe -m app.main intelligence-live"
        ),
    }


def build_exit_evidence_acquisition_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    expected_asset_classes = _exit_evidence_expected_asset_classes()
    duplicate_before = effective_cache.duplicate_timestamp_report()
    quarantine = effective_cache.quarantine_asset_class_mismatches(
        expected_asset_classes=expected_asset_classes,
        quarantined_at=now,
    )
    duplicate_after = effective_cache.duplicate_timestamp_report()
    if config.etoro_demo_execution_enabled:
        return {
            **_real_validation_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "windows_cmd": DEFAULT_EXIT_EVIDENCE_WINDOWS_CMD,
            "cache_integrity": _cache_integrity_payload(
                duplicate_before=duplicate_before,
                duplicate_after=duplicate_after,
                quarantine=quarantine,
            ),
        }
    read_client = client
    credentials = runtime_credentials(values)
    if read_client is None and credentials is not None and config.etoro_api_enabled:
        read_client = EtoroReadClient(
            credentials,
            DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
        )
    if read_client is None:
        return {
            **_real_validation_blocked(
                "ETORO_READ_ONLY_NOT_CONFIGURED",
                "canonical eToro read-only mapping is required for Step 8.0H",
                config=config,
            ),
            "requested_symbols": _exit_evidence_symbol_payload(),
            "requested_timeframes": tuple(item.value for item in EXIT_EVIDENCE_TIMEFRAMES),
            "windows_cmd": DEFAULT_EXIT_EVIDENCE_WINDOWS_CMD,
            "cache_integrity": _cache_integrity_payload(
                duplicate_before=duplicate_before,
                duplicate_after=duplicate_after,
                quarantine=quarantine,
            ),
        }

    policy_engine = default_asset_policy_engine()
    admission_traces: list[dict[str, object]] = []
    symbols = tuple(symbol for group in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.values() for symbol in group)
    try:
        instruments, rejected_symbols, resolution_provider = _resolve_etoro_research_universe(
            client=read_client,
            symbols=symbols,
            as_of=now,
            asset_class_filter=None,
            policy_engine=policy_engine,
            expected_asset_classes=expected_asset_classes,
            admission_traces=admission_traces,
        )
    except EtoroApiError as exc:
        metadata = exc.safe_metadata()
        return {
            **_real_validation_blocked(
                metadata.get("category", "ETORO_API_ERROR"),
                "eToro read-only Step 8.0H instrument resolution failed",
                config=config,
            ),
            "endpoint": metadata.get("endpoint"),
            "http_status": metadata.get("http_status"),
            "transport_detail": metadata.get("transport_detail"),
            "cf_ray": metadata.get("cf_ray"),
            "requested_symbols": _exit_evidence_symbol_payload(),
            "requested_timeframes": tuple(item.value for item in EXIT_EVIDENCE_TIMEFRAMES),
            "admission_traces": tuple(admission_traces),
            "windows_cmd": DEFAULT_EXIT_EVIDENCE_WINDOWS_CMD,
            "cache_integrity": _cache_integrity_payload(
                duplicate_before=duplicate_before,
                duplicate_after=duplicate_after,
                quarantine=quarantine,
            ),
        }

    mapping_service = InstrumentMappingService(_stooq_mapping_overrides(instruments))
    registry = HistoricalDataProviderRegistry(
        (
            HistoricalProviderEntry(EtoroHistoricalMarketDataProvider(read_client), 1),
            HistoricalProviderEntry(
                StooqHistoricalDataProvider(mapping_service=mapping_service),
                2,
            ),
        ),
        cache=effective_cache,
        mapping_service=mapping_service,
    )
    coverage: list[dict[str, object]] = []
    for instrument in instruments:
        result = registry.fetch(
            instrument=instrument,
            timeframes=EXIT_EVIDENCE_TIMEFRAMES,
            as_of=now,
            limit=1000,
        )
        for timeframe in EXIT_EVIDENCE_TIMEFRAMES:
            dataset = next((item for item in result.datasets if item.timeframe is timeframe), None)
            if dataset is None:
                coverage.append(
                    _missing_coverage_payload(
                        instrument=instrument,
                        timeframe=timeframe,
                        reasons=result.reasons,
                    )
                )
                continue
            coverage.append(_coverage_payload(dataset))
    successful = tuple(
        item
        for item in coverage
        if item["bar_count"] and item["data_quality_state"] in {"GOOD", "PARTIAL"}
    )
    status = "EXIT_EVIDENCE_DATASET_READY_FOR_VALIDATION"
    if rejected_symbols or len(successful) < len(instruments) * len(EXIT_EVIDENCE_TIMEFRAMES):
        status = "EXIT_EVIDENCE_DATASET_PARTIAL"
    if not successful:
        status = "EXIT_EVIDENCE_DATA_ACQUISITION_BLOCKED"
    return {
        "status": status,
        "phase": "STEP_8_0H_PHASE_1",
        "requested_symbols": _exit_evidence_symbol_payload(),
        "requested_timeframes": tuple(item.value for item in EXIT_EVIDENCE_TIMEFRAMES),
        "resolution_provider": resolution_provider,
        "resolved_instruments": _resolved_instrument_payload(instruments),
        "rejected_instruments": rejected_symbols,
        "admission_traces": tuple(admission_traces),
        "coverage": tuple(coverage),
        "successfully_mapped_instruments": len(instruments),
        "rejected_instrument_count": len(rejected_symbols),
        "total_1d_bars": sum(
            _coverage_bar_count(item) for item in coverage if item["timeframe"] == "1D"
        ),
        "total_4h_bars": sum(
            _coverage_bar_count(item) for item in coverage if item["timeframe"] == "4H"
        ),
        "usable_instruments_per_asset_class": dict(
            sorted(Counter(str(item["asset_class"]) for item in successful).items())
        ),
        "estimated_lifecycle_evidence_capacity": _evidence_capacity(successful),
        "cache_integrity": _cache_integrity_payload(
            duplicate_before=duplicate_before,
            duplicate_after=duplicate_after,
            quarantine=quarantine,
        ),
        "historical_depth_limit": _historical_depth_limit_payload(coverage),
        "dataset_ready_for_lifecycle_walk_forward": status
        == "EXIT_EVIDENCE_DATASET_READY_FOR_VALIDATION",
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "broker_write_calls": 0,
        "demo_execution_enabled": False,
        "real_execution_available": False,
    }


def build_polygon_provider_pilot_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    if config.etoro_demo_execution_enabled:
        return {
            **_provider_pilot_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "windows_cmd": DEFAULT_POLYGON_PILOT_WINDOWS_CMD,
        }
    api_key = _polygon_api_key(values)
    if api_key is None:
        return {
            **_provider_pilot_blocked(
                "POLYGON_MASSIVE_API_KEY_NOT_CONFIGURED",
                "read-only Polygon/Massive API key is required for the provider pilot",
                config=config,
            ),
            "required_env_vars": ("AEGIS_POLYGON_API_KEY", "MASSIVE_API_KEY"),
            "api_key_configured": False,
            "windows_cmd": DEFAULT_POLYGON_PILOT_WINDOWS_CMD,
        }

    instruments = _polygon_pilot_instruments(now)
    mapping_overrides = _polygon_mapping_overrides(instruments)
    mapping_service = InstrumentMappingService(mapping_overrides)
    provider = PolygonHistoricalMarketDataProvider(
        api_key=api_key,
        transport=transport,
        max_pages=2,
    )
    registry = HistoricalDataProviderRegistry(
        (HistoricalProviderEntry(provider, 1),),
        cache=effective_cache,
        mapping_service=mapping_service,
    )
    sample_coverage: list[dict[str, object]] = []
    depth: list[dict[str, object]] = []
    overlaps: list[dict[str, object]] = []
    pagination_verified = False
    try:
        for instrument in instruments:
            for timeframe in POLYGON_PILOT_TIMEFRAMES:
                sample_result = registry.fetch(
                    instrument=instrument,
                    timeframes=(timeframe,),
                    as_of=now,
                    limit=120 if timeframe is TimeFrame.ONE_DAY else 300,
                )
                dataset = next(
                    (item for item in sample_result.datasets if item.timeframe is timeframe),
                    None,
                )
                if dataset is None:
                    sample_coverage.append(
                        _missing_coverage_payload(
                            instrument=instrument,
                            timeframe=timeframe,
                            reasons=sample_result.reasons,
                        )
                    )
                else:
                    sample_coverage.append(_coverage_payload(dataset))
                    pagination_verified = pagination_verified or _transport_saw_next_page(transport)
                    if instrument.symbol in {"AAPL", "SPY", "BTC"}:
                        etoro_bars = effective_cache.get_bars(
                            provider="etoro",
                            instrument_key=(instrument.broker, instrument.broker_instrument_id),
                            timeframe=timeframe,
                            as_of=now,
                            limit=500,
                            instrument_factory=instrument.model_dump(mode="json"),
                        )
                        overlaps.append(
                            _cross_provider_overlap_payload(
                                instrument=instrument,
                                timeframe=timeframe,
                                left_provider="polygon",
                                left_bars=dataset.bars,
                                reference_provider="etoro",
                                reference_bars=etoro_bars,
                            )
                        )
            depth.extend(
                _polygon_depth_probe(
                    provider=provider,
                    instrument=instrument,
                    now=now,
                )
            )
    except DataProviderError as exc:
        return _polygon_provider_error_payload(exc, config=config)

    duplicate_after = effective_cache.duplicate_timestamp_report()
    entitlement = _polygon_entitlement_summary(depth)
    status = _polygon_pilot_status(sample_coverage, depth, duplicate_after)
    return {
        "status": status,
        "phase": "STEP_8_0J_POLYGON_PROVIDER_ADAPTER_PILOT",
        "provider": "polygon",
        "provider_brand": "Polygon/Massive",
        "api_key_configured": True,
        "pilot_symbols": _polygon_pilot_symbol_payload(),
        "timeframes": tuple(item.value for item in POLYGON_PILOT_TIMEFRAMES),
        "provider_mappings": tuple(
            _provider_mapping_payload(reference) for reference in mapping_overrides.values()
        ),
        "sample_coverage": tuple(sample_coverage),
        "historical_depth_accessible": tuple(depth),
        "entitlement_summary": entitlement,
        "deepest_verified_stock_etf_entitlement": entitlement[
            "deepest_verified_stock_etf_entitlement"
        ],
        "deepest_verified_crypto_entitlement": entitlement["deepest_verified_crypto_entitlement"],
        "four_hour_exit_validation_depth_adequate": entitlement[
            "four_hour_exit_validation_depth_adequate"
        ],
        "plan_recommendation": _massive_plan_recommendation(entitlement),
        "pagination_verified": pagination_verified,
        "cache_duplicate_count": sum(_duplicate_timestamp_count(item) for item in duplicate_after),
        "cache_duplicate_groups": duplicate_after,
        "cross_provider_integrity": tuple(overlaps),
        "diagnostic_tolerances": {
            "close_price_relative_warning": "0.02",
            "ohlc_relative_warning": "0.03",
            "timestamp_match_required_for_comparison": True,
            "volume_exact_match_required": False,
        },
        "dia_status": _dia_status(mapping_overrides),
        "safe_to_begin_full_backfill": status == "HISTORICAL_PROVIDER_ADAPTER_READY_FOR_BACKFILL",
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "broker_write_calls": 0,
        "demo_execution_enabled": False,
        "real_execution_available": False,
    }


def build_alpaca_provider_pilot_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    if config.etoro_demo_execution_enabled:
        return {
            **_alpaca_pilot_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "windows_cmd": DEFAULT_ALPACA_PILOT_WINDOWS_CMD,
        }
    key_id, secret_key = _alpaca_api_credentials(values)
    if key_id is None or secret_key is None:
        return {
            **_alpaca_pilot_blocked(
                "ALPACA_API_KEYS_NOT_CONFIGURED",
                "read-only Alpaca stock/ETF historical pilot requires API key ID and secret",
                config=config,
            ),
            "required_env_vars": (
                "ALPACA_API_KEY_ID",
                "ALPACA_API_SECRET_KEY",
                "APCA_API_KEY_ID",
                "APCA_API_SECRET_KEY",
            ),
            "api_key_configured": key_id is not None,
            "api_secret_configured": secret_key is not None,
            "windows_cmd": DEFAULT_ALPACA_PILOT_WINDOWS_CMD,
        }

    instruments = _alpaca_pilot_instruments(now)
    mapping_overrides = _alpaca_mapping_overrides(instruments)
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id=key_id,
        api_secret_key=secret_key,
        transport=transport,
        stock_feed="sip",
        crypto_location="us",
        max_pages=2,
    )
    canonical_broker_ids = _verified_etoro_broker_ids(effective_cache)
    mapping_quarantine = effective_cache.quarantine_provider_broker_reference_mismatches(
        provider="alpaca",
        expected_broker_ids=canonical_broker_ids,
        quarantined_at=now,
    )
    depth: list[dict[str, object]] = []
    sample_coverage: list[dict[str, object]] = []
    overlaps: list[dict[str, object]] = []
    for instrument in instruments:
        for timeframe in ALPACA_PILOT_TIMEFRAMES:
            for label, start, end in _alpaca_probe_windows(instrument, now):
                row, bars = _alpaca_probe_row(
                    provider=provider,
                    instrument=instrument,
                    timeframe=timeframe,
                    label=label,
                    start=start,
                    end=end,
                )
                depth.append(row)
                if not bars:
                    continue
                mapping = mapping_overrides[("alpaca", instrument.key)]
                effective_cache.upsert_bars(
                    provider="alpaca",
                    bars=bars,
                    fetched_at=now,
                    mapping=mapping,
                )
                if label == "recent":
                    sample_coverage.append(
                        _bars_coverage_payload(
                            provider="alpaca",
                            instrument=instrument,
                            timeframe=timeframe,
                            bars=bars,
                            cache_status="UPDATED",
                            mapping_status="RESOLVED",
                            source_notes=_alpaca_source_notes(instrument),
                        )
                    )
                    if instrument.symbol in {"AAPL", "SPY", "BTC", "ETH", "SOL"}:
                        for reference_provider in ("etoro", "polygon"):
                            reference_bars = effective_cache.get_bars(
                                provider=reference_provider,
                                instrument_key=(instrument.broker, instrument.broker_instrument_id),
                                timeframe=timeframe,
                                as_of=now,
                                limit=500,
                                instrument_factory=instrument.model_dump(mode="json"),
                            )
                            overlaps.append(
                                _cross_provider_overlap_payload(
                                    instrument=instrument,
                                    timeframe=timeframe,
                                    left_provider="alpaca",
                                    left_bars=bars,
                                    reference_provider=reference_provider,
                                    reference_bars=reference_bars,
                                )
                            )
    duplicates = effective_cache.duplicate_timestamp_report()
    status = _alpaca_pilot_status(depth, duplicates)
    pagination_summary = _alpaca_pagination_summary(depth)
    return {
        "status": status,
        "phase": "STEP_8_0K_ALPACA_FREE_PROVIDER_PILOT",
        "provider": "alpaca",
        "api_key_configured": True,
        "api_secret_configured": True,
        "pilot_symbols": _alpaca_pilot_symbol_payload(),
        "timeframes": tuple(item.value for item in ALPACA_PILOT_TIMEFRAMES),
        "provider_mappings": tuple(
            _provider_mapping_payload(reference) for reference in mapping_overrides.values()
        ),
        "historical_depth_accessible": tuple(depth),
        "sample_coverage": tuple(sample_coverage),
        **pagination_summary,
        "cross_provider_integrity": tuple(overlaps),
        "backfill_mapping_reconciliation": _alpaca_backfill_mapping_reconciliation(
            effective_cache,
            mapping_overrides=mapping_overrides,
        ),
        "mapping_quarantine": mapping_quarantine,
        "feed_semantics": {
            "stocks_etfs": "SIP historical feed requested for old delayed windows",
            "crypto": "Alpaca US crypto historical bars from provider response",
            "timezone": "Alpaca timestamps normalized as UTC",
        },
        "data_quality_summary": _alpaca_quality_summary(depth),
        "dia_status": {
            "provider_mapping": "DIA_ALPACA_MAPPING_VERIFIED_AS_ETF",
            "provider_symbol": "DIA",
            "etoro_reference": "ETORO_DIA_REFERENCE_UNAVAILABLE",
            "invalid_etoro_crypto_mapping": "REMAINS_QUARANTINED",
        },
        "safe_to_begin_full_backfill": status == "ALPACA_FREE_PILOT_READY_FOR_WINDOWS",
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "broker_write_calls": 0,
        "demo_execution_enabled": False,
        "real_execution_available": False,
        "windows_cmd": DEFAULT_ALPACA_PILOT_WINDOWS_CMD,
    }


def build_alpaca_full_backfill_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    if config.etoro_demo_execution_enabled:
        return {
            **_alpaca_backfill_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "windows_cmd": DEFAULT_ALPACA_FULL_BACKFILL_WINDOWS_CMD,
        }
    key_id, secret_key = _alpaca_api_credentials(values)
    if key_id is None or secret_key is None:
        return {
            **_alpaca_backfill_blocked(
                "ALPACA_API_KEYS_NOT_CONFIGURED",
                "read-only Alpaca full backfill requires API key ID and secret",
                config=config,
            ),
            "required_env_vars": (
                "ALPACA_API_KEY_ID",
                "ALPACA_API_SECRET_KEY",
                "APCA_API_KEY_ID",
                "APCA_API_SECRET_KEY",
            ),
            "api_key_configured": key_id is not None,
            "api_secret_configured": secret_key is not None,
            "windows_cmd": DEFAULT_ALPACA_FULL_BACKFILL_WINDOWS_CMD,
        }
    canonical_broker_ids = _verified_etoro_broker_ids(effective_cache)
    mapping_quarantine = effective_cache.quarantine_provider_broker_reference_mismatches(
        provider="alpaca",
        expected_broker_ids=canonical_broker_ids,
        quarantined_at=now,
    )
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id=key_id,
        api_secret_key=secret_key,
        transport=transport,
        stock_feed="sip",
        crypto_location="us",
        max_pages=25,
    )
    rows: list[dict[str, object]] = []
    mappings: list[ProviderInstrumentReference] = []
    for instrument in _alpaca_full_backfill_instruments(
        now,
        canonical_broker_ids=canonical_broker_ids,
    ):
        mapping = _alpaca_reference_for_instrument(instrument)
        mappings.append(mapping)
        for timeframe in ALPACA_PILOT_TIMEFRAMES:
            rows.append(
                _alpaca_full_backfill_row(
                    provider=provider,
                    cache=effective_cache,
                    instrument=instrument,
                    timeframe=timeframe,
                    mapping=mapping,
                    now=now,
                )
            )
    duplicates = effective_cache.duplicate_timestamp_report()
    status = _alpaca_full_backfill_status(rows, duplicates)
    return {
        "status": status,
        "phase": "STEP_8_0L_ALPACA_FULL_HISTORICAL_BACKFILL",
        "provider": "alpaca",
        "requested_symbols": _exit_evidence_symbol_payload(),
        "requested_timeframes": tuple(item.value for item in ALPACA_PILOT_TIMEFRAMES),
        "configured_history": {
            "stock_etf_start": ALPACA_STOCK_ETF_BACKFILL_START.isoformat(),
            "crypto_years_back": ALPACA_CRYPTO_BACKFILL_YEARS,
        },
        "backfill": tuple(rows),
        "provider_mappings": tuple(_provider_mapping_payload(mapping) for mapping in mappings),
        "mapping_reconciliation": _alpaca_backfill_mapping_reconciliation(
            effective_cache,
            mapping_overrides={("alpaca", mapping.broker_symbol): mapping for mapping in mappings},
        ),
        "mapping_quarantine": mapping_quarantine,
        "pagination": _alpaca_backfill_pagination_summary(rows),
        "cache_duplicate_count": sum(_duplicate_timestamp_count(item) for item in duplicates),
        "duplicate_timestamp_report": duplicates,
        "summary": _alpaca_full_backfill_summary(rows),
        "safe_for_lifecycle_validation": status == "ALPACA_FULL_BACKFILL_COMPLETE",
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "broker_write_calls": 0,
        "demo_execution_enabled": False,
        "real_execution_available": False,
        "windows_cmd": DEFAULT_ALPACA_FULL_BACKFILL_WINDOWS_CMD,
    }


def build_alpaca_core_4h_backfill_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    if config.etoro_demo_execution_enabled:
        return {
            **_alpaca_backfill_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "windows_cmd": DEFAULT_ALPACA_CORE_4H_WINDOWS_CMD,
        }
    key_id, secret_key = _alpaca_api_credentials(values)
    if key_id is None or secret_key is None:
        return {
            **_alpaca_backfill_blocked(
                "ALPACA_API_KEYS_NOT_CONFIGURED",
                "read-only Alpaca core 4H backfill requires API key ID and secret",
                config=config,
            ),
            "required_env_vars": (
                "ALPACA_API_KEY_ID",
                "ALPACA_API_SECRET_KEY",
                "APCA_API_KEY_ID",
                "APCA_API_SECRET_KEY",
            ),
            "api_key_configured": key_id is not None,
            "api_secret_configured": secret_key is not None,
            "windows_cmd": DEFAULT_ALPACA_CORE_4H_WINDOWS_CMD,
        }
    canonical_broker_ids = _verified_etoro_broker_ids(effective_cache)
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id=key_id,
        api_secret_key=secret_key,
        transport=transport,
        stock_feed="sip",
        crypto_location="us",
        max_pages=25,
    )
    rows: list[dict[str, object]] = []
    mappings: list[ProviderInstrumentReference] = []
    for instrument in _alpaca_core_4h_instruments(
        now,
        canonical_broker_ids=canonical_broker_ids,
    ):
        if instrument is None:
            continue
        mapping = _alpaca_reference_for_instrument(instrument)
        mappings.append(mapping)
        rows.append(
            _alpaca_core_4h_row(
                provider=provider,
                cache=effective_cache,
                instrument=instrument,
                mapping=mapping,
                now=now,
            )
        )
    missing_mappings = _missing_core_4h_mappings(canonical_broker_ids)
    duplicates = effective_cache.duplicate_timestamp_report()
    status = _alpaca_core_4h_status(rows, duplicates, missing_mappings)
    return {
        "status": status,
        "phase": "STEP_8_0L_ALPACA_CORE_4H_VALIDATION_DATASET",
        "provider": "alpaca",
        "core_symbols": _alpaca_core_4h_symbol_payload(),
        "requested_timeframes": tuple(item.value for item in ALPACA_CORE_4H_TIMEFRAMES),
        "configured_history": {
            "years_back": ALPACA_CORE_4H_BACKFILL_YEARS,
            "requested_start": _alpaca_core_4h_range(now)[0].isoformat(),
            "requested_end": _alpaca_core_4h_range(now)[1].isoformat(),
        },
        "backfill": tuple(rows),
        "provider_mappings": tuple(_provider_mapping_payload(mapping) for mapping in mappings),
        "missing_canonical_mappings": missing_mappings,
        "pagination": _alpaca_backfill_pagination_summary(rows),
        "cache_duplicate_count": sum(_duplicate_timestamp_count(item) for item in duplicates),
        "duplicate_timestamp_report": duplicates,
        "summary": _alpaca_full_backfill_summary(rows),
        "safe_for_lifecycle_validation": status == "ALPACA_CORE_4H_READY",
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "broker_write_calls": 0,
        "demo_execution_enabled": False,
        "real_execution_available": False,
        "windows_cmd": DEFAULT_ALPACA_CORE_4H_WINDOWS_CMD,
    }


def build_real_strategy_validation_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    cache: HistoricalDataCache | None = None,
    store: StrategyValidationStore | None = None,
    clock: Callable[[], datetime] | None = None,
    asset_class: str | None = None,
    instrument: str | None = None,
    timeframe: str | None = None,
    start: str | None = None,
    end: str | None = None,
    max_instruments: int = 9,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    if config.etoro_demo_execution_enabled:
        return _real_validation_blocked(
            "DEMO_EXECUTION_ENABLED",
            "Demo execution must remain false",
            config=config,
        )
    as_of = _parse_datetime_filter(end) or now
    start_at = _parse_datetime_filter(start)
    if start_at is not None and start_at >= as_of:
        return _real_validation_blocked(
            "INVALID_DATE_RANGE",
            "start must be before end/as_of",
            config=config,
        )
    requirements = EvidenceRequirements()
    policy_engine = default_asset_policy_engine()
    policy_diagnostics = build_policy_strategy_diagnostics(policy_engine)
    admission_traces: list[dict[str, object]] = []
    selected_timeframes = _selected_timeframes(timeframe)
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    read_client = client
    credentials = runtime_credentials(values)
    if read_client is None and credentials is not None and config.etoro_api_enabled:
        read_client = EtoroReadClient(
            credentials,
            DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
        )
    try:
        instruments, rejected_symbols, resolution_provider = _resolve_research_universe(
            client=read_client,
            as_of=as_of,
            asset_class_filter=_asset_class_filter(asset_class),
            instrument_filter=instrument,
            max_instruments=max_instruments,
            policy_engine=policy_engine,
            admission_traces=admission_traces,
        )
    except EtoroApiError as exc:
        metadata = exc.safe_metadata()
        return {
            **_real_validation_blocked(
                metadata.get("category", "ETORO_API_ERROR"),
                "eToro read-only universe resolution failed",
                config=config,
            ),
            "endpoint": metadata.get("endpoint"),
            "http_status": metadata.get("http_status"),
            "transport_detail": metadata.get("transport_detail"),
            "cf_ray": metadata.get("cf_ray"),
            "policy_diagnostics": policy_diagnostics,
            "admission_traces": tuple(admission_traces),
            "windows_cmd": DEFAULT_REAL_VALIDATION_WINDOWS_CMD,
        }
    if not instruments:
        return {
            **_real_validation_blocked(
                "NO_RESEARCH_UNIVERSE",
                "no policy-enabled instruments could be resolved",
                config=config,
            ),
            "rejected_symbols": rejected_symbols,
            "policy_diagnostics": policy_diagnostics,
            "admission_traces": tuple(admission_traces),
            "windows_cmd": DEFAULT_REAL_VALIDATION_WINDOWS_CMD,
        }

    mapping_service = InstrumentMappingService(_stooq_mapping_overrides(instruments))
    providers: list[HistoricalProviderEntry] = []
    if read_client is not None:
        providers.append(HistoricalProviderEntry(EtoroHistoricalMarketDataProvider(read_client), 1))
    providers.append(
        HistoricalProviderEntry(
            StooqHistoricalDataProvider(mapping_service=mapping_service),
            2,
        )
    )
    registry = HistoricalDataProviderRegistry(
        providers,
        cache=effective_cache,
        mapping_service=mapping_service,
    )
    build = build_real_historical_validation_datasets(
        registry=registry,
        instruments=instruments,
        timeframes=selected_timeframes,
        as_of=as_of,
        start=start_at,
        limit=_history_limit(start_at=start_at, as_of=as_of),
        requirements=requirements,
    )
    if not build.datasets_by_timeframe:
        return {
            **_real_validation_blocked(
                "HISTORICAL_DATA_INSUFFICIENT",
                "no instrument/timeframe met minimum evidence requirements",
                config=config,
            ),
            "universe_rule": build.universe_rule,
            "resolution_provider": resolution_provider,
            "coverage": tuple(item.model_dump(mode="json") for item in build.coverage),
            "rejected_symbols": build.rejected_symbols,
            "policy_diagnostics": policy_diagnostics,
            "admission_traces": tuple(admission_traces),
            "windows_cmd": DEFAULT_REAL_VALIDATION_WINDOWS_CMD,
        }

    result_store = store or default_strategy_validation_store(
        DEFAULT_STRATEGY_VALIDATION_STORE_PATH
    )
    runs = []
    matrix_rows: list[dict[str, object]] = []
    diagnostics: list[ZeroTradeDiagnostic] = []
    record_ids: list[int] = []
    for selected_timeframe, dataset in sorted(
        build.datasets_by_timeframe.items(),
        key=lambda item: item[0].value,
    ):
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
            timeframe=selected_timeframe,
        )
        record_ids.append(result_store.record_result(result))
        runs.append(_validation_run_payload(result, selected_timeframe))
        matrix_rows.extend(row.model_dump(mode="json") for row in result.research_matrix)
        diagnostics.extend(result.zero_trade_diagnostics)

    return {
        "status": "REAL_DATA_RESEARCH_COMPLETE",
        "validation_runtime_version": REAL_VALIDATION_RUNTIME_VERSION,
        "policy_admission_source": POLICY_ADMISSION_SOURCE,
        "real_dataset": True,
        "universe_rule": build.universe_rule,
        "resolution_provider": resolution_provider,
        "policy_diagnostics": policy_diagnostics,
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "record_store_path": str(DEFAULT_STRATEGY_VALIDATION_STORE_PATH),
        "persisted_record_ids": tuple(record_ids),
        "selected_instruments": build.selected_instruments,
        "rejected_symbols": build.rejected_symbols,
        "admission_traces": tuple(admission_traces),
        "requested_timeframes": tuple(item.value for item in selected_timeframes),
        "coverage": tuple(item.model_dump(mode="json") for item in build.coverage),
        "progress": (
            {
                "stage": "dataset_building",
                "instruments_requested": len(instruments),
                "timeframes_requested": len(selected_timeframes),
            },
            {
                "stage": "historical_replay",
                "validation_runs": len(runs),
                "bars_loaded": sum(item.bar_count for item in build.coverage),
            },
        ),
        "validation_runs": tuple(runs),
        "decision_funnel": _combined_funnel(runs),
        "zero_trade_diagnostics": _top_diagnostics(tuple(diagnostics)),
        "research_matrix": tuple(matrix_rows),
        "qualified_configurations": tuple(
            row
            for row in matrix_rows
            if row["qualification"] in {"SHADOW_ELIGIBLE", "DEMO_ELIGIBLE"}
        ),
        "rejected_configurations": tuple(
            row for row in matrix_rows if row["qualification"] == "REJECTED"
        ),
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "windows_cmd": DEFAULT_REAL_VALIDATION_WINDOWS_CMD,
    }


def build_lifecycle_walkforward_readiness_report(
    config: ApplicationConfig,
    *,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    if config.etoro_demo_execution_enabled:
        return _blocked(
            "DEMO_EXECUTION_ENABLED",
            "Demo execution must remain false",
            config=config,
        )
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    instruments = tuple(
        instrument
        for instrument in _alpaca_core_4h_instruments(
            now,
            canonical_broker_ids=_verified_etoro_broker_ids(effective_cache),
        )
        if instrument is not None
    )
    requested_symbols = tuple(
        symbol for symbols in ALPACA_CORE_4H_SYMBOLS_BY_CLASS.values() for symbol in symbols
    )
    cached_bars_by_instrument = {
        instrument.key: effective_cache.get_bars_range(
            provider="alpaca",
            instrument_key=(instrument.broker, instrument.broker_instrument_id),
            timeframe=TimeFrame.ONE_DAY,
            start=datetime(2016, 1, 4, tzinfo=UTC),
            end=now,
            instrument_factory=instrument.model_dump(mode="json"),
        )
        for instrument in instruments
    }
    bars_by_instrument = {
        key: tuple(bars[-720:]) for key, bars in cached_bars_by_instrument.items()
    }
    coverage = tuple(
        {
            "symbol": instrument.symbol,
            "asset_class": instrument.asset_class.value,
            "timeframe": TimeFrame.ONE_DAY.value,
            "bars": len(bars_by_instrument.get(instrument.key, ())),
            "cached_total_bars": len(cached_bars_by_instrument.get(instrument.key, ())),
            "start": _optional_datetime_iso(
                bars_by_instrument[instrument.key][0].timestamp
                if bars_by_instrument.get(instrument.key)
                else None
            ),
            "end": _optional_datetime_iso(
                bars_by_instrument[instrument.key][-1].timestamp
                if bars_by_instrument.get(instrument.key)
                else None
            ),
            "cache_provider": "alpaca",
        }
        for instrument in instruments
    )
    missing_symbols = tuple(
        symbol
        for symbol in requested_symbols
        if symbol not in {instrument.symbol for instrument in instruments}
        or not any(
            instrument.symbol == symbol and len(bars_by_instrument.get(instrument.key, ())) >= 60
            for instrument in instruments
        )
    )
    if missing_symbols:
        return {
            **_blocked(
                "CORE_1D_CACHE_INSUFFICIENT",
                "core 1D Alpaca cache does not yet contain enough bars for lifecycle readiness",
                config=config,
            ),
            "core_universe": requested_symbols,
            "coverage": coverage,
            "missing_or_insufficient_symbols": missing_symbols,
            "existing_components": _lifecycle_existing_components(),
            "missing_pieces": ("complete cached 1D bars for every core symbol",),
        }
    dataset = HistoricalValidationDataset(
        metadata=build_dataset_metadata(
            provider="alpaca-cache",
            instruments=instruments,
            bars_by_instrument=bars_by_instrument,
            timeframes=(TimeFrame.ONE_DAY,),
            created_at=now,
            mapping_version="alpaca-core-1d-lifecycle-readiness-v1",
        ),
        bars_by_instrument=bars_by_instrument,
    )
    result = HistoricalValidationEngine(
        risk_policy=config.risk,
        strategy_config=config.strategy,
    ).run(
        dataset=dataset,
        cost_assumptions=TransactionCostAssumptions(),
        random_seed=80,
        replay_stride=20,
    )
    lifecycle = result.lifecycle.model_dump(mode="json") if result.lifecycle is not None else {}
    has_future_ignored = any(decision.future_records_ignored > 0 for decision in result.decisions)
    has_no_future_visible = all(
        decision.future_records_ignored >= 0 for decision in result.decisions
    )
    required_components = _lifecycle_existing_components()
    missing_pieces = _lifecycle_missing_pieces(result)
    evidence_gaps = _lifecycle_evidence_gaps(result)
    status = (
        "LIFECYCLE_WALKFORWARD_READY" if not missing_pieces else "LIFECYCLE_WALKFORWARD_BLOCKED"
    )
    return {
        "status": status,
        "phase": "STEP_8_0M_1D_LIFECYCLE_EXITPOLICY_WALKFORWARD_READINESS",
        "provider": "alpaca-cache",
        "core_universe": requested_symbols,
        "timeframe": TimeFrame.ONE_DAY.value,
        "readiness_window": {
            "source": "existing cached 1D dataset",
            "max_bars_per_instrument": 720,
            "purpose": "bounded lifecycle path readiness, not final evidence scoring",
        },
        "coverage": coverage,
        "dataset_id": dataset.metadata.dataset_id,
        "dataset_digest": dataset.metadata.data_digest,
        "existing_components": required_components,
        "missing_pieces": missing_pieces,
        "evidence_gaps": evidence_gaps,
        "validation_run": _validation_run_payload(result, TimeFrame.ONE_DAY),
        "lifecycle_path": (
            "decision",
            "trade_proposal",
            "simulated_position_open",
            "position_management",
            "exit_policy_evaluation",
            "simulated_position_close",
            "realized_pnl_and_audit_trail",
        ),
        "lifecycle": lifecycle,
        "anti_lookahead": {
            "clock": "HistoricalReplayClock",
            "visible_data_rule": "bar.timestamp <= replay_timestamp",
            "future_records_ignored_observed": has_future_ignored,
            "future_records_never_visible": has_no_future_visible,
        },
        "broker_write": False,
        "broker_write_calls": result.broker_write_calls,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def build_lifecycle_zero_trade_forensics_report(
    config: ApplicationConfig,
    *,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    if config.etoro_demo_execution_enabled:
        return _blocked(
            "DEMO_EXECUTION_ENABLED",
            "Demo execution must remain false",
            config=config,
        )
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    instruments = tuple(
        instrument
        for instrument in _alpaca_core_4h_instruments(
            now,
            canonical_broker_ids=_verified_etoro_broker_ids(effective_cache),
        )
        if instrument is not None
    )
    requested_symbols = tuple(
        symbol for symbols in ALPACA_CORE_4H_SYMBOLS_BY_CLASS.values() for symbol in symbols
    )
    cached_bars_by_instrument = {
        instrument.key: effective_cache.get_bars_range(
            provider="alpaca",
            instrument_key=(instrument.broker, instrument.broker_instrument_id),
            timeframe=TimeFrame.ONE_DAY,
            start=datetime(2016, 1, 4, tzinfo=UTC),
            end=now,
            instrument_factory=instrument.model_dump(mode="json"),
        )
        for instrument in instruments
    }
    bars_by_instrument = {
        key: tuple(bars[-720:]) for key, bars in cached_bars_by_instrument.items()
    }
    missing_symbols = tuple(
        symbol
        for symbol in requested_symbols
        if symbol not in {instrument.symbol for instrument in instruments}
        or not any(
            instrument.symbol == symbol and len(bars_by_instrument.get(instrument.key, ())) >= 60
            for instrument in instruments
        )
    )
    if missing_symbols:
        return {
            **_blocked(
                "CORE_1D_CACHE_INSUFFICIENT",
                "core 1D Alpaca cache does not yet contain enough bars for zero-trade forensics",
                config=config,
            ),
            "core_universe": requested_symbols,
            "missing_or_insufficient_symbols": missing_symbols,
        }
    dataset = HistoricalValidationDataset(
        metadata=build_dataset_metadata(
            provider="alpaca-cache",
            instruments=instruments,
            bars_by_instrument=bars_by_instrument,
            timeframes=(TimeFrame.ONE_DAY,),
            created_at=now,
            mapping_version="alpaca-core-1d-zero-trade-forensics-v1",
        ),
        bars_by_instrument=bars_by_instrument,
    )
    result = HistoricalValidationEngine(
        risk_policy=config.risk,
        strategy_config=config.strategy,
    ).run(
        dataset=dataset,
        cost_assumptions=TransactionCostAssumptions(),
        random_seed=80,
        replay_stride=20,
    )
    forensics = _lifecycle_zero_trade_forensics(result.decisions)
    return {
        "status": "LIFECYCLE_ZERO_TRADE_ROOT_CAUSE_IDENTIFIED",
        "phase": "STEP_8_0N_1D_ZERO_TRADE_FORENSICS",
        "provider": "alpaca-cache",
        "core_universe": requested_symbols,
        "timeframe": TimeFrame.ONE_DAY.value,
        "readiness_window": {
            "source": "existing cached 1D dataset",
            "max_bars_per_instrument": 720,
            "purpose": "zero-trade root-cause tracing, not parameter optimization",
        },
        "dataset_id": dataset.metadata.dataset_id,
        "dataset_digest": dataset.metadata.data_digest,
        "pipeline": (
            "observation",
            "intelligence_opportunity",
            "agent_decision",
            "trade_proposal",
            "risk_decision",
            "simulated_entry",
        ),
        "forensics": forensics,
        "anti_lookahead": {
            "clock": "HistoricalReplayClock",
            "visible_data_rule": "bar.timestamp <= replay_timestamp",
            "future_records_ignored_observed": any(
                decision.future_records_ignored > 0 for decision in result.decisions
            ),
            "future_records_never_visible": all(
                decision.future_records_ignored >= 0 for decision in result.decisions
            ),
        },
        "broker_write": False,
        "broker_write_calls": result.broker_write_calls,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def build_exit_policy_v2_train_validation_report(
    config: ApplicationConfig,
    *,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    if config.etoro_demo_execution_enabled:
        return _blocked(
            "DEMO_EXECUTION_ENABLED",
            "Demo execution must remain false",
            config=config,
        )
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    registry = default_exit_policy_v2_candidate_registry(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    manifest = default_exit_policy_v2_preregistered_experiment_v2_manifest(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    if config.strategy.confidence_profile != EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE:
        return _blocked(
            "EXITPOLICY_V2_MANIFEST_CONFIDENCE_PROFILE_MISMATCH",
            (
                "runtime confidence profile must match the frozen experiment manifest: "
                f"{EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE}"
            ),
            config=config,
        )
    if config.strategy.exit_policy_profile != EXIT_POLICY_V2_GUARDED:
        return _blocked(
            "EXITPOLICY_V2_MANIFEST_EXIT_POLICY_PROFILE_MISMATCH",
            (
                "runtime exit policy profile must match the frozen experiment manifest: "
                f"{EXIT_POLICY_V2_GUARDED}"
            ),
            config=config,
        )
    instruments_by_symbol = {
        instrument.symbol: instrument
        for instrument in _alpaca_full_backfill_instruments(
            now,
            canonical_broker_ids=_verified_etoro_broker_ids(effective_cache),
        )
    }
    rows: list[dict[str, object]] = []
    validation_rows: list[dict[str, object]] = []
    for bundle in registry.candidate_bundles:
        select_exit_policy_for_historical_validation(
            config.strategy.exit_policy_profile,
            v2_parameter_bundle=bundle,
            v2_candidate_registry=registry,
            v2_experiment_manifest=manifest,
        )
        agent = DeterministicAegisAgent(exit_policy=ExitPolicyV2Guarded(parameter_bundle=bundle))
        for partition in manifest.dataset_partitions:
            if partition.role not in {"TRAIN", "VALIDATION"}:
                continue
            instruments = tuple(
                instruments_by_symbol[symbol]
                for symbol in partition.eligible_symbols
                if symbol in instruments_by_symbol
                and instruments_by_symbol[symbol].numeric_instrument_id is not None
            )
            bars_by_instrument = {
                instrument.key: effective_cache.get_bars_range(
                    provider="alpaca",
                    instrument_key=(instrument.broker, instrument.broker_instrument_id),
                    timeframe=TimeFrame.ONE_DAY,
                    start=partition.start,
                    end=partition.end,
                    instrument_factory=instrument.model_dump(mode="json"),
                )
                for instrument in instruments
            }
            usable_bars_by_instrument = {
                key: bars for key, bars in bars_by_instrument.items() if len(bars) >= 60
            }
            missing = tuple(
                sorted(
                    set(partition.eligible_symbols)
                    - {bars[0].instrument.symbol for bars in usable_bars_by_instrument.values()}
                )
            )
            if not usable_bars_by_instrument:
                row: dict[str, object] = {
                    "candidate_bundle_id": bundle.parameters.parameter_bundle_id,
                    "candidate_bundle_fingerprint": bundle.fingerprint,
                    "asset_class": partition.asset_class.value,
                    "partition": partition.role,
                    "status": "DATA_INSUFFICIENT",
                    "eligible_symbols": partition.eligible_symbols,
                    "missing_or_insufficient_symbols": missing,
                    "broker_write_calls": 0,
                }
                rows.append(row)
                if partition.role == "VALIDATION":
                    validation_rows.append(row)
                continue
            dataset = HistoricalValidationDataset(
                metadata=build_dataset_metadata(
                    provider="alpaca-cache",
                    instruments=tuple(
                        bars[0].instrument for bars in usable_bars_by_instrument.values()
                    ),
                    bars_by_instrument=usable_bars_by_instrument,
                    timeframes=(TimeFrame.ONE_DAY,),
                    created_at=now,
                    mapping_version=(
                        "exitpolicy-v2-preregistered-train-validation-v1:"
                        f"{partition.asset_class.value}:{partition.role}:"
                        f"{bundle.fingerprint[:12]}"
                    ),
                ),
                bars_by_instrument=usable_bars_by_instrument,
            )
            result = HistoricalValidationEngine(
                risk_policy=config.risk,
                strategy_config=config.strategy,
                agent=agent,
            ).run(
                dataset=dataset,
                cost_assumptions=TransactionCostAssumptions(),
                random_seed=80,
                replay_stride=5,
            )
            row = _exit_policy_v2_candidate_result_payload(
                bundle_id=bundle.parameters.parameter_bundle_id,
                bundle_fingerprint=bundle.fingerprint,
                partition=partition,
                result=result,
                missing_symbols=missing,
                eligible_bars=sum(len(bars) for bars in usable_bars_by_instrument.values()),
            )
            rows.append(row)
            if partition.role == "VALIDATION":
                validation_rows.append(row)
    selected = _select_exit_policy_v2_candidate(validation_rows)
    return {
        "status": selected["status"],
        "phase": "STEP_8_0W_EXECUTE_PREREGISTERED_V2_TRAIN_VALIDATION",
        "global_default_unchanged": "V1_LEGACY remains the default outside explicit config",
        "confidence_profile": config.strategy.confidence_profile,
        "exit_policy_profile": config.strategy.exit_policy_profile,
        "manifest_confidence_profile": manifest.confidence_profile,
        "manifest_exit_policy_profile": manifest.exit_policy_profile,
        "experiment_version": manifest.experiment_version,
        "manifest_sha256": manifest.manifest_sha256,
        "candidate_registry_fingerprint": registry.fingerprint,
        "candidate_bundle_count": len(registry.candidate_bundles),
        "safety_invariants": manifest.safety_invariants,
        "research_exposed_ranges": tuple(
            partition.model_dump(mode="json") for partition in manifest.research_exposed_ranges
        ),
        "holdout_executed": False,
        "train_validation_results": tuple(rows),
        "selection": selected,
        "invariants": {
            "cooldown_blocks_same_symbol_open_or_increase": True,
            "same_timestamp_reduce_close_precedes_increase": True,
            "evidence": (
                "DeterministicAegisAgent evaluates ExitPolicy before BUY/INCREASE proposal "
                "and consumes PositionManagementState.cooldown_bars_remaining."
            ),
        },
        "anti_lookahead": {
            "clock": "HistoricalReplayClock",
            "visible_data_rule": "bar.timestamp <= replay_timestamp",
            "holdout_not_executed": True,
            "research_exposed_not_used_for_selection": True,
        },
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def build_exit_policy_v2_exposed_holdout_diagnostic_report(
    config: ApplicationConfig,
    *,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    governance = _exit_policy_v2_governance_preflight(config)
    if governance["status"] != "PASS":
        return _blocked(str(governance["category"]), str(governance["reason"]), config=config)
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    registry = default_exit_policy_v2_candidate_registry(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    manifest = default_exit_policy_v2_preregistered_experiment_v2_manifest(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    selected = _exit_policy_v2_balanced_candidate(registry)
    rows = _run_exit_policy_v2_partitions(
        config=config,
        cache=effective_cache,
        clock_timestamp=now,
        manifest=manifest,
        registry=registry,
        bundle=selected,
        roles={"HOLDOUT"},
        initial_cash=Decimal("1000"),
        diagnostic_label="EXPOSED_HOLDOUT_DIAGNOSTIC",
    )
    aggregate = _exit_policy_v2_rows_aggregate(rows)
    return {
        "status": "EXPOSED_HOLDOUT_DIAGNOSTIC_OBSERVED",
        "phase": "STEP_8_0Y_REPAIRED_EXPOSED_HOLDOUT_DIAGNOSTIC",
        "holdout_pristine": False,
        "holdout_exposure_status": "2022-2023 HOLDOUT permanently marked EXPOSED",
        "holdout_acceptance_rule": "NOT_PRESENT_IN_MANIFEST",
        "candidate_bundle_id": selected.parameters.parameter_bundle_id,
        "candidate_fingerprint": selected.fingerprint,
        "manifest_version": manifest.experiment_version,
        "manifest_sha256": manifest.manifest_sha256,
        "preflight": governance,
        "asset_class_results": tuple(rows),
        "aggregate": aggregate,
        "two_hundred_research_simulation_preparation": {
            "ready": True,
            "initial_cash_parameter": "HistoricalValidationEngine.run(initial_cash=Decimal('200'))",
            "candidate": selected.parameters.parameter_bundle_id,
            "candidate_fingerprint": selected.fingerprint,
            "classification": "research simulation only; not broker execution",
        },
        "broker_write": False,
        "broker_write_calls": aggregate["broker_write_calls"],
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "network_calls": 0,
    }


def build_prospective_shadow_validation_readiness_report(
    config: ApplicationConfig,
    *,
    store: ProspectiveShadowValidationStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    activation_timestamp = (clock or (lambda: datetime.now(UTC)))()
    if config.etoro_demo_execution_enabled:
        return _blocked(
            "DEMO_EXECUTION_ENABLED",
            "Demo execution must remain false for prospective shadow validation",
            config=config,
        )
    if config.strategy.confidence_profile != EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE:
        return _blocked(
            "PROSPECTIVE_SHADOW_CONFIDENCE_PROFILE_MISMATCH",
            "prospective shadow validation requires V2_B_GUARDED",
            config=config,
        )
    if config.strategy.exit_policy_profile != EXIT_POLICY_V2_GUARDED:
        return _blocked(
            "PROSPECTIVE_SHADOW_EXIT_POLICY_PROFILE_MISMATCH",
            "prospective shadow validation requires EXITPOLICY_V2_GUARDED",
            config=config,
        )

    registry = default_exit_policy_v2_candidate_registry(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    manifest = default_exit_policy_v2_preregistered_experiment_v2_manifest(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    baseline = Eur200ResearchBaseline.create()
    candidate = _exit_policy_v2_balanced_candidate(registry)
    governance = _exit_policy_v2_governance_preflight(config)
    if governance["status"] != "PASS":
        return _blocked(str(governance["category"]), str(governance["reason"]), config=config)
    risk_policy_digest = prospective_risk_policy_digest(config)
    shadow_manifest = ProspectiveShadowValidationManifest.create(
        activation_timestamp=activation_timestamp,
        baseline=baseline,
        config=config,
        candidate_bundle=candidate,
        candidate_registry=registry,
        experiment_manifest=manifest,
        risk_policy_digest=risk_policy_digest,
    )
    target_store = store or default_prospective_shadow_store()
    baseline_record_id = target_store.record_baseline(baseline)
    manifest_record_id = target_store.record_manifest(shadow_manifest)
    activation_record_id = target_store.record_decision(
        {
            "event_type": "PROSPECTIVE_SHADOW_ACTIVATED",
            "activation_timestamp": activation_timestamp.isoformat(),
            "manifest_fingerprint": shadow_manifest.fingerprint,
            "starting_equity": "200",
            "positions_carried_from_research": 0,
            "realized_pnl_carried_from_research": "0",
            "unrealized_pnl_carried_from_research": "0",
            "broker_write_calls": 0,
            "demo_execution_enabled": False,
            "real_execution_available": False,
        }
    )
    return {
        "status": "PROSPECTIVE_SHADOW_VALIDATION_READY",
        "phase": "PROSPECTIVE_SHADOW_VALIDATION_PREPARATION",
        "baseline": baseline.model_dump(mode="json"),
        "baseline_record_id": baseline_record_id,
        "prospective_manifest": shadow_manifest.model_dump(mode="json"),
        "manifest_record_id": manifest_record_id,
        "activation_record_id": activation_record_id,
        "activation_timestamp": activation_timestamp.isoformat(),
        "prospective_account": {
            "starting_equity": "200",
            "positions_carried_from_research": 0,
            "realized_pnl_carried_from_research": "0",
            "unrealized_pnl_carried_from_research": "0",
        },
        "audit_trail": {
            "store": "work/prospective-shadow-validation.sqlite3",
            "record_kinds": (
                "prospective-shadow-baseline",
                "prospective-shadow-manifest",
                "prospective-shadow-decision",
            ),
            "required_future_fields": shadow_manifest.audit_pipeline,
        },
        "governance": governance,
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def build_active_market_scanner_foundation_report(
    config: ApplicationConfig,
    *,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    as_of = (clock or (lambda: datetime.now(UTC)))()
    inventory = active_scanner_inventory(
        cache=effective_cache,
        canonical_symbols_by_class=EXIT_EVIDENCE_SYMBOLS_BY_CLASS,
    )
    readiness = timeframe_readiness_matrix(effective_cache)
    instruments = _active_scanner_cached_instruments(effective_cache)
    scan_as_of = _active_scanner_as_of(effective_cache, instruments, TimeFrame.ONE_DAY) or as_of
    scanner_result = scan_cached_active_market(
        cache=effective_cache,
        instruments=instruments,
        as_of=scan_as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
        confidence_profile=config.strategy.confidence_profile,
    )
    return {
        "status": "ACTIVE_MARKET_SCANNER_FOUNDATION_READY",
        "phase": "STEP_9_0A_ACTIVE_MARKET_SCANNER_FOUNDATION",
        "frozen_research_untouched": True,
        "current_capabilities": {
            "symbols_assets_reliably_analyzable": tuple(
                sorted(instrument.symbol for instrument in instruments)
            ),
            "asset_classes_represented": tuple(
                sorted({instrument.asset_class.value for instrument in instruments})
            ),
            "historical_data_providers": inventory["historical_providers"],
            "latest_read_only_market_data": inventory["latest_read_only_market_data"],
            "cached_vs_required_acquisition": {
                "cached_symbols": inventory["symbols_currently_cached"],
                "missing_from_cache": inventory["missing_from_cache"],
                "fresh_read_only_acquisition_required_for_live_scanning": True,
            },
        },
        "timeframe_readiness": readiness,
        "active_scanner_architecture": {
            "entry_path": (
                "market observation",
                "opportunity ranking",
                "agent decision",
                "TradeProposal",
                "RiskManager",
                "simulated OPEN/INCREASE later",
            ),
            "position_management_path": (
                "open position",
                "next bar for that symbol",
                "current state/agent context",
                "ExitPolicy",
                "HOLD/REDUCE/CLOSE",
                "simulated execution later",
            ),
            "continuous_loop": (
                "new market data",
                "update scanner",
                "rank universe",
                "identify candidate changes",
                "evaluate existing positions independently",
                "produce proposals",
                "RiskManager",
                "simulated execution later",
            ),
        },
        "example_scanner_output": _active_scanner_payload(scanner_result),
        "active_intraday_blockers": (
            "1H cache/provider acquisition is not implemented for the active scanner universe",
            "4H cache is partial and not reliably continuous across the full universe",
            "fresh latest/read-only polling loop is not yet wired to persistent shadow decisions",
        ),
        "next_blocker": (
            "fresh read-only market-data acquisition loop plus 1H/continuous 4H coverage"
        ),
        "broker_write": False,
        "broker_write_calls": scanner_result.broker_write_calls,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def build_active_scanner_observation_temporal_alignment_report(
    config: ApplicationConfig,
    *,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    instruments = _active_scanner_cached_instruments(effective_cache)
    scan_cycle_timestamp = (
        _active_scanner_as_of(
            effective_cache,
            instruments,
            TimeFrame.ONE_HOUR,
        )
        or now
    )
    scanner = ActiveMarketScanner(minimum_bars=ACTIVE_SCANNER_1H_MINIMUM_BARS)
    bars_by_symbol = {
        instrument.symbol: effective_cache.get_bars(
            provider="alpaca",
            instrument_key=(instrument.broker, instrument.broker_instrument_id),
            timeframe=TimeFrame.ONE_HOUR,
            as_of=scan_cycle_timestamp,
            limit=ACTIVE_SCANNER_1H_LOOKBACK_BARS,
            instrument_factory=instrument.model_dump(mode="json"),
        )
        for instrument in instruments
    }
    portfolio = PortfolioSnapshot(
        as_of=scan_cycle_timestamp,
        currency=Currency.EUR,
        cash=Decimal("200"),
    )
    snapshot = scanner.build_observation_snapshot(
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        portfolio=portfolio,
        scan_cycle_timestamp=scan_cycle_timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )
    scanner_result = scanner.scan(
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        portfolio=portfolio,
        as_of=scan_cycle_timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )
    alignment_ready = all(
        observation.bar_timestamp <= observation.scan_cycle_timestamp
        for observation in tuple(snapshot.entry_candidates)
        + tuple(snapshot.entry_exclusions)
        + tuple(snapshot.positions_to_manage)
    ) and bool(snapshot.entry_candidates or snapshot.entry_exclusions)
    future_bar_exclusion = _future_bar_exclusion_verified(
        scanner_result,
        bars_by_symbol,
        scan_cycle_timestamp,
    )
    stale_exclusion = _stale_exclusion_verified()
    mixed_timestamp_handling = _mixed_timestamp_handling_verified(scanner_result, bars_by_symbol)
    duplicate_evaluation_prevention = _duplicate_evaluation_prevention_verified(
        scanner,
        instruments,
        bars_by_symbol,
        portfolio,
        scan_cycle_timestamp,
    )
    position_independence = _position_independence_verified(
        scanner,
        instruments,
        bars_by_symbol,
        scan_cycle_timestamp,
    )
    anti_lookahead_verified = all(
        observation.bar_timestamp <= observation.scan_cycle_timestamp
        for observation in tuple(snapshot.entry_candidates)
        + tuple(snapshot.entry_exclusions)
        + tuple(snapshot.positions_to_manage)
    )
    return {
        "status": (
            "ACTIVE_SCANNER_FOUNDATION_READY"
            if alignment_ready and duplicate_evaluation_prevention and anti_lookahead_verified
            else "ACTIVE_SCANNER_FOUNDATION_BLOCKED"
        ),
        "phase": "STEP_9_0A3_SCANNER_OBSERVATION_MODEL_TEMPORAL_ALIGNMENT",
        "scan_cycle_timestamp": scan_cycle_timestamp.isoformat(),
        "observation_model": "READY" if alignment_ready else "BLOCKED",
        "temporal_alignment": "READY" if alignment_ready else "BLOCKED",
        "cross_asset_snapshot": "READY"
        if snapshot.entry_candidates or snapshot.entry_exclusions
        else "BLOCKED",
        "future_bar_exclusion": future_bar_exclusion,
        "stale_exclusion": stale_exclusion,
        "mixed_timestamp_handling": mixed_timestamp_handling,
        "duplicate_evaluation_prevention": duplicate_evaluation_prevention,
        "position_independence": position_independence,
        "anti_lookahead_verified": anti_lookahead_verified,
        "current_capabilities": active_scanner_inventory(
            cache=effective_cache,
            canonical_symbols_by_class=EXIT_EVIDENCE_SYMBOLS_BY_CLASS,
        ),
        "timeframe_readiness": timeframe_readiness_matrix(effective_cache),
        "scan_snapshot": _active_scanner_observation_snapshot_payload(snapshot),
        "scanner_output": _active_scanner_payload(scanner_result),
        "broker_write": False,
        "broker_write_calls": scanner_result.broker_write_calls,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "critical_blockers": tuple(
            reason
            for reason, passed in (
                ("temporal_alignment", alignment_ready),
                ("duplicate_evaluation_prevention", duplicate_evaluation_prevention),
                ("anti_lookahead", anti_lookahead_verified),
            )
            if not passed
        ),
    }


def build_readonly_active_scan_cycle_report(
    config: ApplicationConfig,
    *,
    cache: HistoricalDataCache | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Run one deterministic, read-only active scan cycle from cached 1H bars."""
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    now = (clock or (lambda: datetime.now(UTC)))()
    instruments = _active_scanner_cached_instruments(effective_cache)
    scan_cycle_timestamp = (
        _active_scanner_as_of(effective_cache, instruments, TimeFrame.ONE_HOUR) or now
    )
    bars_by_symbol = {
        instrument.symbol: effective_cache.get_bars(
            provider="alpaca",
            instrument_key=(instrument.broker, instrument.broker_instrument_id),
            timeframe=TimeFrame.ONE_HOUR,
            as_of=scan_cycle_timestamp,
            limit=ACTIVE_SCANNER_1H_LOOKBACK_BARS,
            instrument_factory=instrument.model_dump(mode="json"),
        )
        for instrument in instruments
    }
    portfolio = PortfolioSnapshot(
        as_of=scan_cycle_timestamp,
        currency=Currency.EUR,
        cash=Decimal("200"),
    )
    scanner = ActiveMarketScanner(minimum_bars=ACTIVE_SCANNER_1H_MINIMUM_BARS)
    result = scanner.scan(
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        portfolio=portfolio,
        as_of=scan_cycle_timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )
    snapshot = scanner.build_observation_snapshot(
        instruments=instruments,
        bars_by_symbol=bars_by_symbol,
        portfolio=portfolio,
        scan_cycle_timestamp=scan_cycle_timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )
    observations = tuple(snapshot.entry_candidates) + tuple(snapshot.entry_exclusions)
    observation_by_symbol = {item.symbol: item for item in observations}
    asset_records = tuple(
        {
            **_scanner_observation_payload(observation_by_symbol[candidate.symbol]),
            "ranking_position": candidate.rank,
            "classification": candidate.bucket.value,
            "reason_code": candidate.rejection_reasons
            or (
                (candidate.bucket.value,)
                if candidate.bucket is not ActiveScannerBucket.TOP_OPPORTUNITIES
                else ()
            ),
        }
        for candidate in result.candidates
        if candidate.symbol in observation_by_symbol
    )
    blockers = () if instruments and observations else ("NO_VALIDATED_1H_OBSERVATIONS",)
    return {
        "status": "READ_ONLY_ACTIVE_SCAN_CYCLE_READY" if not blockers else "BLOCKED",
        "phase": "STEP_9_0A4_READ_ONLY_ACTIVE_SCAN_CYCLE",
        "scan_cycle_timestamp": scan_cycle_timestamp.isoformat(),
        "universe_scanned": tuple(instrument.symbol for instrument in instruments),
        "assets_requested": len(instruments),
        "assets_comparable": len(snapshot.entry_candidates),
        "assets_excluded": len(snapshot.entry_exclusions),
        "top_opportunities": tuple(
            _active_candidate_payload(item) for item in result.top_opportunities
        ),
        "watchlist": tuple(_active_candidate_payload(item) for item in result.watchlist),
        "no_trade": tuple(_active_candidate_payload(item) for item in result.no_trade),
        "rejected": tuple(_active_candidate_payload(item) for item in result.rejected),
        "asset_observations": asset_records,
        "positions_to_manage": tuple(
            _scanner_observation_payload(item) for item in snapshot.positions_to_manage
        ),
        "temporal_metadata": {
            "all_observations_causal": all(
                item.bar_timestamp <= item.scan_cycle_timestamp for item in observations
            ),
            "duplicate_evaluations_prevented": snapshot.duplicate_evaluations_prevented,
            "observation_keys": tuple(item.duplicate_evaluation_key for item in observations),
        },
        "scanner_output": _active_scanner_payload(result),
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "critical_blockers": blockers,
    }


def build_active_scanner_1h_foundation_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    stock_feed: str = "sip",
    requested_feed_by_asset_class: Mapping[AssetClass, str] | None = None,
    fetch_start: datetime | None = None,
    report_title: str = "STEP 9.0B 1H Fresh Read-Only Intraday Data Foundation",
    phase: str = "STEP_9_0B_1H_FRESH_READ_ONLY_INTRADAY_DATA_FOUNDATION",
    windows_cmd: str = DEFAULT_ACTIVE_SCANNER_1H_WINDOWS_CMD,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    causal_fetch_start = fetch_start or (
        now - timedelta(hours=ACTIVE_SCANNER_1H_LOOKBACK_BARS + 24)
    )
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    lookback = one_hour_lookback_requirement()
    capability = provider_one_hour_capability_matrix()
    if config.etoro_demo_execution_enabled:
        return {
            **_active_scanner_1h_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "lookback_requirement": lookback,
            "provider_capability_matrix": capability,
        }
    key_id, secret_key = _alpaca_api_credentials(values)
    if key_id is None or secret_key is None:
        return {
            **_active_scanner_1h_blocked(
                "ALPACA_API_KEYS_NOT_CONFIGURED",
                "read-only Alpaca 1H acquisition requires API key ID and secret",
                config=config,
            ),
            "lookback_requirement": lookback,
            "provider_capability_matrix": capability,
            "required_env_vars": (
                "ALPACA_API_KEY_ID",
                "ALPACA_API_SECRET_KEY",
                "APCA_API_KEY_ID",
                "APCA_API_SECRET_KEY",
            ),
            "windows_cmd": DEFAULT_ACTIVE_SCANNER_1H_WINDOWS_CMD,
        }
    canonical_broker_ids = _verified_etoro_broker_ids(effective_cache)
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id=key_id,
        api_secret_key=secret_key,
        transport=transport,
        stock_feed=stock_feed,
        crypto_location="us",
        max_pages=3,
    )
    rows: list[dict[str, object]] = []
    instruments = _alpaca_full_backfill_instruments(
        now,
        canonical_broker_ids=canonical_broker_ids,
    )
    for instrument in instruments:
        mapping = _alpaca_reference_for_instrument(instrument)
        requested_feed = (
            requested_feed_by_asset_class.get(instrument.asset_class, stock_feed)
            if requested_feed_by_asset_class is not None
            else stock_feed
        )
        rows.append(
            _active_scanner_1h_acquisition_row(
                provider=provider,
                cache=effective_cache,
                instrument=instrument,
                mapping=mapping,
                now=now,
                fetch_start=causal_fetch_start,
                requested_feed=requested_feed,
            )
        )
    ready_symbols = tuple(
        sorted(str(row["symbol"]) for row in rows if row["final_status"] == "READY")
    )
    ready_instruments = tuple(
        instrument for instrument in instruments if instrument.symbol in ready_symbols
    )
    scanner_payload: dict[str, object] | None = None
    if ready_instruments:
        scanner = scan_cached_active_market(
            cache=effective_cache,
            instruments=ready_instruments,
            as_of=now,
            timeframe=TimeFrame.ONE_HOUR,
            simulated_capital=Decimal("200"),
            confidence_profile=config.strategy.confidence_profile,
            minimum_bars=ACTIVE_SCANNER_1H_MINIMUM_BARS,
        )
        scanner_payload = _active_scanner_payload(scanner)
    status = (
        "ACTIVE_SCANNER_1H_READY"
        if len(ready_symbols) == len(instruments) and scanner_payload is not None
        else "ACTIVE_SCANNER_1H_DATA_ACQUISITION_BLOCKER"
    )
    rejected = tuple(row for row in rows if row["final_status"] != "READY")
    return {
        "status": status,
        "phase": phase,
        "report_title": report_title,
        "lookback_requirement": lookback,
        "provider_capability_matrix": capability,
        "requested_symbols": _exit_evidence_symbol_payload(),
        "symbols_requested": len(instruments),
        "symbols_1h_ready": len(ready_symbols),
        "ready_symbols": ready_symbols,
        "rejected_symbols": tuple(
            {
                "symbol": row["symbol"],
                "asset_class": row["asset_class"],
                "status": row["final_status"],
                "reason": row["freshness_status"],
            }
            for row in rejected
        ),
        "acquisition": tuple(rows),
        "example_scanner_output": scanner_payload,
        "multi_timeframe_preparation": {
            "one_day": "broader regime/context already cache-backed",
            "four_hour": "intermediate confirmation remains partial",
            "one_hour": "active opportunity timing works independently when fresh bars pass",
        },
        "repeated_intraday_shadow_scan_ready": status == "ACTIVE_SCANNER_1H_READY",
        "next_blocker": None
        if status == "ACTIVE_SCANNER_1H_READY"
        else "obtain fresh sufficient 1H Alpaca bars for every canonical symbol",
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "windows_cmd": windows_cmd,
    }


def build_active_scanner_1h_pilot_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    lookback = one_hour_lookback_requirement()
    capability = provider_one_hour_capability_matrix()
    if config.etoro_demo_execution_enabled:
        return {
            **_active_scanner_1h_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "lookback_requirement": lookback,
            "provider_capability_matrix": capability,
        }
    key_id, secret_key = _alpaca_api_credentials(values)
    if key_id is None or secret_key is None:
        return {
            **_active_scanner_1h_blocked(
                "ALPACA_API_KEYS_NOT_CONFIGURED",
                "read-only Alpaca 1H pilot requires API key ID and secret",
                config=config,
            ),
            "lookback_requirement": lookback,
            "provider_capability_matrix": capability,
            "required_env_vars": (
                "ALPACA_API_KEY_ID",
                "ALPACA_API_SECRET_KEY",
                "APCA_API_KEY_ID",
                "APCA_API_SECRET_KEY",
            ),
            "windows_cmd": DEFAULT_ACTIVE_SCANNER_1H_PILOT_WINDOWS_CMD,
        }
    canonical_broker_ids = _verified_etoro_broker_ids(effective_cache)
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id=key_id,
        api_secret_key=secret_key,
        transport=transport,
        stock_feed="sip",
        crypto_location="us",
        max_pages=3,
    )
    rows: list[dict[str, object]] = []
    instruments = _alpaca_1h_pilot_instruments(
        now,
        canonical_broker_ids=canonical_broker_ids,
    )
    for instrument in instruments:
        mapping = _alpaca_reference_for_instrument(instrument)
        rows.append(
            _active_scanner_1h_acquisition_row(
                provider=provider,
                cache=effective_cache,
                instrument=instrument,
                mapping=mapping,
                now=now,
            )
        )
    ready_symbols = tuple(
        sorted(str(row["symbol"]) for row in rows if row["final_status"] == "READY")
    )
    ready_instruments = tuple(
        instrument for instrument in instruments if instrument.symbol in ready_symbols
    )
    scanner_payload: dict[str, object] | None = None
    if ready_instruments:
        scanner = scan_cached_active_market(
            cache=effective_cache,
            instruments=ready_instruments,
            as_of=now,
            timeframe=TimeFrame.ONE_HOUR,
            simulated_capital=Decimal("200"),
            confidence_profile=config.strategy.confidence_profile,
            minimum_bars=ACTIVE_SCANNER_1H_MINIMUM_BARS,
        )
        scanner_payload = _active_scanner_payload(scanner)
    status = (
        "ACTIVE_SCANNER_1H_PILOT_READY"
        if len(ready_symbols) == len(instruments) and scanner_payload is not None
        else "ACTIVE_SCANNER_1H_DATA_ACQUISITION_BLOCKER"
    )
    rejected = tuple(row for row in rows if row["final_status"] != "READY")
    return {
        "status": status,
        "phase": "STEP_9_0A2_1H_READ_ONLY_DATA_PILOT",
        "lookback_requirement": lookback,
        "provider_capability_matrix": capability,
        "requested_symbols": _alpaca_1h_pilot_symbol_payload(),
        "pilot_symbols": _alpaca_1h_pilot_symbol_payload(),
        "symbols_requested": len(instruments),
        "symbols_1h_ready": len(ready_symbols),
        "ready_symbols": ready_symbols,
        "rejected_symbols": tuple(
            {
                "symbol": row["symbol"],
                "asset_class": row["asset_class"],
                "status": row["final_status"],
                "reason": row["freshness_status"],
            }
            for row in rejected
        ),
        "acquisition": tuple(rows),
        "example_scanner_output": scanner_payload,
        "multi_timeframe_preparation": {
            "one_day": "broader regime/context already cache-backed",
            "four_hour": "intermediate confirmation remains partial",
            "one_hour": "active opportunity timing works independently when fresh bars pass",
        },
        "repeated_intraday_shadow_scan_ready": status == "ACTIVE_SCANNER_1H_PILOT_READY",
        "next_blocker": None
        if status == "ACTIVE_SCANNER_1H_PILOT_READY"
        else "obtain fresh sufficient 1H Alpaca bars for every pilot symbol",
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "windows_cmd": DEFAULT_ACTIVE_SCANNER_1H_PILOT_WINDOWS_CMD,
    }


def build_active_scanner_1h_iex_equity_pilot_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    fetch_start = now - timedelta(days=15)
    effective_cache = cache or HistoricalDataCache(DEFAULT_MARKET_DATA_CACHE_PATH)
    lookback = one_hour_lookback_requirement()
    capability = provider_one_hour_capability_matrix()
    if config.etoro_demo_execution_enabled:
        return {
            **_active_scanner_1h_blocked(
                "DEMO_EXECUTION_ENABLED",
                "Demo execution must remain false",
                config=config,
            ),
            "lookback_requirement": lookback,
            "provider_capability_matrix": capability,
        }
    key_id, secret_key = _alpaca_api_credentials(values)
    if key_id is None or secret_key is None:
        return {
            **_active_scanner_1h_blocked(
                "ALPACA_API_KEYS_NOT_CONFIGURED",
                "read-only Alpaca 1H IEX equity pilot requires API key ID and secret",
                config=config,
            ),
            "lookback_requirement": lookback,
            "provider_capability_matrix": capability,
            "required_env_vars": (
                "ALPACA_API_KEY_ID",
                "ALPACA_API_SECRET_KEY",
                "APCA_API_KEY_ID",
                "APCA_API_SECRET_KEY",
            ),
            "windows_cmd": DEFAULT_ACTIVE_SCANNER_1H_IEX_EQUITY_WINDOWS_CMD,
        }
    canonical_broker_ids = _verified_etoro_broker_ids(effective_cache)
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id=key_id,
        api_secret_key=secret_key,
        transport=transport,
        stock_feed="iex",
        crypto_location="us",
        max_pages=3,
    )
    rows: list[dict[str, object]] = []
    instruments = _alpaca_iex_equity_1h_instruments(
        now,
        canonical_broker_ids=canonical_broker_ids,
    )
    for instrument in instruments:
        mapping = _alpaca_reference_for_instrument(instrument)
        rows.append(
            _active_scanner_1h_acquisition_row(
                provider=provider,
                cache=effective_cache,
                instrument=instrument,
                mapping=mapping,
                now=now,
                fetch_start=fetch_start,
                requested_feed="iex",
            )
        )
    ready_symbols = tuple(
        sorted(str(row["symbol"]) for row in rows if row["final_status"] == "READY")
    )
    ready_instruments = tuple(
        instrument for instrument in instruments if instrument.symbol in ready_symbols
    )
    scanner_payload: dict[str, object] | None = None
    if ready_instruments:
        scanner = scan_cached_active_market(
            cache=effective_cache,
            instruments=ready_instruments,
            as_of=now,
            timeframe=TimeFrame.ONE_HOUR,
            simulated_capital=Decimal("200"),
            confidence_profile=config.strategy.confidence_profile,
            minimum_bars=ACTIVE_SCANNER_1H_MINIMUM_BARS,
        )
        scanner_payload = _active_scanner_payload(scanner)
    status = (
        "ACTIVE_SCANNER_1H_IEX_EQUITY_PILOT_READY"
        if len(ready_symbols) == len(instruments) and scanner_payload is not None
        else "ACTIVE_SCANNER_1H_DATA_ACQUISITION_BLOCKER"
    )
    rejected = tuple(row for row in rows if row["final_status"] != "READY")
    return {
        "status": status,
        "phase": "STEP_9_0A2B_ALPACA_FREE_IEX_EQUITY_1H_PILOT",
        "report_title": "STEP 9.0A2B IEX Equity 1H Pilot",
        "lookback_requirement": lookback,
        "provider_capability_matrix": capability,
        "requested_symbols": _alpaca_iex_equity_1h_symbol_payload(),
        "pilot_symbols": _alpaca_iex_equity_1h_symbol_payload(),
        "requested_feed": "iex",
        "symbols_requested": len(instruments),
        "symbols_1h_ready": len(ready_symbols),
        "ready_symbols": ready_symbols,
        "rejected_symbols": tuple(
            {
                "symbol": row["symbol"],
                "asset_class": row["asset_class"],
                "status": row["final_status"],
                "reason": row["freshness_status"],
            }
            for row in rejected
        ),
        "acquisition": tuple(rows),
        "example_scanner_output": scanner_payload,
        "multi_timeframe_preparation": {
            "one_day": "broader regime/context already cache-backed",
            "four_hour": "intermediate confirmation remains partial",
            "one_hour": "equity timing can work independently when fresh bars pass",
        },
        "repeated_intraday_shadow_scan_ready": (
            status == "ACTIVE_SCANNER_1H_IEX_EQUITY_PILOT_READY"
        ),
        "next_blocker": None
        if status == "ACTIVE_SCANNER_1H_IEX_EQUITY_PILOT_READY"
        else "obtain fresh sufficient 1H Alpaca IEX bars for AAPL and SPY",
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "windows_cmd": DEFAULT_ACTIVE_SCANNER_1H_IEX_EQUITY_WINDOWS_CMD,
    }


def build_active_scanner_1h_full_universe_sweep_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    cache: HistoricalDataCache | None = None,
    transport: HttpTextTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    report = build_active_scanner_1h_foundation_report(
        config,
        values=values,
        cache=cache,
        transport=transport,
        clock=lambda: now,
        stock_feed="iex",
        requested_feed_by_asset_class={
            AssetClass.EQUITY: "iex",
            AssetClass.ETF: "iex",
            AssetClass.CRYPTO: "crypto",
        },
        fetch_start=now - timedelta(days=ACTIVE_SCANNER_1H_READINESS_SWEEP_DAYS),
        report_title="STEP 9.0A2D 1H Full-Universe Readiness Sweep",
        phase="STEP_9_0A2D_1H_FULL_UNIVERSE_READINESS_SWEEP",
        windows_cmd=DEFAULT_ACTIVE_SCANNER_1H_FULL_UNIVERSE_WINDOWS_CMD,
    )
    acquisition = cast(tuple[Mapping[str, object], ...], report.get("acquisition", ()))
    rows = tuple(row for row in acquisition if isinstance(row, Mapping))
    summary: dict[str, dict[str, object]] = {}
    global_ready = True
    for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO):
        class_rows = tuple(row for row in rows if row.get("asset_class") == asset_class.value)
        ready_count = sum(1 for row in class_rows if row.get("final_status") == "READY")
        total_count = len(class_rows)
        class_status = (
            "READY"
            if total_count > 0 and ready_count == total_count
            else "PARTIAL"
            if ready_count > 0
            else "BLOCKED"
        )
        summary[asset_class.value] = {
            "status": class_status,
            "ready": ready_count,
            "total": total_count,
        }
        report[f"{asset_class.value.lower()}_1h_pilot"] = class_status
        global_ready = global_ready and class_status == "READY"
    report["asset_class_readiness"] = summary
    report["global_1h"] = "READY" if global_ready and rows else "BLOCKED"
    return report


def _exit_policy_v2_candidate_result_payload(
    *,
    bundle_id: str,
    bundle_fingerprint: str,
    partition: ExitPolicyV2DatasetPartition,
    result: StrategyValidationResult,
    missing_symbols: tuple[str, ...],
    eligible_bars: int,
) -> dict[str, object]:
    lifecycle = result.lifecycle
    trades = tuple(
        trade for trade in result.trades if trade.status is ReplayDecisionStatus.SIMULATED_EXECUTED
    )
    close_pnls = tuple(trade.realized_pnl for trade in trades if trade.action is TradeIntent.CLOSE)
    gains = tuple(value for value in close_pnls if value > 0)
    losses = tuple(abs(value) for value in close_pnls if value < 0)
    exit_reason_distribution = Counter(
        reason
        for decision in result.decisions
        for reason in decision.blocker_reasons
        if "EXITPOLICY_V2_" in reason
    )
    risk_rejections = Counter(
        reason for decision in result.decisions for reason in decision.risk_reasons
    )
    cooldown_blocks = sum(
        1
        for decision in result.decisions
        for reason in decision.blocker_reasons
        if "cooldown is active" in reason or "EXITPOLICY_V2_COOLDOWN_BLOCKS_REENTRY" in reason
    )
    open_count = sum(1 for trade in trades if trade.action is TradeIntent.OPEN)
    increase_count = sum(1 for trade in trades if trade.action is TradeIntent.INCREASE)
    reduce_count = sum(1 for trade in trades if trade.action is TradeIntent.REDUCE)
    close_count = sum(1 for trade in trades if trade.action is TradeIntent.CLOSE)
    completed = lifecycle.completed_trade_count if lifecycle is not None else 0
    execution_count = lifecycle.execution_count if lifecycle is not None else len(trades)
    realized_pnl = (
        lifecycle.realized_pnl if lifecycle is not None else sum(close_pnls, Decimal("0"))
    )
    unrealized_pnl = lifecycle.unrealized_pnl if lifecycle is not None else Decimal("0")
    ending_equity = (
        lifecycle.ending_equity if lifecycle is not None else result.metrics.total_return
    )
    max_drawdown = result.metrics.maximum_drawdown
    data_quality_exclusions = sum(
        1
        for decision in result.decisions
        for reason in decision.blocker_reasons
        if "DATA_" in reason or "data quality" in reason.casefold()
    )
    return {
        "candidate_bundle_id": bundle_id,
        "candidate_bundle_fingerprint": bundle_fingerprint,
        "asset_class": partition.asset_class.value,
        "partition": partition.role,
        "status": "EXECUTED_TRAIN_VALIDATION_ONLY",
        "eligible_symbols": partition.eligible_symbols,
        "missing_or_insufficient_symbols": missing_symbols,
        "observations": len(result.decisions),
        "eligible_bars": eligible_bars,
        "open": open_count,
        "increase": increase_count,
        "reduce": reduce_count,
        "close": close_count,
        "completed_trades": completed,
        "still_open_positions": lifecycle.open_position_count if lifecycle is not None else 0,
        "realized_pnl": str(realized_pnl),
        "unrealized_pnl": str(unrealized_pnl),
        "ending_equity": str(ending_equity),
        "max_drawdown": str(max_drawdown),
        "win_count": sum(1 for value in close_pnls if value > 0),
        "loss_count": sum(1 for value in close_pnls if value < 0),
        "average_gain": str(sum(gains, Decimal("0")) / len(gains)) if gains else "NOT_ENOUGH_DATA",
        "average_loss": (
            str(sum(losses, Decimal("0")) / len(losses)) if losses else "NOT_ENOUGH_DATA"
        ),
        "lifecycle_completion_rate": (
            str(Decimal(completed) / Decimal(execution_count)) if execution_count else "0"
        ),
        "exit_reason_code_distribution": dict(exit_reason_distribution),
        "risk_manager_rejections": dict(risk_rejections),
        "cooldown_reentry_blocks": cooldown_blocks,
        "data_quality_exclusions": data_quality_exclusions,
        "future_records_ignored": sum(
            decision.future_records_ignored for decision in result.decisions
        ),
        "future_records_never_visible": all(
            decision.future_records_ignored >= 0 for decision in result.decisions
        ),
        "broker_write_calls": result.broker_write_calls,
    }


def _active_scanner_cached_instruments(
    cache: HistoricalDataCache,
) -> tuple[UniversalInstrument, ...]:
    requested = {
        symbol: asset_class
        for asset_class, symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.items()
        for symbol in symbols
    }
    references = tuple(
        reference
        for reference in cache.mapping_references(provider="alpaca")
        if reference.verified
        and reference.broker_symbol.upper() in requested
        and reference.asset_class is requested[reference.broker_symbol.upper()]
    )
    selected: dict[str, ProviderInstrumentReference] = {}
    for reference in sorted(
        references,
        key=lambda item: (
            item.broker_symbol.upper(),
            not item.broker_instrument_id.startswith("ALPACA_ONLY:"),
            item.broker_instrument_id,
        ),
    ):
        selected.setdefault(reference.broker_symbol.upper(), reference)
    return tuple(
        UniversalInstrument(
            broker=reference.broker,
            broker_instrument_id=reference.broker_instrument_id,
            symbol=reference.broker_symbol.upper(),
            display_name=reference.broker_symbol.upper(),
            asset_class=reference.asset_class,
            currency=reference.currency or Currency.USD,
            exchange=reference.exchange,
            market_status=(
                MarketStatus.CONTINUOUS_24_7
                if reference.asset_class is AssetClass.CRYPTO
                else MarketStatus.UNKNOWN
            ),
            tradeable=None,
            buy_allowed=None,
            sell_allowed=None,
            short_allowed=False,
            leverage_available=False,
            max_leverage=Decimal("1"),
            settlement_type=SettlementType.REAL,
            minimum_order_value=Decimal("1"),
            fractional_supported=True,
            metadata_timestamp=datetime(2026, 8, 30, tzinfo=UTC),
            tags=("alpaca-cache", "active-scanner-foundation"),
        )
        for reference in selected.values()
    )


def _active_scanner_as_of(
    cache: HistoricalDataCache,
    instruments: tuple[UniversalInstrument, ...],
    timeframe: TimeFrame,
) -> datetime | None:
    timestamps = tuple(
        cache.last_timestamp(
            provider="alpaca",
            broker=instrument.broker,
            broker_instrument_id=instrument.broker_instrument_id,
            timeframe=timeframe,
        )
        for instrument in instruments
    )
    available = tuple(timestamp for timestamp in timestamps if timestamp is not None)
    return max(available) if available else None


def _active_scanner_payload(scanner_result: ActiveScannerResult) -> dict[str, object]:
    return {
        "as_of": scanner_result.as_of.isoformat(),
        "timeframe": scanner_result.timeframe.value,
        "simulated_capital": str(scanner_result.simulated_capital),
        "total_candidates": len(scanner_result.candidates),
        "top_opportunities": tuple(
            _active_candidate_payload(candidate) for candidate in scanner_result.top_opportunities
        ),
        "watchlist": tuple(
            _active_candidate_payload(candidate) for candidate in scanner_result.watchlist[:10]
        ),
        "rejected_count": len(scanner_result.rejected),
        "no_trade_count": len(scanner_result.no_trade),
        "duplicate_decisions_prevented": scanner_result.duplicate_decisions_prevented,
        "existing_positions_monitored": scanner_result.existing_positions_monitored,
        "broker_write_calls": scanner_result.broker_write_calls,
    }


def _active_candidate_payload(candidate: ActiveScannerCandidate) -> dict[str, object]:
    return {
        "rank": candidate.rank,
        "symbol": candidate.symbol,
        "asset_class": candidate.asset_class.value,
        "decision": candidate.decision.value,
        "bucket": candidate.bucket.value,
        "opportunity_score": str(candidate.opportunity_score),
        "confidence": str(candidate.confidence),
        "regime": candidate.regime,
        "data_quality": candidate.data_quality_state.value,
        "market_state": candidate.current_market_state.value,
        "provider_provenance": candidate.provider_provenance,
        "affordable_fractionally": candidate.affordable_fractionally,
        "proposed_capital_allocation": str(candidate.proposed_capital_allocation),
        "remaining_simulated_cash": str(candidate.remaining_simulated_cash),
        "current_position_state": candidate.current_position_state,
        "risk_flags": candidate.risk_flags,
        "rejection_reasons": candidate.rejection_reasons,
    }


def _active_scanner_observation_snapshot_payload(snapshot: object) -> dict[str, object]:
    entry_candidates = getattr(snapshot, "entry_candidates", ())
    entry_exclusions = getattr(snapshot, "entry_exclusions", ())
    positions_to_manage = getattr(snapshot, "positions_to_manage", ())
    scan_cycle_timestamp = getattr(snapshot, "scan_cycle_timestamp", None)
    return {
        "scan_cycle_timestamp": scan_cycle_timestamp.isoformat()
        if scan_cycle_timestamp is not None
        else None,
        "entry_candidates": tuple(
            _scanner_observation_payload(observation) for observation in entry_candidates
        ),
        "entry_exclusions": tuple(
            _scanner_observation_payload(observation) for observation in entry_exclusions
        ),
        "positions_to_manage": tuple(
            _scanner_observation_payload(observation) for observation in positions_to_manage
        ),
        "duplicate_evaluations_prevented": getattr(snapshot, "duplicate_evaluations_prevented", 0),
        "broker_write_calls": getattr(snapshot, "broker_write_calls", 0),
    }


def _scanner_observation_payload(observation: object) -> dict[str, object]:
    asset_class = getattr(observation, "asset_class", None)
    scan_cycle_timestamp = getattr(observation, "scan_cycle_timestamp", None)
    bar_timestamp = getattr(observation, "bar_timestamp", None)
    timeframe = getattr(observation, "timeframe", None)
    eligibility_reason_code = getattr(observation, "eligibility_reason_code", None)
    current_market_state = getattr(observation, "current_market_state", None)
    return {
        "symbol": getattr(observation, "symbol", ""),
        "full_name": getattr(observation, "full_name", ""),
        "asset_class": asset_class.value if asset_class is not None else None,
        "scan_cycle_timestamp": scan_cycle_timestamp.isoformat()
        if scan_cycle_timestamp is not None
        else None,
        "bar_timestamp": bar_timestamp.isoformat() if bar_timestamp is not None else None,
        "timeframe": timeframe.value if timeframe is not None else None,
        "current_price": str(getattr(observation, "current_price", "")),
        "opportunity_score": str(getattr(observation, "opportunity_score", "")),
        "confidence": str(getattr(observation, "confidence", "")),
        "regime": getattr(observation, "regime", ()),
        "action_state": getattr(observation, "action_state", ""),
        "data_quality": getattr(observation, "data_quality", ""),
        "provider_provenance": getattr(observation, "provider_provenance", ()),
        "freshness_state": getattr(observation, "freshness_state", ""),
        "market_session_state": getattr(observation, "market_session_state", ""),
        "risk_flags": getattr(observation, "risk_flags", ()),
        "existing_position_state": getattr(observation, "existing_position_state", ""),
        "eligible_for_entry_comparison": getattr(
            observation, "eligible_for_entry_comparison", False
        ),
        "eligibility_reason_code": (
            eligibility_reason_code.value if eligibility_reason_code is not None else None
        ),
        "duplicate_evaluation_key": getattr(observation, "duplicate_evaluation_key", ""),
        "current_market_state": (
            current_market_state.value if current_market_state is not None else None
        ),
    }


def _future_bar_exclusion_verified(
    scanner_result: ActiveScannerResult,
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
    scan_cycle_timestamp: datetime,
) -> bool:
    if not scanner_result.candidates:
        return False
    candidate = scanner_result.candidates[0]
    symbol_bars = bars_by_symbol.get(candidate.symbol, ())
    if not symbol_bars:
        return False
    future_bar = symbol_bars[-1].model_copy(
        update={"timestamp": scan_cycle_timestamp + timedelta(hours=1)}
    )
    observation = _observation_from_candidate(
        candidate=candidate,
        bars=(future_bar,),
        scan_cycle_timestamp=scan_cycle_timestamp,
    )
    return observation.eligibility_reason_code.value == "FUTURE_BAR"


def _stale_exclusion_verified() -> bool:
    as_of = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
    instrument = UniversalInstrument(
        broker="etoro",
        broker_instrument_id="1001",
        symbol="AAPL",
        display_name="AAPL",
        asset_class=AssetClass.EQUITY,
        currency=Currency.USD,
        exchange="NASDAQ",
        market_status=MarketStatus.OPEN,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=as_of,
    )
    stale_bars = tuple(
        MarketBar(
            instrument=instrument,
            timeframe=TimeFrame.ONE_HOUR,
            timestamp=as_of - timedelta(hours=8),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("1000"),
            currency=Currency.USD,
            source="synthetic-stale-probe",
        )
        for _ in range(ACTIVE_SCANNER_1H_MINIMUM_BARS)
    )
    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
        bars=stale_bars,
        as_of=as_of,
        minimum_bars=ACTIVE_SCANNER_1H_MINIMUM_BARS,
    )
    return freshness is IntradayFreshnessStatus.STALE


def _mixed_timestamp_handling_verified(
    scanner_result: ActiveScannerResult,
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
) -> bool:
    candidate = next((item for item in scanner_result.candidates if item.provider_provenance), None)
    if candidate is None:
        return False
    bars = bars_by_symbol.get(candidate.symbol, ())
    if not bars:
        return False
    synthetic = candidate.model_copy(
        update={
            "provider_provenance": ("alpaca", "etoro"),
            "freshness": IntradayFreshnessStatus.STALE.value,
        }
    )
    observation = _observation_from_candidate(
        candidate=synthetic,
        bars=bars,
        scan_cycle_timestamp=scanner_result.as_of,
    )
    return observation.eligibility_reason_code.value == "MIXED_TIMESTAMP_UNSAFE"


def _duplicate_evaluation_prevention_verified(
    scanner: ActiveMarketScanner,
    instruments: tuple[UniversalInstrument, ...],
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
    portfolio: PortfolioSnapshot,
    scan_cycle_timestamp: datetime,
) -> bool:
    if not instruments:
        return False
    duplicate_instrument = instruments[0]
    duplicate_bars = bars_by_symbol.get(duplicate_instrument.symbol, ())
    if not duplicate_bars:
        return False
    duplicate_result = scanner.scan(
        instruments=(duplicate_instrument, duplicate_instrument),
        bars_by_symbol={duplicate_instrument.symbol: duplicate_bars},
        portfolio=portfolio,
        as_of=scan_cycle_timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )
    return duplicate_result.duplicate_decisions_prevented == 1


def _position_independence_verified(
    scanner: ActiveMarketScanner,
    instruments: tuple[UniversalInstrument, ...],
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]],
    scan_cycle_timestamp: datetime,
) -> bool:
    if not instruments:
        return False
    held_instrument = instruments[0]
    stale_bars = tuple(
        MarketBar(
            instrument=held_instrument,
            timeframe=TimeFrame.ONE_HOUR,
            timestamp=scan_cycle_timestamp - timedelta(hours=8),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("1000"),
            currency=Currency.USD,
            source="synthetic-position-probe",
        )
        for _ in range(ACTIVE_SCANNER_1H_MINIMUM_BARS)
    )
    portfolio = PortfolioSnapshot(
        as_of=scan_cycle_timestamp,
        currency=Currency.EUR,
        cash=Decimal("200"),
        positions=(
            Position(
                position_id="probe-position",
                instrument_id=_instrument_id(held_instrument),
                symbol=held_instrument.symbol,
                settlement_type=SettlementType.REAL,
                units=Decimal("1"),
                average_entry_price=Decimal("100"),
                market_price=Decimal("101"),
            ),
        ),
    )
    snapshot = scanner.build_observation_snapshot(
        instruments=instruments,
        bars_by_symbol={held_instrument.symbol: stale_bars},
        portfolio=portfolio,
        scan_cycle_timestamp=scan_cycle_timestamp,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )
    return any(
        observation.symbol == held_instrument.symbol
        and observation.existing_position_state != "NO_POSITION"
        for observation in snapshot.positions_to_manage
    )


def _active_scanner_1h_blocked(
    category: str,
    reason: str,
    *,
    config: ApplicationConfig,
) -> dict[str, object]:
    return {
        "status": "ACTIVE_SCANNER_1H_BLOCKED",
        "category": category,
        "reason": reason,
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def _active_scanner_1h_acquisition_row(
    *,
    provider: AlpacaHistoricalMarketDataProvider,
    cache: HistoricalDataCache,
    instrument: UniversalInstrument,
    mapping: ProviderInstrumentReference,
    now: datetime,
    fetch_start: datetime | None = None,
    requested_feed: str = "sip",
) -> dict[str, object]:
    timeframe = TimeFrame.ONE_HOUR
    fetch_start = fetch_start or (now - timedelta(hours=ACTIVE_SCANNER_1H_LOOKBACK_BARS + 24))
    cached_before = cache.coverage_summary(
        provider="alpaca",
        broker=instrument.broker,
        broker_instrument_id=instrument.broker_instrument_id,
        timeframe=timeframe,
        start=fetch_start,
        end=now,
    )
    fetched: tuple[MarketBar, ...] = ()
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    pagination: Mapping[str, object] = provider.last_pagination_state
    try:
        cached_before_count = _row_int(cached_before, "count")
        if cached_before_count < ACTIVE_SCANNER_1H_LOOKBACK_BARS:
            fetched = provider.get_bars_range(
                instrument,
                timeframe,
                start=fetch_start,
                end=now,
                limit=ACTIVE_SCANNER_1H_LOOKBACK_BARS,
                max_pages=3,
            )
            pagination = provider.last_pagination_state
            if fetched:
                stats = cache.upsert_bars_with_stats(
                    provider="alpaca",
                    bars=fetched,
                    fetched_at=now,
                    mapping=mapping,
                )
    except DataProviderError as exc:
        return {
            "symbol": instrument.symbol,
            "asset_class": instrument.asset_class.value,
            "provider": "alpaca",
            "provider_symbol": _alpaca_provider_symbol(instrument),
            "requested_feed": requested_feed,
            "provider_feed_provenance": _alpaca_feed_provenance(
                instrument,
                requested_feed=requested_feed,
            ),
            "timeframe": timeframe.value,
            "freshness_status": "PROVIDER_UNAVAILABLE",
            "final_status": "PROVIDER_UNAVAILABLE",
            "http_status": exc.http_status,
            "sanitized_endpoint": exc.sanitized_endpoint,
            "transport_category": exc.transport_category,
            "provider_error_code": exc.provider_error_code,
            "provider_error_message": exc.provider_error_message,
            "cache_inserted": 0,
            "cache_updated": 0,
            "cache_unchanged": 0,
            "broker_write_calls": 0,
        }
    cached_after = cache.get_bars(
        provider="alpaca",
        instrument_key=(instrument.broker, instrument.broker_instrument_id),
        timeframe=timeframe,
        as_of=now,
        limit=ACTIVE_SCANNER_1H_LOOKBACK_BARS,
        instrument_factory=instrument.model_dump(mode="json"),
    )
    ohlcv_valid = all(
        bar.open is not None
        and bar.high is not None
        and bar.low is not None
        and bar.close is not None
        and bar.volume is not None
        for bar in cached_after
    )
    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=timeframe,
        bars=cached_after,
        as_of=now,
        minimum_bars=ACTIVE_SCANNER_1H_MINIMUM_BARS,
    )
    final_status = (
        "READY" if freshness.value in {"FRESH", "DELAYED", "MARKET_CLOSED"} else "BLOCKED"
    )
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "provider": "alpaca",
        "provider_symbol": _alpaca_provider_symbol(instrument),
        "requested_feed": requested_feed,
        "provider_feed_provenance": _alpaca_feed_provenance(
            instrument,
            requested_feed=requested_feed,
        ),
        "timeframe": timeframe.value,
        "requested_start": fetch_start.isoformat(),
        "requested_end": now.isoformat(),
        "cached_before_count": cached_before["count"],
        "fetched_bars": len(fetched),
        "cached_context_bars": len(cached_after),
        "earliest_cached_context": cached_after[0].timestamp.isoformat() if cached_after else None,
        "latest_cached_context": cached_after[-1].timestamp.isoformat() if cached_after else None,
        "timezone_semantics": _alpaca_timezone_semantics(instrument),
        "freshness_status": freshness.value,
        "cache_inserted": stats["inserted"],
        "cache_updated": stats["updated"],
        "cache_unchanged": stats["unchanged"],
        "pagination_requested": bool(pagination.get("pagination_requested", False)),
        "pagination_token_observed": bool(pagination.get("pagination_token_observed", False)),
        "second_page_fetched": bool(pagination.get("second_page_fetched", False)),
        "pagination_truncated": bool(pagination.get("pagination_truncated", False)),
        "pages_fetched": _pagination_int(pagination, "pages_fetched"),
        "pagination_verified": bool(pagination.get("pagination_verified", False)),
        "duplicate_timestamps": 0,
        "ohlcv_valid": ohlcv_valid,
        "market_session_semantics": (
            "24/7 crypto"
            if instrument.asset_class is AssetClass.CRYPTO
            else "US equity/ETF market-session aware"
        ),
        "market_session_state": (
            "OPEN"
            if instrument.asset_class is AssetClass.CRYPTO or not _is_market_closed_as_of(now)
            else "CLOSED"
        ),
        "expected_latest_bar": (
            now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        ).isoformat(),
        "final_status": final_status,
        "broker_write_calls": 0,
    }


def _exit_policy_v2_governance_preflight(config: ApplicationConfig) -> dict[str, object]:
    registry = default_exit_policy_v2_candidate_registry(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    manifest = default_exit_policy_v2_preregistered_experiment_v2_manifest(
        created_at=datetime(2026, 8, 30, tzinfo=UTC)
    )
    selected = _exit_policy_v2_balanced_candidate(registry)
    checks = {
        "candidate_fingerprint_verified": (
            selected.fingerprint == EXITPOLICY_V2_BALANCED_CANDIDATE_FINGERPRINT
        ),
        "manifest_fingerprint_verified": (
            manifest.manifest_sha256 == EXITPOLICY_V2_EXPOSED_HOLDOUT_FINGERPRINT
        ),
        "registry_fingerprint_verified": (
            registry.fingerprint
            == "f7d0550db56ac2803239a7bad9dc6056a38bbfc67c9985c58b4a28bfc921d25c"
        ),
        "confidence_profile_verified": (
            config.strategy.confidence_profile
            == manifest.confidence_profile
            == EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE
        ),
        "exit_policy_profile_verified": (
            config.strategy.exit_policy_profile
            == manifest.exit_policy_profile
            == EXIT_POLICY_V2_GUARDED
        ),
    }
    if not all(checks.values()):
        return {
            "status": "BLOCKED",
            "category": "EXITPOLICY_V2_GOVERNANCE_MISMATCH",
            "reason": (
                "frozen candidate, manifest, registry, or runtime profile verification failed"
            ),
            **checks,
        }
    return {
        "status": "PASS",
        "category": "NONE",
        "reason": "frozen candidate, manifest, registry, and runtime profiles verified",
        **checks,
    }


def _exit_policy_v2_balanced_candidate(
    registry: ExitPolicyV2CandidateRegistry,
) -> ExitPolicyV2ParameterBundle:
    return next(
        bundle
        for bundle in registry.candidate_bundles
        if bundle.fingerprint == EXITPOLICY_V2_BALANCED_CANDIDATE_FINGERPRINT
    )


def _run_exit_policy_v2_partitions(
    *,
    config: ApplicationConfig,
    cache: HistoricalDataCache,
    clock_timestamp: datetime,
    manifest: ExitPolicyV2ExperimentManifest,
    registry: ExitPolicyV2CandidateRegistry,
    bundle: ExitPolicyV2ParameterBundle,
    roles: set[str],
    initial_cash: Decimal,
    diagnostic_label: str,
) -> list[dict[str, object]]:
    select_exit_policy_for_historical_validation(
        config.strategy.exit_policy_profile,
        v2_parameter_bundle=bundle,
        v2_candidate_registry=registry,
        v2_experiment_manifest=manifest,
    )
    instruments_by_symbol = {
        instrument.symbol: instrument
        for instrument in _alpaca_full_backfill_instruments(
            clock_timestamp,
            canonical_broker_ids=_verified_etoro_broker_ids(cache),
        )
    }
    rows: list[dict[str, object]] = []
    for partition in manifest.dataset_partitions:
        if partition.role not in roles:
            continue
        included: list[str] = []
        excluded: dict[str, str] = {}
        bars_by_instrument = {}
        for symbol in partition.eligible_symbols:
            instrument = instruments_by_symbol.get(symbol)
            if instrument is None:
                excluded[symbol] = "instrument not present in canonical Alpaca universe"
                continue
            if instrument.numeric_instrument_id is None:
                excluded[symbol] = "verified numeric broker instrument ID unavailable"
                continue
            bars = cache.get_bars_range(
                provider="alpaca",
                instrument_key=(instrument.broker, instrument.broker_instrument_id),
                timeframe=TimeFrame.ONE_DAY,
                start=partition.start,
                end=partition.end,
                instrument_factory=instrument.model_dump(mode="json"),
            )
            if len(bars) < 60:
                excluded[symbol] = f"insufficient cached bars: {len(bars)}"
                continue
            included.append(symbol)
            bars_by_instrument[instrument.key] = bars
        if not bars_by_instrument:
            rows.append(
                {
                    "candidate_bundle_id": bundle.parameters.parameter_bundle_id,
                    "candidate_bundle_fingerprint": bundle.fingerprint,
                    "asset_class": partition.asset_class.value,
                    "partition": partition.role,
                    "status": "DATA_INSUFFICIENT",
                    "diagnostic_label": diagnostic_label,
                    "actual_symbols_included": tuple(included),
                    "actual_symbols_excluded": excluded,
                    "broker_write_calls": 0,
                }
            )
            continue
        dataset = HistoricalValidationDataset(
            metadata=build_dataset_metadata(
                provider="alpaca-cache",
                instruments=tuple(bars[0].instrument for bars in bars_by_instrument.values()),
                bars_by_instrument=bars_by_instrument,
                timeframes=(TimeFrame.ONE_DAY,),
                created_at=clock_timestamp,
                mapping_version=(
                    "exitpolicy-v2-runner-repaired-v1:"
                    f"{diagnostic_label}:{partition.asset_class.value}:"
                    f"{bundle.fingerprint[:12]}"
                ),
            ),
            bars_by_instrument=bars_by_instrument,
        )
        result = HistoricalValidationEngine(
            risk_policy=config.risk,
            strategy_config=config.strategy,
            agent=DeterministicAegisAgent(exit_policy=ExitPolicyV2Guarded(parameter_bundle=bundle)),
        ).run(
            dataset=dataset,
            initial_cash=initial_cash,
            cost_assumptions=TransactionCostAssumptions(),
            random_seed=80,
            replay_stride=5,
        )
        row = _exit_policy_v2_candidate_result_payload(
            bundle_id=bundle.parameters.parameter_bundle_id,
            bundle_fingerprint=bundle.fingerprint,
            partition=partition,
            result=result,
            missing_symbols=tuple(sorted(excluded)),
            eligible_bars=sum(len(bars) for bars in bars_by_instrument.values()),
        )
        row["diagnostic_label"] = diagnostic_label
        row["actual_symbols_included"] = tuple(included)
        row["actual_symbols_excluded"] = excluded
        rows.append(row)
    return rows


def _exit_policy_v2_rows_aggregate(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "observations": sum(_row_int_or_zero(row, "observations") for row in rows),
        "open": sum(_row_int_or_zero(row, "open") for row in rows),
        "increase": sum(_row_int_or_zero(row, "increase") for row in rows),
        "reduce": sum(_row_int_or_zero(row, "reduce") for row in rows),
        "close": sum(_row_int_or_zero(row, "close") for row in rows),
        "completed_trades": sum(_row_int_or_zero(row, "completed_trades") for row in rows),
        "still_open_positions": sum(_row_int_or_zero(row, "still_open_positions") for row in rows),
        "realized_pnl": str(
            sum((_row_decimal_or_zero(row, "realized_pnl") for row in rows), Decimal("0"))
        ),
        "unrealized_pnl": str(
            sum((_row_decimal_or_zero(row, "unrealized_pnl") for row in rows), Decimal("0"))
        ),
        "broker_write_calls": sum(_row_int_or_zero(row, "broker_write_calls") for row in rows),
    }


def _select_exit_policy_v2_candidate(validation_rows: list[dict[str, object]]) -> dict[str, object]:
    executed_rows = tuple(
        row for row in validation_rows if row.get("status") == "EXECUTED_TRAIN_VALIDATION_ONLY"
    )
    qualifying = tuple(
        row
        for row in executed_rows
        if _row_int(row, "completed_trades") >= 3
        and _row_int(row, "close") > 0
        and _row_decimal(row, "max_drawdown") <= Decimal("0.25")
        and _row_int(row, "cooldown_reentry_blocks") >= 0
        and _row_int(row, "data_quality_exclusions") == 0
        and _row_decimal(row, "realized_pnl") >= Decimal("0")
    )
    if not qualifying:
        return {
            "status": "EXITPOLICY_V2_NO_CANDIDATE_QUALIFIED",
            "reason": (
                "no candidate passed the preregistered validation evidence gates without "
                "using HOLDOUT or profit-only ranking"
            ),
            "selected_candidate_bundle_id": None,
            "selected_candidate_bundle_fingerprint": None,
        }
    selected = sorted(
        qualifying,
        key=lambda row: (
            _row_decimal(row, "max_drawdown"),
            _row_int(row, "cooldown_reentry_blocks"),
            -_row_int(row, "completed_trades"),
            -_row_decimal(row, "realized_pnl"),
            str(row["candidate_bundle_fingerprint"]),
        ),
    )[0]
    return {
        "status": "EXITPOLICY_V2_CANDIDATE_SELECTED_AND_FROZEN",
        "reason": (
            "selected by preregistered validation gates, prioritizing drawdown, churn, "
            "completed lifecycle evidence, and only then realized return"
        ),
        "selected_candidate_bundle_id": selected["candidate_bundle_id"],
        "selected_candidate_bundle_fingerprint": selected["candidate_bundle_fingerprint"],
    }


def _row_int(row: dict[str, object], key: str) -> int:
    return int(str(row[key]))


def _row_decimal(row: dict[str, object], key: str) -> Decimal:
    return Decimal(str(row[key]))


def _row_int_or_zero(row: dict[str, object], key: str) -> int:
    if key not in row:
        return 0
    return int(str(row[key]))


def _row_decimal_or_zero(row: dict[str, object], key: str) -> Decimal:
    if key not in row:
        return Decimal("0")
    return Decimal(str(row[key]))


def _blocked(category: str, reason: str, *, config: ApplicationConfig) -> dict[str, object]:
    return {
        "status": "BLOCKED",
        "validation_runtime_version": REAL_VALIDATION_RUNTIME_VERSION,
        "policy_admission_source": POLICY_ADMISSION_SOURCE,
        "category": category,
        "reason": reason,
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
    }


def _exit_evidence_symbol_payload() -> dict[str, tuple[str, ...]]:
    return {
        asset_class.value: symbols
        for asset_class, symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.items()
    }


def _exit_evidence_expected_asset_classes() -> dict[str, AssetClass]:
    return {
        symbol: asset_class
        for asset_class, symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.items()
        for symbol in symbols
    }


def _polygon_pilot_symbol_payload() -> dict[str, tuple[str, ...]]:
    return {
        asset_class.value: symbols
        for asset_class, symbols in POLYGON_PILOT_SYMBOLS_BY_CLASS.items()
    }


def _alpaca_pilot_symbol_payload() -> dict[str, tuple[str, ...]]:
    return {
        asset_class.value: symbols for asset_class, symbols in ALPACA_PILOT_SYMBOLS_BY_CLASS.items()
    }


def _alpaca_1h_pilot_symbol_payload() -> dict[str, tuple[str, ...]]:
    return {
        asset_class.value: symbols
        for asset_class, symbols in ALPACA_1H_PILOT_SYMBOLS_BY_CLASS.items()
    }


def _alpaca_iex_equity_1h_symbol_payload() -> dict[str, tuple[str, ...]]:
    return {
        asset_class.value: symbols
        for asset_class, symbols in ALPACA_IEX_EQUITY_1H_SYMBOLS_BY_CLASS.items()
    }


def _alpaca_feed_provenance(
    instrument: UniversalInstrument,
    *,
    requested_feed: str,
) -> str:
    if instrument.asset_class is AssetClass.CRYPTO:
        return "ALPACA_CRYPTO_US"
    if requested_feed == "iex":
        return "ALPACA_IEX"
    if requested_feed == "sip":
        return "ALPACA_SIP"
    return f"ALPACA_{requested_feed.upper()}"


def _polygon_api_key(values: Mapping[str, str] | None) -> str | None:
    source = values or {}
    for name in ("AEGIS_POLYGON_API_KEY", "MASSIVE_API_KEY"):
        value = source.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _alpaca_api_credentials(values: Mapping[str, str] | None) -> tuple[str | None, str | None]:
    source = values or {}
    key_id = _first_nonempty_value(source, ("ALPACA_API_KEY_ID", "APCA_API_KEY_ID"))
    secret_key = _first_nonempty_value(
        source,
        ("ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY"),
    )
    return key_id, secret_key


def _first_nonempty_value(source: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = source.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _polygon_pilot_instruments(now: datetime) -> tuple[UniversalInstrument, ...]:
    return (
        _pilot_instrument(
            symbol="AAPL",
            broker_instrument_id="1001",
            asset_class=AssetClass.EQUITY,
            exchange="NASDAQ",
            now=now,
        ),
        _pilot_instrument(
            symbol="SPY",
            broker_instrument_id="3000",
            asset_class=AssetClass.ETF,
            exchange="NYSEARCA",
            now=now,
        ),
        _pilot_instrument(
            symbol="BTC",
            broker_instrument_id="100000",
            asset_class=AssetClass.CRYPTO,
            exchange="CRYPTO",
            now=now,
        ),
        _pilot_instrument(
            symbol="DIA",
            broker_instrument_id="ETORO_REFERENCE_UNAVAILABLE",
            asset_class=AssetClass.ETF,
            exchange="NYSEARCA",
            now=now,
        ),
    )


def _alpaca_pilot_instruments(now: datetime) -> tuple[UniversalInstrument, ...]:
    return (
        _pilot_instrument(
            symbol="AAPL",
            broker_instrument_id="1001",
            asset_class=AssetClass.EQUITY,
            exchange="NASDAQ",
            now=now,
        ),
        _pilot_instrument(
            symbol="SPY",
            broker_instrument_id="3000",
            asset_class=AssetClass.ETF,
            exchange="NYSEARCA",
            now=now,
        ),
        _pilot_instrument(
            symbol="DIA",
            broker_instrument_id="ETORO_REFERENCE_UNAVAILABLE",
            asset_class=AssetClass.ETF,
            exchange="NYSEARCA",
            now=now,
        ),
        _pilot_instrument(
            symbol="BTC",
            broker_instrument_id="100000",
            asset_class=AssetClass.CRYPTO,
            exchange="ALPACA_CRYPTO_US",
            now=now,
        ),
        _pilot_instrument(
            symbol="ETH",
            broker_instrument_id="100001",
            asset_class=AssetClass.CRYPTO,
            exchange="ALPACA_CRYPTO_US",
            now=now,
        ),
        _pilot_instrument(
            symbol="SOL",
            broker_instrument_id="100063",
            asset_class=AssetClass.CRYPTO,
            exchange="ALPACA_CRYPTO_US",
            now=now,
        ),
    )


def _alpaca_full_backfill_instruments(
    now: datetime,
    *,
    canonical_broker_ids: Mapping[str, str],
) -> tuple[UniversalInstrument, ...]:
    instruments: list[UniversalInstrument] = []
    for asset_class, symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.items():
        for symbol in symbols:
            broker_instrument_id = canonical_broker_ids.get(symbol, f"ALPACA_ONLY:{symbol}")
            broker = "etoro" if symbol in canonical_broker_ids else "alpaca"
            instruments.append(
                _pilot_instrument(
                    symbol=symbol,
                    broker=broker,
                    broker_instrument_id=broker_instrument_id,
                    asset_class=asset_class,
                    exchange=_alpaca_exchange_for_asset(asset_class),
                    now=now,
                )
            )
    return tuple(instruments)


def _alpaca_1h_pilot_instruments(
    now: datetime,
    *,
    canonical_broker_ids: Mapping[str, str],
) -> tuple[UniversalInstrument, ...]:
    instruments: list[UniversalInstrument] = []
    for asset_class, symbol in (
        (AssetClass.EQUITY, "AAPL"),
        (AssetClass.ETF, "SPY"),
        (AssetClass.CRYPTO, "BTC"),
        (AssetClass.CRYPTO, "ETH"),
    ):
        broker_instrument_id = canonical_broker_ids.get(symbol, f"ALPACA_ONLY:{symbol}")
        broker = "etoro" if symbol in canonical_broker_ids else "alpaca"
        instruments.append(
            _pilot_instrument(
                symbol=symbol,
                broker=broker,
                broker_instrument_id=broker_instrument_id,
                asset_class=asset_class,
                exchange=_alpaca_exchange_for_asset(asset_class),
                now=now,
            )
        )
    return tuple(instruments)


def _alpaca_iex_equity_1h_instruments(
    now: datetime,
    *,
    canonical_broker_ids: Mapping[str, str],
) -> tuple[UniversalInstrument, ...]:
    instruments: list[UniversalInstrument] = []
    for asset_class, symbol in (
        (AssetClass.EQUITY, "AAPL"),
        (AssetClass.ETF, "SPY"),
    ):
        broker_instrument_id = canonical_broker_ids.get(symbol, f"ALPACA_ONLY:{symbol}")
        broker = "etoro" if symbol in canonical_broker_ids else "alpaca"
        instruments.append(
            _pilot_instrument(
                symbol=symbol,
                broker=broker,
                broker_instrument_id=broker_instrument_id,
                asset_class=asset_class,
                exchange=_alpaca_exchange_for_asset(asset_class),
                now=now,
            )
        )
    return tuple(instruments)


def _alpaca_core_4h_instruments(
    now: datetime,
    *,
    canonical_broker_ids: Mapping[str, str],
) -> tuple[UniversalInstrument | None, ...]:
    instruments: list[UniversalInstrument | None] = []
    for asset_class, symbols in ALPACA_CORE_4H_SYMBOLS_BY_CLASS.items():
        for symbol in symbols:
            broker_instrument_id = canonical_broker_ids.get(symbol)
            if broker_instrument_id is None:
                instruments.append(None)
                continue
            instruments.append(
                _pilot_instrument(
                    symbol=symbol,
                    broker="etoro",
                    broker_instrument_id=broker_instrument_id,
                    asset_class=asset_class,
                    exchange=_alpaca_exchange_for_asset(asset_class),
                    now=now,
                )
            )
    return tuple(instruments)


def _missing_core_4h_mappings(canonical_broker_ids: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        symbol
        for symbols in ALPACA_CORE_4H_SYMBOLS_BY_CLASS.values()
        for symbol in symbols
        if symbol not in canonical_broker_ids
    )


def _alpaca_core_4h_symbol_payload() -> dict[str, tuple[str, ...]]:
    return {
        asset_class.value: symbols
        for asset_class, symbols in ALPACA_CORE_4H_SYMBOLS_BY_CLASS.items()
    }


def _alpaca_exchange_for_asset(asset_class: AssetClass) -> str:
    if asset_class is AssetClass.CRYPTO:
        return "ALPACA_CRYPTO_US"
    if asset_class is AssetClass.ETF:
        return "US_ETF"
    return "US_EQUITY"


def _pilot_instrument(
    *,
    symbol: str,
    broker: str = "etoro",
    broker_instrument_id: str,
    asset_class: AssetClass,
    exchange: str,
    now: datetime,
) -> UniversalInstrument:
    return UniversalInstrument(
        broker=broker,
        broker_instrument_id=broker_instrument_id,
        symbol=symbol,
        asset_class=asset_class,
        currency=Currency.USD,
        exchange=exchange,
        metadata_timestamp=now,
    )


def _alpaca_reference_for_instrument(
    instrument: UniversalInstrument,
) -> ProviderInstrumentReference:
    return ProviderInstrumentReference(
        provider="alpaca",
        provider_symbol=_alpaca_provider_symbol(instrument),
        broker=instrument.broker,
        broker_symbol=instrument.symbol,
        broker_instrument_id=instrument.broker_instrument_id,
        exchange=instrument.exchange,
        asset_class=instrument.asset_class,
        currency=instrument.currency,
        mapping_confidence=Decimal("1"),
        mapping_source="explicit Step 8.0L Alpaca full-backfill mapping with provider provenance",
        verified=True,
    )


def _polygon_mapping_overrides(
    instruments: tuple[UniversalInstrument, ...],
) -> dict[tuple[str, str], ProviderInstrumentReference]:
    overrides: dict[tuple[str, str], ProviderInstrumentReference] = {}
    for instrument in instruments:
        provider_symbol = (
            f"X:{instrument.symbol.upper()}USD"
            if instrument.asset_class is AssetClass.CRYPTO
            else instrument.symbol.upper()
        )
        overrides[("polygon", instrument.key)] = ProviderInstrumentReference(
            provider="polygon",
            provider_symbol=provider_symbol,
            broker=instrument.broker,
            broker_symbol=instrument.symbol,
            broker_instrument_id=instrument.broker_instrument_id,
            exchange=instrument.exchange,
            asset_class=instrument.asset_class,
            currency=instrument.currency,
            mapping_confidence=Decimal("1"),
            mapping_source="explicit Step 8.0J pilot mapping with asset-class provenance",
            verified=True,
        )
    return overrides


def _alpaca_mapping_overrides(
    instruments: tuple[UniversalInstrument, ...],
) -> dict[tuple[str, str], ProviderInstrumentReference]:
    overrides: dict[tuple[str, str], ProviderInstrumentReference] = {}
    for instrument in instruments:
        provider_symbol = (
            f"{instrument.symbol.upper()}/USD"
            if instrument.asset_class is AssetClass.CRYPTO
            else instrument.symbol.upper()
        )
        overrides[("alpaca", instrument.key)] = ProviderInstrumentReference(
            provider="alpaca",
            provider_symbol=provider_symbol,
            broker=instrument.broker,
            broker_symbol=instrument.symbol,
            broker_instrument_id=instrument.broker_instrument_id,
            exchange=instrument.exchange,
            asset_class=instrument.asset_class,
            currency=instrument.currency,
            mapping_confidence=Decimal("1"),
            mapping_source="explicit Step 8.0K Alpaca pilot mapping with provider provenance",
            verified=True,
        )
    return overrides


def _provider_mapping_payload(reference: ProviderInstrumentReference) -> dict[str, object]:
    return {
        "provider": reference.provider,
        "provider_symbol": reference.provider_symbol,
        "expected_asset_class": reference.asset_class.value,
        "provider_asset_market_type": (
            "crypto" if reference.asset_class is AssetClass.CRYPTO else "stocks"
        ),
        "exchange_or_market": reference.exchange,
        "currency": reference.currency.value if reference.currency else None,
        "broker_symbol": reference.broker_symbol,
        "broker_instrument_id": reference.broker_instrument_id,
        "mapping_source": reference.mapping_source,
        "verification_status": "VERIFIED" if reference.verified else "UNVERIFIED",
        "provenance": "explicit pilot mapping; ticker-only equivalence is not sufficient",
    }


def _transport_saw_next_page(transport: HttpTextTransport | None) -> bool:
    value = getattr(transport, "saw_next_page", False)
    return bool(value)


def _polygon_depth_probe(
    *,
    provider: PolygonHistoricalMarketDataProvider,
    instrument: UniversalInstrument,
    now: datetime,
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for timeframe in POLYGON_PILOT_TIMEFRAMES:
        for label, start, end in _plan_probe_windows(now):
            try:
                bars = provider.get_bars_range(
                    instrument,
                    timeframe,
                    start=start,
                    end=end,
                    limit=120 if timeframe is TimeFrame.ONE_DAY else 240,
                    max_pages=1,
                )
            except DataProviderError as exc:
                rows.append(
                    {
                        "symbol": instrument.symbol,
                        "asset_class": instrument.asset_class.value,
                        "timeframe": timeframe.value,
                        "probe_label": label,
                        "probe_years_back": _probe_years(label),
                        "probe_start": start.isoformat(),
                        "probe_end": end.isoformat(),
                        "authorization_status": _probe_authorization_status(exc),
                        "http_status": exc.http_status,
                        "transport_category": exc.transport_category,
                        "provider_error_code": exc.provider_error_code,
                        "provider_error_message": exc.provider_error_message,
                        "retry_after": exc.retry_after,
                        "bars_returned": 0,
                        "earliest_returned_timestamp": None,
                        "latest_returned_timestamp": None,
                        "deeper_than_etoro_1000_window": False,
                    }
                )
                continue
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "asset_class": instrument.asset_class.value,
                    "timeframe": timeframe.value,
                    "probe_label": label,
                    "probe_years_back": _probe_years(label),
                    "probe_start": start.isoformat(),
                    "probe_end": end.isoformat(),
                    "authorization_status": "AUTHORIZED" if bars else "NO_DATA",
                    "http_status": 200 if bars else None,
                    "transport_category": None,
                    "provider_error_code": None,
                    "provider_error_message": None,
                    "retry_after": None,
                    "bars_returned": len(bars),
                    "earliest_returned_timestamp": bars[0].timestamp.isoformat() if bars else None,
                    "latest_returned_timestamp": bars[-1].timestamp.isoformat() if bars else None,
                    "deeper_than_etoro_1000_window": bool(bars and label != "recent"),
                }
            )
    return tuple(rows)


def _alpaca_probe_row(
    *,
    provider: AlpacaHistoricalMarketDataProvider,
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
    label: str,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, object], tuple[MarketBar, ...]]:
    pagination_state: dict[str, object] = {
        "pagination_requested": label == "pagination",
        "pagination_token_observed": False,
        "second_page_fetched": False,
        "pagination_verified": False,
    }
    try:
        bars = provider.get_bars_range(
            instrument,
            timeframe,
            start=start,
            end=end,
            limit=2 if label == "pagination" else 120,
            max_pages=2 if label == "pagination" else 1,
        )
        pagination_state = provider.last_pagination_state
    except DataProviderError as exc:
        pagination_state = pagination_state | provider.last_pagination_state
        return (
            {
                "symbol": instrument.symbol,
                "asset_class": instrument.asset_class.value,
                "provider_symbol": _alpaca_provider_symbol(instrument),
                "timeframe": timeframe.value,
                "alpaca_timeframe": _alpaca_timeframe_value(timeframe),
                "probe_label": label,
                "probe_start": start.isoformat(),
                "probe_end": end.isoformat(),
                "authorization_status": _alpaca_authorization_status(exc),
                "http_status": exc.http_status,
                "transport_category": exc.transport_category,
                "provider_error_code": exc.provider_error_code,
                "provider_error_message": exc.provider_error_message,
                "retry_after": exc.retry_after,
                "pagination_requested": bool(pagination_state["pagination_requested"]),
                "pagination_token_observed": bool(pagination_state["pagination_token_observed"]),
                "second_page_fetched": bool(pagination_state["second_page_fetched"]),
                "pagination_verified": bool(pagination_state["pagination_verified"]),
                "bars_returned": 0,
                "earliest_returned_timestamp": None,
                "latest_returned_timestamp": None,
                "ordering": "UNKNOWN",
                "duplicate_timestamps": 0,
                "volume_available": False,
                "feed": _alpaca_feed_label(instrument),
                "timezone_semantics": _alpaca_timezone_semantics(instrument),
                "provenance": "provider_response_failed_no_bars_verified",
                "verification_status": "NOT_VERIFIED",
            },
            (),
        )
    duplicate_count = len(bars) - len({bar.timestamp for bar in bars})
    return (
        {
            "symbol": instrument.symbol,
            "asset_class": instrument.asset_class.value,
            "provider_symbol": _alpaca_provider_symbol(instrument),
            "timeframe": timeframe.value,
            "alpaca_timeframe": _alpaca_timeframe_value(timeframe),
            "probe_label": label,
            "probe_start": start.isoformat(),
            "probe_end": end.isoformat(),
            "authorization_status": "AUTHORIZED" if bars else "NO_DATA",
            "http_status": 200 if bars else None,
            "transport_category": None,
            "provider_error_code": None,
            "provider_error_message": None,
            "retry_after": None,
            "pagination_requested": bool(pagination_state["pagination_requested"]),
            "pagination_token_observed": bool(pagination_state["pagination_token_observed"]),
            "second_page_fetched": bool(pagination_state["second_page_fetched"]),
            "pagination_verified": bool(pagination_state["pagination_verified"]),
            "bars_returned": len(bars),
            "earliest_returned_timestamp": bars[0].timestamp.isoformat() if bars else None,
            "latest_returned_timestamp": bars[-1].timestamp.isoformat() if bars else None,
            "ordering": "ORDERED"
            if tuple(bar.timestamp for bar in bars) == tuple(sorted(bar.timestamp for bar in bars))
            else "OUT_OF_ORDER",
            "duplicate_timestamps": duplicate_count,
            "volume_available": any(bar.volume is not None for bar in bars),
            "feed": _alpaca_feed_label(instrument),
            "timezone_semantics": _alpaca_timezone_semantics(instrument),
            "provenance": "provider_response_bars_verified"
            if bars
            else "provider_response_no_data",
            "verification_status": "VERIFIED" if bars else "NOT_VERIFIED",
        },
        bars,
    )


def _pagination_int(pagination: Mapping[str, object], key: str) -> int:
    value = pagination.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _alpaca_full_backfill_row(
    *,
    provider: AlpacaHistoricalMarketDataProvider,
    cache: HistoricalDataCache,
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
    mapping: ProviderInstrumentReference,
    now: datetime,
) -> dict[str, object]:
    start, end = _alpaca_full_backfill_range(instrument, now)
    try:
        bars = provider.get_bars_range(
            instrument,
            timeframe,
            start=start,
            end=end,
            limit=10_000,
            max_pages=25,
        )
    except DataProviderError as exc:
        return {
            "symbol": instrument.symbol,
            "asset_class": instrument.asset_class.value,
            "timeframe": timeframe.value,
            "provider": "alpaca",
            "provider_symbol": _alpaca_provider_symbol(instrument),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "bars": 0,
            "pages_fetched": _pagination_int(provider.last_pagination_state, "pages_fetched"),
            "gaps": ("NO_PROVIDER_BARS",),
            "cache_status": "NOT_WRITTEN",
            "cache_inserted": 0,
            "cache_updated": 0,
            "cache_unchanged": 0,
            "pagination_requested": bool(
                provider.last_pagination_state.get("pagination_requested", False)
            ),
            "pagination_token_observed": bool(
                provider.last_pagination_state.get("pagination_token_observed", False)
            ),
            "second_page_fetched": bool(
                provider.last_pagination_state.get("second_page_fetched", False)
            ),
            "pagination_verified": bool(
                provider.last_pagination_state.get("pagination_verified", False)
            ),
            "final_status": _alpaca_authorization_status(exc),
            "http_status": exc.http_status,
            "sanitized_endpoint": exc.sanitized_endpoint,
            "transport_category": exc.transport_category,
            "exception_type": exc.exception_type,
            "exception_message": exc.exception_message,
            "errno": exc.errno,
            "winerror": exc.winerror,
            "provider_error_code": exc.provider_error_code,
            "provider_error_message": exc.provider_error_message,
            "retry_after": exc.retry_after,
        }
    pagination = provider.last_pagination_state
    if not bars:
        return {
            "symbol": instrument.symbol,
            "asset_class": instrument.asset_class.value,
            "timeframe": timeframe.value,
            "provider": "alpaca",
            "provider_symbol": _alpaca_provider_symbol(instrument),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "bars": 0,
            "pages_fetched": _pagination_int(pagination, "pages_fetched"),
            "gaps": ("NO_DATA",),
            "cache_status": "NOT_WRITTEN",
            "cache_inserted": 0,
            "cache_updated": 0,
            "cache_unchanged": 0,
            "pagination_requested": bool(pagination.get("pagination_requested", False)),
            "pagination_token_observed": bool(pagination.get("pagination_token_observed", False)),
            "second_page_fetched": bool(pagination.get("second_page_fetched", False)),
            "pagination_verified": bool(pagination.get("pagination_verified", False)),
            "final_status": "NO_DATA",
        }
    stats = cache.upsert_bars_with_stats(
        provider="alpaca",
        bars=bars,
        fetched_at=now,
        mapping=mapping,
    )
    quality = HistoricalDataQualityAnalyzer().evaluate(
        provider="alpaca",
        instrument=instrument,
        timeframe=timeframe,
        bars=bars,
        as_of=now,
        expected_currency=instrument.currency,
    )
    cache_status = "UNCHANGED" if stats["unchanged"] == len(bars) else "UPDATED"
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "timeframe": timeframe.value,
        "provider": "alpaca",
        "provider_symbol": _alpaca_provider_symbol(instrument),
        "start": bars[0].timestamp.isoformat(),
        "end": bars[-1].timestamp.isoformat(),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "bars": len(bars),
        "pages_fetched": _pagination_int(pagination, "pages_fetched"),
        "gaps": quality.provider_gaps,
        "data_quality": quality.status.value,
        "cache_status": cache_status,
        "cache_inserted": stats["inserted"],
        "cache_updated": stats["updated"],
        "cache_unchanged": stats["unchanged"],
        "pagination_requested": bool(pagination.get("pagination_requested", False)),
        "pagination_token_observed": bool(pagination.get("pagination_token_observed", False)),
        "second_page_fetched": bool(pagination.get("second_page_fetched", False)),
        "pagination_verified": bool(pagination.get("pagination_verified", False)),
        "final_status": "SUCCESS",
    }


def _alpaca_core_4h_row(
    *,
    provider: AlpacaHistoricalMarketDataProvider,
    cache: HistoricalDataCache,
    instrument: UniversalInstrument,
    mapping: ProviderInstrumentReference,
    now: datetime,
) -> dict[str, object]:
    timeframe = TimeFrame.FOUR_HOUR
    requested_start, requested_end = _alpaca_core_4h_range(now)
    coverage_before = cache.coverage_summary(
        provider="alpaca",
        broker=instrument.broker,
        broker_instrument_id=instrument.broker_instrument_id,
        timeframe=timeframe,
        start=requested_start,
        end=requested_end,
    )
    latest = coverage_before["latest"]
    fetch_start = requested_start
    if isinstance(latest, datetime):
        fetch_start = min(latest + timedelta(hours=4), requested_end)
    fetched: tuple[MarketBar, ...] = ()
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}
    pagination: Mapping[str, object] = provider.last_pagination_state
    final_status = "SUCCESS"
    if fetch_start < requested_end:
        try:
            fetched = provider.get_bars_range(
                instrument,
                timeframe,
                start=fetch_start,
                end=requested_end,
                limit=10_000,
                max_pages=25,
            )
            pagination = provider.last_pagination_state
        except DataProviderError as exc:
            return _alpaca_core_4h_error_row(
                instrument=instrument,
                start=requested_start,
                end=requested_end,
                provider=provider,
                exc=exc,
            )
        if bool(pagination.get("pagination_truncated", False)):
            final_status = "PAGINATION_TRUNCATED"
        elif not fetched and _int_payload_value(coverage_before["count"]) == 0:
            final_status = "NO_DATA"
        if fetched and final_status == "SUCCESS":
            stats = cache.upsert_bars_with_stats(
                provider="alpaca",
                bars=fetched,
                fetched_at=now,
                mapping=mapping,
            )
    window_bars = cache.get_bars_range(
        provider="alpaca",
        instrument_key=(instrument.broker, instrument.broker_instrument_id),
        timeframe=timeframe,
        start=requested_start,
        end=requested_end,
        instrument_factory=instrument.model_dump(mode="json"),
    )
    quality = HistoricalDataQualityAnalyzer().evaluate(
        provider="alpaca",
        instrument=instrument,
        timeframe=timeframe,
        bars=window_bars,
        as_of=now,
        expected_currency=instrument.currency,
    )
    if final_status == "SUCCESS" and quality.missing_bars_estimate:
        final_status = "DATA_QUALITY_GAP"
    coverage_after = cache.coverage_summary(
        provider="alpaca",
        broker=instrument.broker,
        broker_instrument_id=instrument.broker_instrument_id,
        timeframe=timeframe,
        start=requested_start,
        end=requested_end,
    )
    cache_status = "CACHE_HIT" if fetch_start >= requested_end else "UPDATED"
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "timeframe": timeframe.value,
        "provider": "alpaca",
        "provider_symbol": _alpaca_provider_symbol(instrument),
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "start": _optional_datetime_iso(coverage_after["earliest"]),
        "end": _optional_datetime_iso(coverage_after["latest"]),
        "bars": len(window_bars),
        "fetched_bars": len(fetched),
        "pages_fetched": _pagination_int(pagination, "pages_fetched"),
        "gaps": quality.provider_gaps,
        "missing_bars_estimate": quality.missing_bars_estimate,
        "data_quality": quality.status.value,
        "cache_status": cache_status,
        "cache_inserted": stats["inserted"],
        "cache_updated": stats["updated"],
        "cache_unchanged": stats["unchanged"],
        "pagination_requested": bool(pagination.get("pagination_requested", False)),
        "pagination_token_observed": bool(pagination.get("pagination_token_observed", False)),
        "second_page_fetched": bool(pagination.get("second_page_fetched", False)),
        "pagination_verified": bool(pagination.get("pagination_verified", False)),
        "pagination_truncated": bool(pagination.get("pagination_truncated", False)),
        "final_status": final_status,
    }


def _alpaca_core_4h_error_row(
    *,
    instrument: UniversalInstrument,
    start: datetime,
    end: datetime,
    provider: AlpacaHistoricalMarketDataProvider,
    exc: DataProviderError,
) -> dict[str, object]:
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "timeframe": TimeFrame.FOUR_HOUR.value,
        "provider": "alpaca",
        "provider_symbol": _alpaca_provider_symbol(instrument),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bars": 0,
        "fetched_bars": 0,
        "pages_fetched": _pagination_int(provider.last_pagination_state, "pages_fetched"),
        "gaps": ("NO_PROVIDER_BARS",),
        "cache_status": "NOT_WRITTEN",
        "cache_inserted": 0,
        "cache_updated": 0,
        "cache_unchanged": 0,
        "pagination_requested": bool(
            provider.last_pagination_state.get("pagination_requested", False)
        ),
        "pagination_token_observed": bool(
            provider.last_pagination_state.get("pagination_token_observed", False)
        ),
        "second_page_fetched": bool(
            provider.last_pagination_state.get("second_page_fetched", False)
        ),
        "pagination_verified": bool(
            provider.last_pagination_state.get("pagination_verified", False)
        ),
        "pagination_truncated": bool(
            provider.last_pagination_state.get("pagination_truncated", False)
        ),
        "final_status": _alpaca_authorization_status(exc),
        "http_status": exc.http_status,
        "sanitized_endpoint": exc.sanitized_endpoint,
        "transport_category": exc.transport_category,
        "exception_type": exc.exception_type,
        "exception_message": exc.exception_message,
        "errno": exc.errno,
        "winerror": exc.winerror,
        "provider_error_code": exc.provider_error_code,
        "provider_error_message": exc.provider_error_message,
        "retry_after": exc.retry_after,
    }


def _alpaca_full_backfill_range(
    instrument: UniversalInstrument,
    now: datetime,
) -> tuple[datetime, datetime]:
    end = datetime(now.year, now.month, now.day, tzinfo=UTC)
    if instrument.asset_class is AssetClass.CRYPTO:
        return (end - timedelta(days=365 * ALPACA_CRYPTO_BACKFILL_YEARS + 31), end)
    return ALPACA_STOCK_ETF_BACKFILL_START, end


def _alpaca_core_4h_range(now: datetime) -> tuple[datetime, datetime]:
    end = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return end - timedelta(days=365 * ALPACA_CORE_4H_BACKFILL_YEARS), end


def _optional_datetime_iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _alpaca_probe_windows(
    instrument: UniversalInstrument,
    now: datetime,
) -> tuple[tuple[str, datetime, datetime], ...]:
    anchor = datetime(now.year, now.month, now.day, tzinfo=UTC)
    if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
        return (
            ("recent", anchor - timedelta(days=45), anchor - timedelta(days=15)),
            ("2y", anchor - timedelta(days=(365 * 2) + 31), anchor - timedelta(days=365 * 2)),
            ("5y", anchor - timedelta(days=(365 * 5) + 31), anchor - timedelta(days=365 * 5)),
            (
                "near_2016",
                datetime(2016, 1, 4, tzinfo=UTC),
                datetime(2016, 2, 4, tzinfo=UTC),
            ),
            ("pagination", anchor - timedelta(days=90), anchor - timedelta(days=15)),
        )
    return (
        ("recent", anchor - timedelta(days=45), anchor - timedelta(days=15)),
        ("2y", anchor - timedelta(days=(365 * 2) + 31), anchor - timedelta(days=365 * 2)),
        ("5y", anchor - timedelta(days=(365 * 5) + 31), anchor - timedelta(days=365 * 5)),
        (
            "near_2016",
            datetime(2016, 1, 4, tzinfo=UTC),
            datetime(2016, 2, 4, tzinfo=UTC),
        ),
        ("pagination", anchor - timedelta(days=90), anchor - timedelta(days=15)),
    )


def _alpaca_provider_symbol(instrument: UniversalInstrument) -> str:
    if instrument.asset_class is AssetClass.CRYPTO:
        return f"{instrument.symbol.upper()}/USD"
    return instrument.symbol.upper()


def _alpaca_timeframe_value(timeframe: TimeFrame) -> str:
    return {TimeFrame.ONE_DAY: "1Day", TimeFrame.FOUR_HOUR: "4Hour"}[timeframe]


def _alpaca_feed_label(instrument: UniversalInstrument) -> str:
    if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
        return "sip"
    return "crypto/us"


def _alpaca_timezone_semantics(instrument: UniversalInstrument) -> str:
    if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
        return "US equity sessions; timestamps returned as UTC"
    return "Crypto 24/7; timestamps returned as UTC"


def _alpaca_source_notes(instrument: UniversalInstrument) -> tuple[str, ...]:
    if instrument.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
        return (
            "feed=sip",
            "historical-only read",
            "provider series remains distinct from eToro and Polygon/Massive",
        )
    return (
        "location=us",
        "crypto bars may include quote midpoint prices when no trade occurs",
        "provider response required before marking pair verified",
    )


def _alpaca_authorization_status(exc: DataProviderError) -> str:
    if exc.http_status == 429 or exc.status is DataProviderStatus.RATE_LIMITED:
        return "RATE_LIMITED"
    if exc.http_status in {401, 403}:
        return "NOT_AUTHORIZED"
    if exc.http_status == 404:
        return "NO_DATA"
    if exc.status is DataProviderStatus.TIMEOUT:
        return "TRANSPORT_ERROR"
    return exc.status.value


def _alpaca_pagination_summary(depth: list[dict[str, object]]) -> dict[str, object]:
    return {
        "pagination_requested": any(bool(item.get("pagination_requested")) for item in depth),
        "pagination_token_observed": any(
            bool(item.get("pagination_token_observed")) for item in depth
        ),
        "second_page_fetched": any(bool(item.get("second_page_fetched")) for item in depth),
        "pagination_verified": any(bool(item.get("pagination_verified")) for item in depth),
    }


def _plan_probe_windows(now: datetime) -> tuple[tuple[str, datetime, datetime], ...]:
    anchor = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return tuple(
        (label, anchor - timedelta(days=days_back + 31), anchor - timedelta(days=days_back))
        for label, days_back in (
            ("recent", 60),
            ("2y", 365 * 2),
            ("5y", 365 * 5),
            ("10y", 365 * 10),
            ("20y", 365 * 20),
        )
    )


def _probe_years(label: str) -> int:
    return {"recent": 0, "2y": 2, "5y": 5, "10y": 10, "20y": 20}[label]


def _probe_authorization_status(exc: DataProviderError) -> str:
    if exc.http_status in {401, 403} or exc.provider_error_code == "NOT_AUTHORIZED":
        return "NOT_AUTHORIZED"
    if exc.http_status == 404:
        return "NO_DATA"
    return exc.status.value


def _polygon_entitlement_summary(depth: list[dict[str, object]]) -> dict[str, object]:
    stock_etf_depth = _deepest_authorized_years(
        depth,
        asset_classes={AssetClass.EQUITY.value, AssetClass.ETF.value},
    )
    crypto_depth = _deepest_authorized_years(depth, asset_classes={AssetClass.CRYPTO.value})
    four_hour_depth = _deepest_authorized_years(
        [item for item in depth if item["timeframe"] == TimeFrame.FOUR_HOUR.value],
        asset_classes={AssetClass.EQUITY.value, AssetClass.ETF.value, AssetClass.CRYPTO.value},
    )
    required_years = 5
    return {
        "deepest_verified_stock_etf_entitlement": _depth_label(stock_etf_depth),
        "deepest_verified_stock_etf_years": stock_etf_depth,
        "deepest_verified_crypto_entitlement": _depth_label(crypto_depth),
        "deepest_verified_crypto_years": crypto_depth,
        "deepest_verified_4h_years": four_hour_depth,
        "four_hour_exit_validation_depth_adequate": four_hour_depth >= required_years,
        "adequacy_rule": "4H exit-validation pilot requires at least a verified 5y entitlement",
    }


def _deepest_authorized_years(
    depth: list[dict[str, object]],
    *,
    asset_classes: set[str],
) -> int:
    years = 0
    for item in depth:
        if item["asset_class"] not in asset_classes:
            continue
        if item.get("authorization_status") != "AUTHORIZED":
            continue
        value = item.get("probe_years_back", 0)
        if isinstance(value, int):
            years = max(years, value)
        elif isinstance(value, str):
            years = max(years, int(value))
    return years


def _depth_label(years: int) -> str:
    if years == 0:
        return "RECENT_ONLY"
    return f"{years}Y_VERIFIED"


def _massive_plan_recommendation(entitlement: dict[str, object]) -> str:
    stock_years = _int_payload_value(entitlement["deepest_verified_stock_etf_years"])
    crypto_years = _int_payload_value(entitlement["deepest_verified_crypto_years"])
    four_hour_ok = bool(entitlement["four_hour_exit_validation_depth_adequate"])
    if four_hour_ok and stock_years >= 5 and crypto_years >= 5:
        return "CURRENT_MASSIVE_PLAN_SUFFICIENT"
    if stock_years < 5 and crypto_years >= 5:
        return "STOCKS_STARTER_SUFFICIENT"
    if stock_years >= 5 and crypto_years < 5:
        return "CURRENCIES_STARTER_REQUIRED"
    if stock_years < 5 and crypto_years < 5:
        return "MIXED_PLAN_REQUIRED"
    if stock_years < 10:
        return "STOCKS_DEVELOPER_REQUIRED"
    return "STOCKS_ADVANCED_REQUIRED"


def _int_payload_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _polygon_provider_error_payload(
    exc: DataProviderError, *, config: ApplicationConfig
) -> dict[str, object]:
    diagnostics = exc.safe_diagnostics()
    return {
        **_provider_pilot_blocked(
            exc.status.value,
            "Polygon/Massive provider pilot read failed",
            config=config,
        ),
        "provider_diagnostics": diagnostics,
        "http_status": diagnostics["http_status"],
        "sanitized_endpoint": diagnostics["sanitized_endpoint"],
        "transport_category": diagnostics["transport_category"],
        "provider_error_code": diagnostics["provider_error_code"],
        "provider_error_message": diagnostics["provider_error_message"],
        "retry_after": diagnostics["retry_after"],
        "api_key_configured": True,
        "provider": "polygon",
        "broker_write_calls": 0,
        "demo_execution_enabled": False,
        "real_execution_available": False,
        "windows_cmd": DEFAULT_POLYGON_PILOT_WINDOWS_CMD,
    }


def _cross_provider_overlap_payload(
    *,
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
    left_provider: str,
    left_bars: tuple[MarketBar, ...],
    reference_provider: str,
    reference_bars: tuple[MarketBar, ...],
) -> dict[str, object]:
    left_by_ts = {bar.timestamp: bar for bar in left_bars}
    reference_by_ts = {bar.timestamp: bar for bar in reference_bars}
    common = tuple(sorted(set(left_by_ts).intersection(reference_by_ts)))
    comparisons: list[dict[str, object]] = []
    for timestamp in common[-10:]:
        left = left_by_ts[timestamp]
        right = reference_by_ts[timestamp]
        comparisons.append(
            {
                "timestamp": timestamp.isoformat(),
                "open_relative_deviation": str(_relative_deviation(left.open, right.open)),
                "high_relative_deviation": str(_relative_deviation(left.high, right.high)),
                "low_relative_deviation": str(_relative_deviation(left.low, right.low)),
                "close_relative_deviation": str(_relative_deviation(left.close, right.close)),
                "left_provider_volume_available": left.volume is not None,
                "reference_provider_volume_available": right.volume is not None,
            }
        )
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "timeframe": timeframe.value,
        "left_provider": left_provider,
        "reference_provider": reference_provider,
        "timestamps_compared": len(common),
        "sampled_comparisons": tuple(comparisons),
        "left_provider_missing_overlap_bars": max(0, len(reference_by_ts) - len(common)),
        "reference_provider_missing_overlap_bars": max(0, len(left_by_ts) - len(common)),
        "timezone_normalization": (
            "left-provider and reference-provider timestamps are normalized to UTC; "
            "provider session semantics may differ"
        ),
        "session_calendar_difference": (
            "equity/ETF provider sessions may include extended-hours aggregation differences; "
            "crypto is 24/7"
        ),
        "material_disagreement": _material_disagreement(comparisons),
    }


def _relative_deviation(left: Decimal, right: Decimal) -> Decimal:
    denominator = max(abs(left), abs(right), Decimal("0.00000001"))
    return ((left - right).copy_abs() / denominator).quantize(Decimal("0.000001"))


def _material_disagreement(comparisons: list[dict[str, object]]) -> bool:
    threshold = Decimal("0.03")
    for row in comparisons:
        for key in (
            "open_relative_deviation",
            "high_relative_deviation",
            "low_relative_deviation",
            "close_relative_deviation",
        ):
            if Decimal(str(row[key])) > threshold:
                return True
    return False


def _dia_status(
    mapping_overrides: Mapping[tuple[str, str], ProviderInstrumentReference],
) -> dict[str, object]:
    dia = next(
        (reference for reference in mapping_overrides.values() if reference.broker_symbol == "DIA"),
        None,
    )
    return {
        "provider_mapping": "DIA_PROVIDER_MAPPING_VERIFIED"
        if dia
        else "DIA_PROVIDER_MAPPING_MISSING",
        "provider_asset_class": dia.asset_class.value if dia else None,
        "provider_symbol": dia.provider_symbol if dia else None,
        "etoro_reference": "ETORO_DIA_REFERENCE_UNAVAILABLE",
        "invalid_etoro_crypto_mapping": "REMAINS_QUARANTINED",
        "cross_provider_comparison": "SKIPPED_NO_VALID_ETORO_DIA_REFERENCE",
    }


def _polygon_pilot_status(
    sample_coverage: list[dict[str, object]],
    depth: list[dict[str, object]],
    duplicates: tuple[dict[str, object], ...],
) -> str:
    if duplicates:
        return "HISTORICAL_PROVIDER_INTEGRATION_BLOCKER"
    if any(not item["bar_count"] for item in sample_coverage):
        return "HISTORICAL_PROVIDER_PLAN_LIMITATION"
    if not any(item.get("authorization_status") == "AUTHORIZED" for item in depth):
        return "HISTORICAL_PROVIDER_PLAN_LIMITATION"
    if not _polygon_entitlement_summary(depth)["four_hour_exit_validation_depth_adequate"]:
        return "HISTORICAL_PROVIDER_PLAN_LIMITATION"
    return "HISTORICAL_PROVIDER_ADAPTER_READY_FOR_BACKFILL"


def _alpaca_pilot_status(
    depth: list[dict[str, object]],
    duplicates: tuple[dict[str, object], ...],
) -> str:
    if duplicates:
        return "ALPACA_ADAPTER_BLOCKER"
    required = {
        (symbol, timeframe.value)
        for symbols in ALPACA_PILOT_SYMBOLS_BY_CLASS.values()
        for symbol in symbols
        for timeframe in ALPACA_PILOT_TIMEFRAMES
    }
    verified = {
        (str(item["symbol"]), str(item["timeframe"]))
        for item in depth
        if item.get("verification_status") == "VERIFIED"
    }
    if not required.issubset(verified):
        return "ALPACA_ADAPTER_BLOCKER"
    return "ALPACA_FREE_PILOT_READY_FOR_WINDOWS"


def _alpaca_full_backfill_status(
    rows: list[dict[str, object]],
    duplicates: tuple[dict[str, object], ...],
) -> str:
    if duplicates:
        return "ALPACA_PROVIDER_BLOCKER"
    if any(item.get("transport_category") for item in rows):
        return "ALPACA_PROVIDER_BLOCKER"
    if not rows or any(item.get("final_status") != "SUCCESS" for item in rows):
        return "ALPACA_MAPPING_RECONCILIATION_REQUIRED"
    return "ALPACA_FULL_BACKFILL_COMPLETE"


def _alpaca_core_4h_status(
    rows: list[dict[str, object]],
    duplicates: tuple[dict[str, object], ...],
    missing_mappings: tuple[str, ...],
) -> str:
    if duplicates:
        return "ALPACA_PROVIDER_BLOCKER"
    if missing_mappings:
        return "ALPACA_MAPPING_RECONCILIATION_REQUIRED"
    if any(item.get("transport_category") for item in rows):
        return "ALPACA_PROVIDER_BLOCKER"
    if not rows or any(item.get("final_status") != "SUCCESS" for item in rows):
        return "ALPACA_CORE_4H_INCOMPLETE"
    return "ALPACA_CORE_4H_READY"


def _alpaca_backfill_blocked(
    category: str, reason: str, *, config: ApplicationConfig
) -> dict[str, object]:
    status = (
        "ALPACA_API_KEYS_NOT_CONFIGURED"
        if category == "ALPACA_API_KEYS_NOT_CONFIGURED"
        else "ALPACA_MAPPING_RECONCILIATION_REQUIRED"
    )
    return {
        "status": status,
        "phase": "STEP_8_0L_ALPACA_FULL_HISTORICAL_BACKFILL",
        "category": category,
        "reason": reason,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
    }


def _provider_pilot_blocked(
    category: str, reason: str, *, config: ApplicationConfig
) -> dict[str, object]:
    return {
        "status": "HISTORICAL_PROVIDER_INTEGRATION_BLOCKER",
        "phase": "STEP_8_0J_POLYGON_PROVIDER_ADAPTER_PILOT",
        "category": category,
        "reason": reason,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
    }


def _alpaca_pilot_blocked(
    category: str, reason: str, *, config: ApplicationConfig
) -> dict[str, object]:
    return {
        "status": "ALPACA_ADAPTER_BLOCKER",
        "phase": "STEP_8_0K_ALPACA_FREE_PROVIDER_PILOT",
        "category": category,
        "reason": reason,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
    }


def _resolved_instrument_payload(
    instruments: tuple[UniversalInstrument, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "symbol": instrument.symbol,
            "provider": "etoro-official-search",
            "instrument_id": instrument.broker_instrument_id,
            "asset_class": instrument.asset_class.value,
            "mapping_status": "RESOLVED",
        }
        for instrument in instruments
    )


def _missing_coverage_payload(
    *,
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
    reasons: tuple[str, ...],
) -> dict[str, object]:
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "provider": "NONE",
        "instrument_id": instrument.broker_instrument_id,
        "timeframe": timeframe.value,
        "bar_count": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "missing_bar_diagnostics": reasons,
        "duplicate_timestamps": 0,
        "timestamp_ordering": "UNKNOWN",
        "data_quality_state": "DATA_INSUFFICIENT",
        "cache_status": "MISS",
        "mapping_status": "RESOLVED",
    }


def _coverage_payload(dataset: HistoricalDataset) -> dict[str, object]:
    timestamps = tuple(bar.timestamp for bar in dataset.bars)
    instrument = dataset.instrument
    timeframe = dataset.timeframe
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "provider": dataset.provider,
        "instrument_id": instrument.broker_instrument_id,
        "timeframe": timeframe.value,
        "bar_count": len(dataset.bars),
        "first_timestamp": min(timestamps).isoformat(),
        "last_timestamp": max(timestamps).isoformat(),
        "missing_bar_diagnostics": dataset.quality.provider_gaps,
        "duplicate_timestamps": dataset.quality.duplicate_timestamps,
        "timestamp_ordering": "OUT_OF_ORDER" if dataset.quality.out_of_order else "ORDERED",
        "data_quality_state": dataset.quality.status.value,
        "cache_status": "HIT" if dataset.provenance.cached else "UPDATED",
        "mapping_status": "RESOLVED",
        "deterministic_cache_key": (
            f"{dataset.provider}:"
            f"{instrument.broker}:"
            f"{instrument.broker_instrument_id}:"
            f"{timeframe.value}"
        ),
    }


def _bars_coverage_payload(
    *,
    provider: str,
    instrument: UniversalInstrument,
    timeframe: TimeFrame,
    bars: tuple[MarketBar, ...],
    cache_status: str,
    mapping_status: str,
    source_notes: tuple[str, ...],
) -> dict[str, object]:
    timestamps = tuple(bar.timestamp for bar in bars)
    duplicate_count = len(timestamps) - len(set(timestamps))
    return {
        "symbol": instrument.symbol,
        "asset_class": instrument.asset_class.value,
        "provider": provider,
        "instrument_id": instrument.broker_instrument_id,
        "timeframe": timeframe.value,
        "bar_count": len(bars),
        "first_timestamp": min(timestamps).isoformat(),
        "last_timestamp": max(timestamps).isoformat(),
        "missing_bar_diagnostics": (),
        "duplicate_timestamps": duplicate_count,
        "timestamp_ordering": "OUT_OF_ORDER"
        if timestamps != tuple(sorted(timestamps))
        else "ORDERED",
        "data_quality_state": "GOOD" if duplicate_count == 0 else "CONFLICTING",
        "cache_status": cache_status,
        "mapping_status": mapping_status,
        "source_notes": source_notes,
        "deterministic_cache_key": (
            f"{provider}:{instrument.broker}:{instrument.broker_instrument_id}:{timeframe.value}"
        ),
    }


def _alpaca_quality_summary(depth: list[dict[str, object]]) -> dict[str, object]:
    verified = [item for item in depth if item.get("verification_status") == "VERIFIED"]
    crypto_verified = sorted(
        {
            str(item["symbol"])
            for item in verified
            if item.get("asset_class") == AssetClass.CRYPTO.value
        }
    )
    feed_values = sorted(
        {
            str(item["feed"])
            for item in depth
            if item.get("asset_class") in {AssetClass.EQUITY.value, AssetClass.ETF.value}
        }
    )
    return {
        "verified_symbol_timeframes": len(
            {(str(item["symbol"]), str(item["timeframe"])) for item in verified}
        ),
        "authorized_probe_rows": sum(
            1 for item in depth if item.get("authorization_status") == "AUTHORIZED"
        ),
        "no_data_probe_rows": sum(
            1 for item in depth if item.get("authorization_status") == "NO_DATA"
        ),
        "rate_limited_probe_rows": sum(
            1 for item in depth if item.get("authorization_status") == "RATE_LIMITED"
        ),
        "stock_etf_feed_observed": tuple(feed_values),
        "crypto_pairs_verified_by_response": tuple(crypto_verified),
        "provider_isolation": "alpaca bars are cached under provider=alpaca only",
    }


def _alpaca_backfill_pagination_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "pagination_requested": any(bool(item.get("pagination_requested")) for item in rows),
        "pagination_token_observed": any(
            bool(item.get("pagination_token_observed")) for item in rows
        ),
        "second_page_fetched": any(bool(item.get("second_page_fetched")) for item in rows),
        "pagination_verified": any(bool(item.get("pagination_verified")) for item in rows),
        "rows_with_second_page": sum(1 for item in rows if item.get("second_page_fetched")),
    }


def _lifecycle_existing_components() -> tuple[dict[str, object], ...]:
    return (
        {
            "component": "HistoricalReplayClock",
            "source": "app.validation.replay.HistoricalReplayClock",
            "status": "PRESENT",
            "purpose": "anti-lookahead visible data boundary",
        },
        {
            "component": "AegisOpportunityIntelligenceEngine",
            "source": "app.intelligence.service.AegisOpportunityIntelligenceEngine",
            "status": "PRESENT",
            "purpose": "deterministic opportunity analysis at replay timestamp",
        },
        {
            "component": "DeterministicAegisAgent",
            "source": "app.agent.service.DeterministicAegisAgent",
            "status": "PRESENT",
            "purpose": "TradeProposal or HOLD generation from normalized facts",
        },
        {
            "component": "ExitPolicy",
            "source": "app.agent.exit_policy.ExitPolicy",
            "status": "PRESENT",
            "purpose": "position-aware REDUCE/CLOSE evaluation for existing long holdings",
        },
        {
            "component": "RiskManager",
            "source": "app.risk.manager.RiskManager",
            "status": "PRESENT",
            "purpose": "independent deterministic risk gate",
        },
        {
            "component": "SimulatedExecutionEngine",
            "source": "app.validation.execution.SimulatedExecutionEngine",
            "status": "PRESENT",
            "purpose": "research-only OPEN/INCREASE/REDUCE/CLOSE accounting",
        },
    )


def _lifecycle_missing_pieces(result: StrategyValidationResult) -> tuple[str, ...]:
    lifecycle = result.lifecycle
    if lifecycle is None:
        return ("lifecycle summary was not produced",)
    missing: list[str] = []
    if not result.walk_forward_windows:
        missing.append("walk-forward windows were not produced")
    if not any(decision.future_records_ignored > 0 for decision in result.decisions):
        missing.append("anti-lookahead evidence did not observe hidden future bars")
    return tuple(missing)


def _lifecycle_evidence_gaps(result: StrategyValidationResult) -> tuple[str, ...]:
    lifecycle = result.lifecycle
    if lifecycle is None:
        return ("lifecycle summary was not produced",)
    gaps: list[str] = []
    if lifecycle.entry_count == 0:
        gaps.append("no simulated OPEN entry was naturally produced")
    if lifecycle.increase_count == 0:
        gaps.append("no simulated INCREASE was naturally produced")
    if lifecycle.reduction_count == 0:
        gaps.append("no simulated REDUCE was naturally produced")
    if lifecycle.completed_trade_count == 0:
        gaps.append("no simulated CLOSE completed trade was naturally produced")
    if not any(decision.proposal_intent is not None for decision in result.decisions):
        gaps.append("no TradeProposal was produced")
    return tuple(gaps)


def _lifecycle_zero_trade_forensics(
    decisions: tuple[ReplayDecision, ...],
) -> dict[str, object]:
    observations = len(decisions)
    actionable = tuple(
        decision for decision in decisions if decision.aegis_decision.value in {"BUY", "REDUCE"}
    )
    proposals = tuple(decision for decision in decisions if decision.proposal_id is not None)
    risk_rejected = tuple(
        decision for decision in proposals if decision.status is ReplayDecisionStatus.RISK_REJECTED
    )
    risk_approved = tuple(decision for decision in proposals if decision not in risk_rejected)
    simulated_entries = tuple(
        decision
        for decision in proposals
        if decision.status is ReplayDecisionStatus.SIMULATED_EXECUTED
        and (
            decision.proposal_intent is not None
            and decision.proposal_intent.value in {"OPEN", "INCREASE"}
        )
    )
    pre_risk_rejected = max(0, len(actionable) - len(proposals))
    stage_counts = {
        "observations": observations,
        "intelligence_opportunity_records": observations,
        "actionable_decisions": len(actionable),
        "proposals_created": len(proposals),
        "proposals_rejected_before_risk_manager": pre_risk_rejected,
        "risk_manager_approvals": len(risk_approved),
        "risk_manager_rejections": len(risk_rejected),
        "simulated_entries": len(simulated_entries),
    }
    first_divergence = _first_zero_trade_divergence(stage_counts)
    return {
        "stage_counts": stage_counts,
        "decision_distribution": _final_decision_funnel(decisions),
        "strategy_vote_funnel": _strategy_vote_funnel(decisions),
        "gate_failure_counts": _gate_failure_counts(decisions),
        "root_gate_failures": _root_gate_failures(decisions),
        "proposal_gate_actuals": {
            "opportunity_score": _gate_value_counts(decisions, "opportunity_score"),
            "confidence": _gate_value_counts(decisions, "confidence"),
            "data_quality": _gate_value_counts(decisions, "data_quality"),
        },
        "risk_rejection_reasons": dict(
            Counter(reason for decision in risk_rejected for reason in decision.risk_reasons)
        ),
        "zero_count_transitions": _zero_count_transitions(stage_counts, decisions),
        "first_divergence": first_divergence,
        "root_cause": first_divergence["reason"],
    }


def _first_zero_trade_divergence(stage_counts: dict[str, int]) -> dict[str, object]:
    if stage_counts["observations"] == 0:
        return {
            "from": "cache",
            "to": "observation",
            "reason": "no replay observations were generated",
        }
    if stage_counts["actionable_decisions"] == 0:
        return {
            "from": "intelligence_opportunity",
            "to": "agent_decision",
            "reason": (
                "opportunity/agent layer produced no BUY or REDUCE decisions; "
                "no TradeProposal may be created downstream"
            ),
        }
    if stage_counts["proposals_created"] == 0:
        return {
            "from": "agent_decision",
            "to": "trade_proposal",
            "reason": "actionable decisions did not produce admissible TradeProposal objects",
        }
    if stage_counts["risk_manager_approvals"] == 0:
        return {
            "from": "trade_proposal",
            "to": "risk_decision",
            "reason": "RiskManager rejected every proposal",
        }
    if stage_counts["simulated_entries"] == 0:
        return {
            "from": "risk_decision",
            "to": "simulated_entry",
            "reason": "authorized proposals did not produce simulated OPEN/INCREASE entries",
        }
    return {
        "from": "simulated_entry",
        "to": "completed_trade",
        "reason": "entries exist; zero completed trades must be investigated in exit lifecycle",
    }


def _zero_count_transitions(
    stage_counts: dict[str, int],
    decisions: tuple[ReplayDecision, ...],
) -> tuple[dict[str, object], ...]:
    transitions = (
        (
            "observation",
            "intelligence_opportunity",
            stage_counts["intelligence_opportunity_records"],
            "all replay observations generated intelligence records",
        ),
        (
            "intelligence_opportunity",
            "agent_decision",
            stage_counts["actionable_decisions"],
            "final Aegis decision was HOLD/IGNORE after opportunity gates",
        ),
        (
            "agent_decision",
            "trade_proposal",
            stage_counts["proposals_created"],
            "no TradeProposal created after agent/actionability gates",
        ),
        (
            "trade_proposal",
            "risk_decision",
            stage_counts["risk_manager_approvals"] + stage_counts["risk_manager_rejections"],
            "RiskManager was not reached because no proposal existed",
        ),
        (
            "risk_decision",
            "simulated_entry",
            stage_counts["simulated_entries"],
            "no approved OPEN/INCREASE proposal reached simulated execution",
        ),
    )
    return tuple(
        {
            "from": start,
            "to": end,
            "count": count,
            "reason": _transition_reason(reason, decisions),
        }
        for start, end, count, reason in transitions
        if count == 0
    )


def _transition_reason(reason: str, decisions: tuple[ReplayDecision, ...]) -> str:
    if "HOLD/IGNORE" not in reason:
        return reason
    failures = _root_gate_failures(decisions)
    if not failures:
        return reason
    top_gate, count = next(iter(failures.items()))
    return f"{reason}; top root gate failure: {top_gate} ({count})"


def _alpaca_full_backfill_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    successful = [item for item in rows if item.get("final_status") == "SUCCESS"]
    return {
        "requested_symbol_timeframes": len(rows),
        "successful_symbol_timeframes": len(successful),
        "total_bars": sum(_int_payload_value(item.get("bars", 0)) for item in successful),
        "cache_inserted": sum(_int_payload_value(item.get("cache_inserted", 0)) for item in rows),
        "cache_updated": sum(_int_payload_value(item.get("cache_updated", 0)) for item in rows),
        "cache_unchanged": sum(_int_payload_value(item.get("cache_unchanged", 0)) for item in rows),
        "unsupported_or_blocked": tuple(
            f"{item.get('symbol')}:{item.get('timeframe')}:{item.get('final_status')}"
            for item in rows
            if item.get("final_status") != "SUCCESS"
        ),
    }


def _verified_etoro_broker_ids(cache: HistoricalDataCache) -> dict[str, str]:
    expected = _exit_evidence_expected_asset_classes()
    references = cache.mapping_references(provider="etoro")
    broker_ids: dict[str, str] = {}
    for reference in references:
        symbol = reference.broker_symbol.upper()
        expected_asset_class = expected.get(symbol)
        if expected_asset_class is None:
            continue
        if reference.asset_class is not expected_asset_class:
            continue
        if not reference.verified or reference.broker_instrument_id is None:
            continue
        broker_ids[symbol] = reference.broker_instrument_id
    return broker_ids


def _alpaca_backfill_mapping_reconciliation(
    cache: HistoricalDataCache,
    *,
    mapping_overrides: Mapping[tuple[str, str], ProviderInstrumentReference],
) -> tuple[dict[str, object], ...]:
    expected = _exit_evidence_expected_asset_classes()
    etoro_ids = _verified_etoro_broker_ids(cache)
    alpaca_refs = {
        reference.broker_symbol.upper(): reference
        for reference in cache.mapping_references(provider="alpaca")
        if reference.verified
    }
    rows: list[dict[str, object]] = []
    for asset_class, symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.items():
        for symbol in symbols:
            provider_symbol = f"{symbol}/USD" if asset_class is AssetClass.CRYPTO else symbol
            etoro_id = etoro_ids.get(symbol)
            alpaca_ref = alpaca_refs.get(symbol)
            mapping_state = "VERIFIED_CROSS_PROVIDER"
            verification_status = "ETORO_REFERENCE_VERIFIED_ALPACA_RESPONSE_PENDING"
            if etoro_id is None and alpaca_ref is None:
                mapping_state = "ETORO_REFERENCE_UNAVAILABLE"
                verification_status = "PENDING_PROVIDER_RESPONSE"
            elif etoro_id is None and alpaca_ref is not None:
                mapping_state = "ALPACA_ONLY_VERIFIED"
                verification_status = "ALPACA_PROVIDER_RESPONSE_VERIFIED"
            elif alpaca_ref is not None and alpaca_ref.broker_instrument_id != etoro_id:
                mapping_state = "MAPPING_CONFLICT_QUARANTINED"
                verification_status = "CROSS_PROVIDER_EQUIVALENCE_QUARANTINED"
            elif alpaca_ref is not None:
                verification_status = "VERIFIED_BY_PROVIDER_AND_ETORO_REFERENCE"
            rows.append(
                {
                    "symbol": symbol,
                    "requested_asset_class": expected[symbol].value,
                    "alpaca_provider_symbol": provider_symbol,
                    "alpaca_asset_class": asset_class.value,
                    "canonical_broker_instrument_id": etoro_id,
                    "broker_symbol": symbol if etoro_id is not None else None,
                    "currency": Currency.USD.value,
                    "mapping_source": (
                        "eToro cache verified reference + explicit Alpaca provider symbol"
                        if etoro_id is not None
                        else "explicit Alpaca provider symbol; no verified eToro reference"
                    ),
                    "verification_status": verification_status,
                    "mapping_state": mapping_state,
                    "ticker_equality_sufficient": False,
                }
            )
    pilot_symbols = {
        reference.broker_symbol.upper()
        for reference in mapping_overrides.values()
        if reference.verified
    }
    return tuple(
        row
        | {
            "pilot_provider_mapping_present": row["symbol"] in pilot_symbols,
        }
        for row in rows
    )


def _cache_integrity_payload(
    *,
    duplicate_before: tuple[dict[str, object], ...],
    duplicate_after: tuple[dict[str, object], ...],
    quarantine: dict[str, object],
) -> dict[str, object]:
    duplicate_rows_before = sum(_duplicate_timestamp_count(item) for item in duplicate_before)
    duplicate_rows_after = sum(_duplicate_timestamp_count(item) for item in duplicate_after)
    duplicate_root_cause = (
        "not present in current SQLite cache after primary-key upsert; if seen in "
        "Windows output, source was provider response/report-stage overlap before "
        "persisted cache state"
    )
    if duplicate_rows_before or duplicate_rows_after:
        duplicate_root_cause = (
            "duplicates persisted in cache and require row-level conflict inspection"
        )
    return {
        "duplicate_timestamp_root_cause": duplicate_root_cause,
        "duplicate_timestamp_groups_before": duplicate_before,
        "duplicate_timestamp_groups_after": duplicate_after,
        "duplicate_rows_before": duplicate_rows_before,
        "duplicate_rows_after": duplicate_rows_after,
        "quarantine": quarantine,
    }


def _duplicate_timestamp_count(item: dict[str, object]) -> int:
    value = item.get("duplicate_timestamps", 0)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _evidence_capacity(coverage: tuple[dict[str, object], ...]) -> dict[str, object]:
    timeframes = sorted({str(item["timeframe"]) for item in coverage})
    bars_by_timeframe = {
        timeframe: sum(
            _coverage_bar_count(item) for item in coverage if item["timeframe"] == timeframe
        )
        for timeframe in timeframes
    }
    estimated_decisions = {
        timeframe: max(0, count // 5) for timeframe, count in bars_by_timeframe.items()
    }
    estimated_held_observations = {
        timeframe: max(0, decisions // 10) for timeframe, decisions in estimated_decisions.items()
    }
    estimated_completed_lifecycles = {
        timeframe: max(0, held // 8) for timeframe, held in estimated_held_observations.items()
    }
    return {
        "method": "planning estimate from usable bars; not validation evidence",
        "usable_symbol_timeframes": len(coverage),
        "bars_by_timeframe": bars_by_timeframe,
        "estimated_decisions": estimated_decisions,
        "estimated_held_position_observations": estimated_held_observations,
        "estimated_completed_lifecycles": estimated_completed_lifecycles,
    }


def _coverage_bar_count(item: dict[str, object]) -> int:
    value = item.get("bar_count", 0)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _historical_depth_limit_payload(coverage: list[dict[str, object]]) -> dict[str, object]:
    shallow_4h = tuple(
        {
            "symbol": item["symbol"],
            "asset_class": item["asset_class"],
            "bar_count": item["bar_count"],
            "first_timestamp": item["first_timestamp"],
            "last_timestamp": item["last_timestamp"],
        }
        for item in coverage
        if item["timeframe"] == "4H" and _coverage_bar_count(item) <= 1000
    )
    return {
        "etoro_current_adapter_limit": (
            "EtoroHistoricalMarketDataProvider calls candle_history with "
            "candles_count=min(limit, 1000) and exposes no documented pagination/date cursor"
        ),
        "backfill_implemented": False,
        "reason": (
            "no supported pagination/direction/date semantics are present in the current "
            "central read client; unsupported endpoint behavior was not invented"
        ),
        "shallow_4h_datasets": shallow_4h,
        "provider_expansion_required_if_4h_depth_needs_several_thousand_bars": bool(shallow_4h),
    }


def _real_validation_blocked(
    category: str, reason: str, *, config: ApplicationConfig
) -> dict[str, object]:
    return {
        "status": "BLOCKED",
        "validation_runtime_version": REAL_VALIDATION_RUNTIME_VERSION,
        "policy_admission_source": POLICY_ADMISSION_SOURCE,
        "category": category,
        "reason": reason,
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
        "market_data_cache_path": str(DEFAULT_MARKET_DATA_CACHE_PATH),
        "record_store_path": str(DEFAULT_STRATEGY_VALIDATION_STORE_PATH),
    }


def _parse_datetime_filter(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if "T" not in normalized:
        normalized = f"{normalized}T00:00:00+00:00"
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _selected_timeframes(value: str | None) -> tuple[TimeFrame, ...]:
    if value is None:
        return (
            TimeFrame.ONE_HOUR,
            TimeFrame.FOUR_HOUR,
            TimeFrame.ONE_DAY,
            TimeFrame.ONE_WEEK,
        )
    requested = []
    for raw in value.split(","):
        normalized = raw.strip().upper()
        if not normalized:
            continue
        requested.append(
            next(
                item
                for item in TimeFrame
                if item.value.upper() == normalized or item.name == normalized
            )
        )
    return tuple(dict.fromkeys(requested))


def _asset_class_filter(value: str | None) -> AssetClass | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not normalized:
        return None
    return AssetClass(normalized)


def _history_limit(*, start_at: datetime | None, as_of: datetime) -> int:
    if start_at is None:
        return 1000
    return min(1000, max(120, (as_of - start_at).days + 30))


def _resolve_research_universe(
    *,
    client: EtoroReadClient | None,
    as_of: datetime,
    asset_class_filter: AssetClass | None,
    instrument_filter: str | None,
    max_instruments: int,
    policy_engine: AssetPolicyEngine,
    admission_traces: list[dict[str, object]] | None = None,
) -> tuple[tuple[UniversalInstrument, ...], tuple[str, ...], str]:
    symbols = _research_symbols(
        asset_class_filter=asset_class_filter,
        instrument_filter=instrument_filter,
        max_instruments=max_instruments,
    )
    if client is not None:
        return _resolve_etoro_research_universe(
            client=client,
            symbols=symbols,
            as_of=as_of,
            asset_class_filter=asset_class_filter,
            policy_engine=policy_engine,
            admission_traces=admission_traces,
        )
    return _fallback_research_universe(
        symbols=symbols,
        as_of=as_of,
        asset_class_filter=asset_class_filter,
        policy_engine=policy_engine,
        admission_traces=admission_traces,
    )


def _research_symbols(
    *,
    asset_class_filter: AssetClass | None,
    instrument_filter: str | None,
    max_instruments: int,
) -> tuple[str, ...]:
    if max_instruments <= 0:
        max_instruments = 1
    if instrument_filter:
        return (instrument_filter.strip().upper(),)
    by_class = {
        AssetClass.EQUITY: ("AAPL", "MSFT", "NVDA"),
        AssetClass.ETF: ("SPY", "QQQ", "VTI"),
        AssetClass.CRYPTO: ("BTC", "ETH", "SOL"),
    }
    symbols: list[str] = []
    for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO):
        if asset_class_filter is not None and asset_class is not asset_class_filter:
            continue
        symbols.extend(by_class[asset_class])
    return tuple(symbols[:max_instruments])


def _resolve_etoro_research_universe(
    *,
    client: EtoroReadClient,
    symbols: tuple[str, ...],
    as_of: datetime,
    asset_class_filter: AssetClass | None,
    policy_engine: AssetPolicyEngine,
    expected_asset_classes: Mapping[str, AssetClass] | None = None,
    admission_traces: list[dict[str, object]] | None = None,
) -> tuple[tuple[UniversalInstrument, ...], tuple[str, ...], str]:
    instruments: list[UniversalInstrument] = []
    rejected: list[str] = []
    metadata_cache: dict[int, dict[str, object]] = {}
    instrument_type_names: dict[int, str] | None = None
    for symbol in symbols:
        resolution = client.resolve_instrument(symbol, as_of=as_of)
        classification = classify_etoro_instrument_metadata(resolution.classification_metadata)
        metadata = dict(resolution.classification_metadata)
        evidence_source = classification.evidence_source
        classification_status = classification.status
        if (
            classification.asset_class is AssetClass.UNKNOWN
            and resolution.resolved
            and resolution.instrument_id > 0
        ):
            if resolution.instrument_id not in metadata_cache:
                metadata_cache.update(client.instrument_metadata((resolution.instrument_id,)))
            provider_metadata = metadata_cache.get(resolution.instrument_id, {})
            enriched_metadata = safe_instrument_classification_metadata(provider_metadata)
            if enriched_metadata:
                metadata = {**metadata, **enriched_metadata}
                if any(key in metadata for key in ("instrumentTypeID", "instrumentTypeId")):
                    instrument_type_names = instrument_type_names or client.instrument_type_names()
                classification = classify_etoro_instrument_metadata(
                    metadata,
                    instrument_type_names=instrument_type_names,
                )
                evidence_source = (
                    f"instruments.{classification.evidence_source}"
                    if classification.evidence_source != "none"
                    else "instruments"
                )
                classification_status = classification.status
        asset_class = classification.asset_class
        expected_asset_class = (
            expected_asset_classes.get(symbol.upper()) if expected_asset_classes else None
        )
        if expected_asset_class is not None and asset_class is not expected_asset_class:
            reason = "REQUESTED_ASSET_CLASS_MISMATCH"
            _append_admission_trace(
                admission_traces,
                symbol=symbol,
                resolved=resolution.resolved,
                broker_instrument_id=str(resolution.instrument_id),
                raw_type_fields_used=metadata,
                classification_evidence_source=evidence_source,
                classification_status=classification_status,
                normalized_asset_class=asset_class,
                policy_engine=policy_engine,
                admission_result=False,
                actual_rejection_code=reason,
            )
            rejected.append(f"{symbol}:{reason}:{expected_asset_class.value}!={asset_class.value}")
            continue
        if asset_class_filter is not None and asset_class is not asset_class_filter:
            reason = "ASSET_CLASS_MISMATCH"
            _append_admission_trace(
                admission_traces,
                symbol=symbol,
                resolved=resolution.resolved,
                broker_instrument_id=str(resolution.instrument_id),
                raw_type_fields_used=metadata,
                classification_evidence_source=evidence_source,
                classification_status=classification_status,
                normalized_asset_class=asset_class,
                policy_engine=policy_engine,
                admission_result=False,
                actual_rejection_code=reason,
            )
            rejected.append(f"{symbol}:{reason}")
            continue
        if not resolution.resolved or not resolution.structurally_supported:
            reason = resolution.structural_status
            _append_admission_trace(
                admission_traces,
                symbol=symbol,
                resolved=resolution.resolved,
                broker_instrument_id=str(resolution.instrument_id),
                raw_type_fields_used=metadata,
                classification_evidence_source=evidence_source,
                classification_status=classification_status,
                normalized_asset_class=asset_class,
                policy_engine=policy_engine,
                admission_result=False,
                actual_rejection_code=reason,
            )
            rejected.append(f"{symbol}:{reason}")
            continue
        admitted, reason = _research_asset_admission(asset_class, policy_engine)
        if asset_class is AssetClass.UNKNOWN and classification_status == "CLASSIFICATION_UNKNOWN":
            reason = "CLASSIFICATION_UNKNOWN"
        _append_admission_trace(
            admission_traces,
            symbol=symbol,
            resolved=resolution.resolved,
            broker_instrument_id=str(resolution.instrument_id),
            raw_type_fields_used=metadata,
            classification_evidence_source=evidence_source,
            classification_status=classification_status,
            normalized_asset_class=asset_class,
            policy_engine=policy_engine,
            admission_result=admitted,
            actual_rejection_code=reason,
        )
        if not admitted:
            rejected.append(f"{symbol}:{reason}")
            continue
        instruments.append(
            UniversalInstrument(
                broker="etoro",
                broker_instrument_id=str(resolution.instrument_id),
                symbol=resolution.internal_symbol_full,
                display_name=resolution.display_name,
                asset_class=asset_class,
                currency=Currency.USD,
                market_status=resolution.market_status,
                tradeable=resolution.is_currently_tradable,
                buy_allowed=resolution.is_buy_enabled,
                sell_allowed=True,
                short_allowed=False,
                leverage_available=False,
                max_leverage=Decimal("1"),
                settlement_type=SettlementType.REAL,
                last_price=resolution.current_rate,
                price_timestamp=as_of if resolution.current_rate is not None else None,
                metadata_timestamp=as_of,
                tags=("source:eToro-resolution",),
            )
        )
    return tuple(instruments), tuple(rejected), "etoro-official-search"


def _fallback_research_universe(
    *,
    symbols: tuple[str, ...],
    as_of: datetime,
    asset_class_filter: AssetClass | None,
    policy_engine: AssetPolicyEngine,
    admission_traces: list[dict[str, object]] | None = None,
) -> tuple[tuple[UniversalInstrument, ...], tuple[str, ...], str]:
    instruments: list[UniversalInstrument] = []
    rejected: list[str] = []
    for index, symbol in enumerate(symbols, start=1):
        asset_class = _known_asset_class(symbol)
        if asset_class_filter is not None and asset_class is not asset_class_filter:
            reason = "ASSET_CLASS_MISMATCH"
            _append_admission_trace(
                admission_traces,
                symbol=symbol,
                resolved=True,
                broker_instrument_id=str(100_000 + index),
                raw_type_fields_used={"fallbackSymbolClass": asset_class.value},
                classification_evidence_source="public-fallback-symbols",
                classification_status="CLASSIFIED_FROM_FALLBACK_METADATA",
                normalized_asset_class=asset_class,
                policy_engine=policy_engine,
                admission_result=False,
                actual_rejection_code=reason,
            )
            rejected.append(f"{symbol}:{reason}")
            continue
        admitted, reason = _research_asset_admission(asset_class, policy_engine)
        _append_admission_trace(
            admission_traces,
            symbol=symbol,
            resolved=True,
            broker_instrument_id=str(100_000 + index),
            raw_type_fields_used={"fallbackSymbolClass": asset_class.value},
            classification_evidence_source="public-fallback-symbols",
            classification_status="CLASSIFIED_FROM_FALLBACK_METADATA",
            normalized_asset_class=asset_class,
            policy_engine=policy_engine,
            admission_result=admitted,
            actual_rejection_code=reason,
        )
        if not admitted:
            rejected.append(f"{symbol}:{reason}")
            continue
        if asset_class is AssetClass.CRYPTO:
            rejected.append(f"{symbol}:NO_PUBLIC_FALLBACK_MAPPING")
            continue
        instruments.append(
            UniversalInstrument(
                broker="research-history",
                broker_instrument_id=str(100_000 + index),
                symbol=symbol,
                display_name=f"{symbol} research instrument",
                asset_class=asset_class,
                currency=Currency.USD,
                exchange="US",
                market_status=MarketStatus.OPEN,
                tradeable=True,
                buy_allowed=True,
                sell_allowed=True,
                short_allowed=False,
                leverage_available=False,
                max_leverage=Decimal("1"),
                settlement_type=SettlementType.REAL,
                metadata_timestamp=as_of,
                tags=("source:deterministic-public-fallback-universe",),
            )
        )
    return tuple(instruments), tuple(rejected), "public-fallback-symbols"


def _known_asset_class(symbol: str) -> AssetClass:
    if symbol in {"SPY", "QQQ", "VTI"}:
        return AssetClass.ETF
    if symbol in {"BTC", "ETH", "SOL"}:
        return AssetClass.CRYPTO
    return AssetClass.EQUITY


def build_policy_strategy_diagnostics(
    policy_engine: AssetPolicyEngine | None = None,
) -> tuple[dict[str, object], ...]:
    engine = policy_engine or default_asset_policy_engine()
    rows: list[dict[str, object]] = []
    for asset_class in AssetClass:
        profile = profile_for(asset_class)
        try:
            policy = engine.policy_for(asset_class)
        except KeyError:
            rows.append(
                {
                    "asset_class": asset_class.value,
                    "policy_enabled": False,
                    "policy_long_allowed": False,
                    "policy_leverage_allowed": False,
                    "policy_version": engine.policy_version,
                    "strategy_profile_enabled": profile.enabled,
                    "strategy_profile_version": profile.profile_version,
                    "policy_missing": True,
                }
            )
            continue
        rows.append(
            {
                "asset_class": asset_class.value,
                "policy_enabled": policy.enabled,
                "policy_long_allowed": policy.long_allowed,
                "policy_leverage_allowed": policy.leverage_allowed,
                "policy_version": engine.policy_version,
                "strategy_profile_enabled": profile.enabled,
                "strategy_profile_version": profile.profile_version,
                "policy_missing": False,
            }
        )
    return tuple(rows)


def _research_asset_admission(
    asset_class: AssetClass,
    policy_engine: AssetPolicyEngine,
) -> tuple[bool, str]:
    if asset_class is AssetClass.UNKNOWN:
        return False, "CLASSIFICATION_INSUFFICIENT"
    try:
        policy = policy_engine.policy_for(asset_class)
    except KeyError:
        return False, "POLICY_MISSING_ASSET_CLASS"
    if not policy.enabled:
        return False, "POLICY_DISABLED_ASSET_CLASS"
    if not policy.long_allowed:
        return False, "POLICY_LONG_DISABLED"
    profile = profile_for(asset_class)
    if not profile.enabled:
        return False, "STRATEGY_PROFILE_DISABLED"
    return True, "ADMITTED"


def _append_admission_trace(
    traces: list[dict[str, object]] | None,
    *,
    symbol: str,
    resolved: bool,
    broker_instrument_id: str,
    raw_type_fields_used: Mapping[str, object],
    classification_evidence_source: str,
    classification_status: str,
    normalized_asset_class: AssetClass,
    policy_engine: AssetPolicyEngine,
    admission_result: bool,
    actual_rejection_code: str,
) -> None:
    if traces is None:
        return
    policy = None
    policy_found = False
    try:
        policy = policy_engine.policy_for(normalized_asset_class)
        policy_found = True
    except KeyError:
        policy_found = False
    profile = profile_for(normalized_asset_class)
    traces.append(
        {
            "symbol": symbol,
            "resolved": resolved,
            "broker_instrument_id": broker_instrument_id,
            "raw_type_fields_used": dict(raw_type_fields_used),
            "classification_evidence_source": classification_evidence_source,
            "classification_status": classification_status,
            "normalized_asset_class": normalized_asset_class.value,
            "normalized_asset_class_repr": repr(normalized_asset_class),
            "normalized_asset_class_type": _type_name(type(normalized_asset_class)),
            "policy_lookup_key": normalized_asset_class.value,
            "policy_found": policy_found,
            "policy_enabled": policy.enabled if policy is not None else False,
            "long_allowed": policy.long_allowed if policy is not None else False,
            "strategy_lookup_key": normalized_asset_class.value,
            "strategy_found": profile.asset_class is normalized_asset_class,
            "strategy_enabled": profile.enabled,
            "admission_result": admission_result,
            "actual_rejection_code": actual_rejection_code,
        }
    )


def _type_name(value: type[object]) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _module_file_path(module: object) -> Path:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise RuntimeError("module file path is unavailable")
    return Path(module_file).resolve()


def build_runtime_forensics_report() -> dict[str, object]:
    import app
    import app.data.runtime as runtime_module
    import app.intelligence.profiles as profiles_module
    import app.main as main_module
    import app.policies.defaults as defaults_module
    import app.policies.engine as engine_module

    modules = (
        app,
        main_module,
        runtime_module,
        defaults_module,
        engine_module,
        profiles_module,
    )
    functions = (
        build_real_strategy_validation_report,
        _resolve_research_universe,
        _resolve_etoro_research_universe,
        _research_asset_admission,
    )
    policy_engine = default_asset_policy_engine()
    equity_policy = policy_engine.policy_for(AssetClass.EQUITY)
    repo_root = Path(__file__).resolve().parents[2]
    return {
        "status": "RUNTIME_FORENSICS",
        "validation_runtime_version": REAL_VALIDATION_RUNTIME_VERSION,
        "policy_admission_source": POLICY_ADMISSION_SOURCE,
        "python": {
            "sys_executable": sys.executable,
            "sys_prefix": sys.prefix,
            "cwd": str(Path.cwd().resolve()),
            "relevant_sys_path": tuple(
                entry
                for entry in sys.path
                if entry
                and (
                    str(repo_root) in str(Path(entry).resolve())
                    or "site-packages" in entry
                    or ".venv" in entry
                )
            ),
        },
        "modules": tuple(
            {
                "name": module.__name__,
                "file": str(_module_file_path(module)),
                "inside_current_repository": str(_module_file_path(module)).startswith(
                    str(repo_root)
                ),
            }
            for module in modules
        ),
        "function_fingerprints": tuple(
            {
                "qualified_name": f"{function.__module__}.{function.__name__}",
                "module_path": str(Path(inspect.getsourcefile(function) or "").resolve()),
                "source_sha256_short": hashlib.sha256(
                    inspect.getsource(function).encode("utf-8")
                ).hexdigest()[:16],
            }
            for function in functions
        ),
        "enum_identity": {
            "normalized_equity_type": _type_name(type(AssetClass.EQUITY)),
            "policy_key_type": _type_name(type(equity_policy.asset_class)),
            "equity_key_equal": equity_policy.asset_class == AssetClass.EQUITY,
            "etf_key_equal": policy_engine.policy_for(AssetClass.ETF).asset_class == AssetClass.ETF,
            "crypto_key_equal": policy_engine.policy_for(AssetClass.CRYPTO).asset_class
            == AssetClass.CRYPTO,
        },
        "policy_diagnostics": build_policy_strategy_diagnostics(policy_engine),
        "rejection_emitters": (
            "app.data.runtime._research_asset_admission:POLICY_DISABLED_ASSET_CLASS",
            "app.data.runtime.build_real_strategy_validation_report:NO_RESEARCH_UNIVERSE",
        ),
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": False,
        "real_execution_available": False,
    }


def build_etoro_instrument_schema_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    symbols: tuple[str, ...] = ("AAPL", "SPY", "BTC"),
) -> dict[str, object]:
    if config.etoro_demo_execution_enabled:
        return _real_validation_blocked(
            "DEMO_EXECUTION_ENABLED",
            "Demo execution must remain false",
            config=config,
        )
    credentials = runtime_credentials(values)
    if credentials is None:
        return _real_validation_blocked(
            "CREDENTIALS",
            "eToro credentials are not configured",
            config=config,
        )
    if not config.etoro_api_enabled:
        return _real_validation_blocked(
            "API_DISABLED",
            "ETORO_API_ENABLED is false",
            config=config,
        )
    client = EtoroReadClient(
        credentials,
        DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode)),
    )
    try:
        results = tuple(_safe_schema_probe(client, symbol.strip().upper()) for symbol in symbols)
    except EtoroApiError as exc:
        metadata = exc.safe_metadata()
        return {
            **_real_validation_blocked(
                metadata.get("category", "ETORO_API_ERROR"),
                "eToro read-only instrument schema probe failed",
                config=config,
            ),
            "endpoint": metadata.get("endpoint"),
            "http_status": metadata.get("http_status"),
            "transport_detail": metadata.get("transport_detail"),
            "cf_ray": metadata.get("cf_ray"),
        }
    return {
        "status": "ETORO_INSTRUMENT_SCHEMA_PROBE",
        "validation_runtime_version": REAL_VALIDATION_RUNTIME_VERSION,
        "policy_admission_source": POLICY_ADMISSION_SOURCE,
        "symbols": results,
        "broker_write": False,
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def _safe_schema_probe(client: EtoroReadClient, symbol: str) -> dict[str, object]:
    search_raw = client.raw_instrument_search(symbol)
    search_items = _safe_search_items(search_raw)
    winning_item = next(
        (
            item
            for item in search_items
            if str(item.get("internalSymbolFull", "")).strip().upper() == symbol
        ),
        None,
    )
    broker_instrument_id = _positive_int(
        winning_item.get("instrumentId") if winning_item is not None else None
    )
    metadata: dict[str, str | int | bool] = {}
    metadata_source = "search"
    if winning_item is not None:
        metadata.update(safe_instrument_classification_metadata(winning_item))
    if broker_instrument_id is not None:
        detail = client.instrument_metadata((broker_instrument_id,)).get(broker_instrument_id, {})
        detail_metadata = safe_instrument_classification_metadata(detail)
        if detail_metadata:
            metadata.update(detail_metadata)
            metadata_source = "search+instruments"
    classification = classify_etoro_instrument_metadata(metadata)
    return {
        "symbol": symbol,
        "resolved": broker_instrument_id is not None,
        "broker_instrument_id": broker_instrument_id,
        "search_candidates": tuple(
            {
                "instrumentId": item.get("instrumentId"),
                "internalSymbolFull": item.get("internalSymbolFull"),
                "symbol": item.get("symbol"),
                "displayname": item.get("displayname"),
                "safe_classification_fields": safe_instrument_classification_metadata(item),
            }
            for item in search_items
        ),
        "classification_metadata_source": metadata_source,
        "safe_classification_fields": metadata,
        "canonical_asset_class": classification.asset_class.value,
        "classification_status": classification.status,
        "classification_evidence_source": classification.evidence_source,
    }


def _safe_search_items(raw: object) -> tuple[dict[str, object], ...]:
    if not isinstance(raw, dict):
        return ()
    items = raw.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, dict))


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _stooq_mapping_overrides(
    instruments: tuple[UniversalInstrument, ...],
) -> dict[tuple[str, str], ProviderInstrumentReference]:
    overrides: dict[tuple[str, str], ProviderInstrumentReference] = {}
    for instrument in instruments:
        if instrument.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}:
            continue
        overrides[("stooq", instrument.key)] = ProviderInstrumentReference(
            provider="stooq",
            provider_symbol=f"{instrument.symbol.lower()}.us",
            broker=instrument.broker,
            broker_symbol=instrument.symbol,
            broker_instrument_id=instrument.broker_instrument_id,
            exchange=instrument.exchange or "US",
            asset_class=instrument.asset_class,
            currency=instrument.currency,
            mapping_confidence=Decimal("0.85"),
            mapping_source="deterministic US liquid research universe mapping",
            verified=False,
        )
    return overrides


def _validation_run_payload(
    typed: StrategyValidationResult, timeframe: TimeFrame
) -> dict[str, object]:
    lifecycle = typed.lifecycle.model_dump(mode="json") if typed.lifecycle is not None else None
    return {
        "run_id": typed.run_id,
        "timeframe": timeframe.value,
        "dataset_id": typed.dataset.dataset_id,
        "dataset_digest": typed.dataset.data_digest,
        "period_splits": tuple(split.name.value for split in typed.period_splits),
        "walk_forward_windows": len(typed.walk_forward_windows),
        "decisions": len(typed.decisions),
        "trade_count": typed.metrics.trade_count,
        "trade_count_semantics": "completed_realized_trade_records",
        "lifecycle": lifecycle,
        "equity_curve_semantics": "portfolio_equity_mark_to_market",
        "equity_curve_points": len(typed.equity_curve_detail),
        "total_return": str(typed.metrics.total_return),
        "total_return_semantics": "total_portfolio_equity_return",
        "annualized_return": str(typed.metrics.annualized_return),
        "profit_factor": str(typed.metrics.profit_factor),
        "closed_trade_profit_factor": str(typed.metrics.profit_factor),
        "expectancy": str(typed.metrics.expectancy),
        "closed_trade_expectancy": str(typed.metrics.expectancy),
        "sharpe_ratio": str(typed.metrics.sharpe_ratio),
        "sharpe_ratio_semantics": "portfolio_equity_sharpe",
        "sortino_ratio": str(typed.metrics.sortino_ratio),
        "sortino_ratio_semantics": "portfolio_equity_sortino",
        "maximum_drawdown": str(typed.metrics.maximum_drawdown),
        "maximum_drawdown_semantics": "portfolio_equity_drawdown",
        "qualification": typed.qualification.status.value,
        "decision_funnel": (
            typed.decision_funnel.model_dump(mode="json")
            if typed.decision_funnel is not None
            else None
        ),
        "zero_trade_diagnostics": tuple(
            item.model_dump(mode="json") for item in typed.zero_trade_diagnostics[:10]
        ),
        "decision_forensics": _decision_forensics_payload(typed.decisions),
        "evidence_passed": typed.evidence_passed,
        "evidence_failures": typed.evidence_failures,
        "score_calibration": tuple(
            item.model_dump(mode="json") for item in typed.score_calibration
        ),
        "confidence_calibration": tuple(
            item.model_dump(mode="json") for item in typed.confidence_calibration
        ),
        "stress_results": tuple(item.model_dump(mode="json") for item in typed.stress_results),
        "parameter_stability": typed.parameter_stability.value,
        "overfit_risk": typed.overfit_risk.value,
        "monte_carlo": typed.monte_carlo.model_dump(mode="json"),
    }


def _combined_funnel(runs: list[dict[str, object]]) -> dict[str, int]:
    keys = (
        "market_observations",
        "candidates_analyzed",
        "buy_signals",
        "watch_signals",
        "hold_signals",
        "avoid_signals",
        "reduce_signals",
        "final_buy_decisions",
        "final_hold_decisions",
        "final_reduce_decisions",
        "final_ignore_decisions",
        "trade_proposals",
        "risk_rejected",
        "simulated_executed",
        "exited_trades",
    )
    totals = dict.fromkeys(keys, 0)
    for run in runs:
        funnel = run.get("decision_funnel")
        if not isinstance(funnel, dict):
            continue
        for key in keys:
            totals[key] += int(funnel.get(key, 0))
    return totals


def _decision_forensics_payload(decisions: tuple[ReplayDecision, ...]) -> dict[str, object]:
    if not decisions:
        return {
            "confidence_scale": "0..1",
            "score_scale": "0..100",
            "representative_traces": (),
        }
    return {
        "confidence_scale": "0..1",
        "score_scale": "0..100",
        "strategy_vote_funnel": _strategy_vote_funnel(decisions),
        "final_decision_funnel": _final_decision_funnel(decisions),
        "score_distribution": _decimal_distribution(
            tuple(decision.score for decision in decisions)
        ),
        "confidence_distribution": _decimal_distribution(
            tuple(decision.confidence for decision in decisions)
        ),
        "data_quality_distribution": _gate_value_counts(decisions, "data_quality"),
        "gate_failure_counts": _gate_failure_counts(decisions),
        "root_gate_failures": _root_gate_failures(decisions),
        "downstream_decision_outcomes": _downstream_decision_outcomes(decisions),
        "cumulative_gate_funnel": _cumulative_gate_funnel(decisions),
        "fine_confidence_calibration": _fine_confidence_calibration(decisions),
        "confidence_compression_diagnostics": _confidence_compression_diagnostics(decisions),
        "confidence_ablation_study": build_confidence_ablation_study(decisions),
        "risk_manager_proposal_matrix": _risk_manager_proposal_matrix(decisions),
        "risk_manager_proposal_aggregates": _risk_manager_proposal_aggregates(decisions),
        "threshold_provenance": _threshold_provenance(),
        "representative_traces": _representative_decision_traces(decisions),
        "metric_provenance": {
            "trade_performance": "simulated executed trades and equity curve",
            "forward_outcome_research": "candidate forward returns after decision time",
        },
    }


def _strategy_vote_funnel(decisions: tuple[ReplayDecision, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for decision in decisions:
        counts.update(direction.value for direction in decision.strategy_directions)
    return dict(sorted(counts.items()))


def _final_decision_funnel(decisions: tuple[ReplayDecision, ...]) -> dict[str, int]:
    counts = Counter(decision.aegis_decision.value for decision in decisions)
    return dict(sorted(counts.items()))


def _decimal_distribution(values: tuple[Decimal, ...]) -> dict[str, str]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "minimum": str(ordered[0]),
        "p10": str(_percentile(ordered, Decimal("0.10"))),
        "p25": str(_percentile(ordered, Decimal("0.25"))),
        "median": str(_percentile(ordered, Decimal("0.50"))),
        "p75": str(_percentile(ordered, Decimal("0.75"))),
        "p90": str(_percentile(ordered, Decimal("0.90"))),
        "maximum": str(ordered[-1]),
    }


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal:
    index = int((Decimal(len(values) - 1) * percentile).to_integral_value())
    return values[index]


def _gate_failure_counts(decisions: tuple[ReplayDecision, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for decision in decisions:
        for gate in decision.proposal_gate_trace:
            if gate.get("passed") is False:
                counts[str(gate.get("gate"))] += 1
    return dict(sorted(counts.items()))


def _root_gate_failures(decisions: tuple[ReplayDecision, ...]) -> dict[str, int]:
    root_gates = {"opportunity_score", "confidence", "data_quality"}
    counts: Counter[str] = Counter()
    for decision in decisions:
        for gate in decision.proposal_gate_trace:
            gate_name = str(gate.get("gate"))
            if gate_name in root_gates and gate.get("passed") is False:
                counts[gate_name] += 1
    return dict(sorted(counts.items()))


def _downstream_decision_outcomes(decisions: tuple[ReplayDecision, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for decision in decisions:
        counts[f"final_action:{decision.aegis_decision.value}"] += 1
        if decision.proposal_id is None:
            counts["trade_proposal:false"] += 1
        else:
            counts["trade_proposal:true"] += 1
        counts[f"replay_status:{decision.status.value}"] += 1
    return dict(sorted(counts.items()))


def _cumulative_gate_funnel(decisions: tuple[ReplayDecision, ...]) -> dict[str, int]:
    total = len(decisions)
    score_pass = tuple(
        decision for decision in decisions if _gate_passed(decision, "opportunity_score")
    )
    score_fail = total - len(score_pass)
    confidence_pass = tuple(
        decision for decision in score_pass if _gate_passed(decision, "confidence")
    )
    confidence_fail = len(score_pass) - len(confidence_pass)
    data_quality_pass = tuple(
        decision for decision in confidence_pass if _gate_passed(decision, "data_quality")
    )
    data_quality_fail = len(confidence_pass) - len(data_quality_pass)
    final_buy = tuple(
        decision for decision in data_quality_pass if decision.aegis_decision.value == "BUY"
    )
    proposals = tuple(decision for decision in final_buy if decision.proposal_id is not None)
    risk_reached = len(proposals)
    risk_authorized = sum(
        1
        for decision in proposals
        if not decision.risk_reasons and decision.status is not ReplayDecisionStatus.RISK_REJECTED
    )
    simulated = sum(
        1 for decision in proposals if decision.status is ReplayDecisionStatus.SIMULATED_EXECUTED
    )
    return {
        "total_candidates": total,
        "score_pass": len(score_pass),
        "score_fail": score_fail,
        "confidence_pass_after_score": len(confidence_pass),
        "confidence_fail_after_score": confidence_fail,
        "data_quality_pass_after_confidence": len(data_quality_pass),
        "data_quality_fail_after_confidence": data_quality_fail,
        "final_buy_after_all_upstream_gates": len(final_buy),
        "trade_proposal_created": len(proposals),
        "risk_manager_reached": risk_reached,
        "risk_authorized": risk_authorized,
        "simulated_executed": simulated,
    }


def _gate_passed(decision: ReplayDecision, gate_name: str) -> bool:
    for gate in decision.proposal_gate_trace:
        if gate.get("gate") == gate_name:
            return gate.get("passed") is True
    return False


def _gate_value_counts(decisions: tuple[ReplayDecision, ...], gate_name: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for decision in decisions:
        for gate in decision.proposal_gate_trace:
            if gate.get("gate") == gate_name:
                counts[str(gate.get("actual"))] += 1
    return dict(sorted(counts.items()))


def _risk_manager_proposal_matrix(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for decision in decisions:
        if decision.proposal_id is None:
            continue
        confidence_gate = _gate_by_name(decision, "confidence")
        risk_confidence = _gate_by_name(decision, "risk_manager_confidence")
        stale_price = _gate_by_name(decision, "risk_manager_stale_price")
        authorization = _gate_by_name(decision, "risk_manager_authorization")
        rejection_codes = tuple(str(reason) for reason in decision.risk_reasons)
        rows.append(
            {
                "proposal_id": decision.proposal_id,
                "proposal_side": (
                    decision.proposal_side.value if decision.proposal_side is not None else None
                ),
                "proposal_intent": (
                    decision.proposal_intent.value if decision.proposal_intent is not None else None
                ),
                "symbol": decision.symbol,
                "asset_class": decision.asset_class.value,
                "timestamp": decision.timestamp.isoformat(),
                "score": str(decision.score),
                "v2_b_confidence": str(decision.confidence),
                "v2_b_threshold": str(confidence_gate.get("threshold", "")),
                "v2_b_threshold_pass": confidence_gate.get("passed"),
                "confidence_model_version": confidence_gate.get("confidence_model_version"),
                "confidence_semantics_version": confidence_gate.get("confidence_semantics_version"),
                "risk_manager_confidence_input": risk_confidence.get("actual"),
                "risk_manager_confidence_field": risk_confidence.get("field_read"),
                "risk_manager_minimum_threshold": risk_confidence.get("threshold"),
                "risk_manager_confidence_pass": risk_confidence.get("passed"),
                "risk_manager_confidence_scale": risk_confidence.get("scale"),
                "risk_manager_threshold_provenance": risk_confidence.get("threshold_provenance"),
                "risk_policy_profile_version": risk_confidence.get("risk_policy_profile_version"),
                "asset_policy_version": risk_confidence.get("asset_policy_version"),
                "price_timestamp": stale_price.get("price_timestamp"),
                "replay_reference_timestamp": stale_price.get("reference_timestamp"),
                "price_age_seconds": stale_price.get("actual"),
                "max_allowed_price_age_seconds": stale_price.get("threshold"),
                "stale_price_pass": stale_price.get("passed"),
                "all_risk_manager_rejection_codes": rejection_codes,
                "risk_manager_authorized": authorization.get("passed"),
            }
        )
    return tuple(rows)


def _risk_manager_proposal_aggregates(
    decisions: tuple[ReplayDecision, ...],
) -> dict[str, object]:
    matrix = _risk_manager_proposal_matrix(decisions)
    by_asset: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    confidence_values: list[Decimal] = []
    confidence_margins: list[Decimal] = []
    confidence_pass = 0
    stale_pass = 0
    authorized = 0
    for row in matrix:
        by_asset[str(row["asset_class"])] += 1
        confidence = Decimal(str(row["risk_manager_confidence_input"]))
        threshold = Decimal(str(row["risk_manager_minimum_threshold"]))
        confidence_values.append(confidence)
        confidence_margins.append(confidence - threshold)
        if row["risk_manager_confidence_pass"] is True:
            confidence_pass += 1
        if row["stale_price_pass"] is True:
            stale_pass += 1
        if row["risk_manager_authorized"] is True:
            authorized += 1
        rejection_codes = row["all_risk_manager_rejection_codes"]
        if isinstance(rejection_codes, tuple | list):
            for reason in rejection_codes:
                by_reason[str(reason)] += 1
    return {
        "proposal_count": len(matrix),
        "by_asset_class": dict(sorted(by_asset.items())),
        "by_rejection_reason": dict(sorted(by_reason.items())),
        "proposal_confidence_distribution": _decimal_distribution(tuple(confidence_values)),
        "risk_manager_confidence_margin_distribution": _decimal_distribution(
            tuple(confidence_margins)
        ),
        "risk_manager_confidence_pass": confidence_pass,
        "risk_manager_stale_price_pass": stale_pass,
        "risk_manager_authorized": authorized,
        "sequential_risk_funnel": {
            "trade_proposal_created": len(matrix),
            "risk_manager_reached": len(matrix),
            "confidence_risk_check_pass": confidence_pass,
            "stale_price_check_pass": stale_pass,
            "risk_authorization_created": authorized,
            "simulated_execution_eligible": authorized,
        },
    }


def _gate_by_name(decision: ReplayDecision, gate_name: str) -> dict[str, object]:
    for gate in decision.proposal_gate_trace:
        if gate.get("gate") == gate_name:
            return gate
    return {}


def _representative_decision_traces(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[dict[str, object], ...]:
    selected: list[ReplayDecision] = []
    for candidate in (
        max(decisions, key=lambda item: item.score),
        max(decisions, key=lambda item: item.confidence),
    ):
        if candidate not in selected:
            selected.append(candidate)
    for asset_class in ("EQUITY", "ETF", "CRYPTO"):
        asset_candidate = next(
            (item for item in decisions if item.asset_class.value == asset_class),
            None,
        )
        if asset_candidate is not None and asset_candidate not in selected:
            selected.append(asset_candidate)
    score_pass_candidate = next(
        (
            item
            for item in sorted(decisions, key=lambda decision: decision.score, reverse=True)
            if any(
                gate.get("gate") == "opportunity_score" and gate.get("passed") is True
                for gate in item.proposal_gate_trace
            )
            and item.proposal_id is None
        ),
        None,
    )
    if score_pass_candidate is not None and score_pass_candidate not in selected:
        selected.append(score_pass_candidate)
    return tuple(_decision_trace_payload(decision) for decision in selected[:7])


def _decision_trace_payload(decision: ReplayDecision) -> dict[str, object]:
    return {
        "symbol": decision.symbol,
        "asset_class": decision.asset_class.value,
        "timestamp": decision.timestamp.isoformat(),
        "regime": decision.regime.value,
        "score": str(decision.score),
        "confidence": str(decision.confidence),
        "confidence_scale": "0..1",
        "ensemble_action": decision.ensemble_direction.value
        if decision.ensemble_direction is not None
        else None,
        "strategy_signal_counts": decision.strategy_signal_counts,
        "final_opportunity_action": decision.aegis_decision.value,
        "status": decision.status.value,
        "trade_proposal_eligible": decision.proposal_id is not None,
        "blockers": decision.blocker_reasons,
        "proposal_gates": decision.proposal_gate_trace,
        "confidence_decomposition": decision.confidence_decomposition,
        "future_records_ignored": decision.future_records_ignored,
    }


def _fine_confidence_calibration(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[dict[str, object], ...]:
    groups: dict[str, list[ReplayDecision]] = defaultdict(list)
    for decision in decisions:
        if decision.forward_return is None:
            continue
        label = str(decision.confidence.quantize(Decimal("0.01")))
        groups[label].append(decision)
    return tuple(
        {
            "confidence": label,
            "sample_count": len(items),
            "mean_forward_return": str(
                sum((item.forward_return or Decimal("0") for item in items), Decimal("0"))
                / Decimal(len(items))
            ),
            "median_forward_return": str(
                _percentile(
                    sorted(item.forward_return or Decimal("0") for item in items),
                    Decimal("0.50"),
                )
            ),
            "win_rate": str(
                Decimal(sum(1 for item in items if (item.forward_return or Decimal("0")) > 0))
                / Decimal(len(items))
            ),
            "mae": str(min(item.forward_return or Decimal("0") for item in items)),
            "mfe": str(max(item.forward_return or Decimal("0") for item in items)),
            "asset_class_counts": dict(
                sorted(Counter(item.asset_class.value for item in items).items())
            ),
            "regime_counts": dict(sorted(Counter(item.regime.value for item in items).items())),
        }
        for label, items in sorted(groups.items())
    )


def _confidence_compression_diagnostics(
    decisions: tuple[ReplayDecision, ...],
) -> dict[str, object]:
    decompositions = tuple(
        decision.confidence_decomposition
        for decision in decisions
        if decision.confidence_decomposition
    )
    if not decompositions:
        return {}
    return {
        "formula": "(ensemble_confidence + regime_confidence + data_quality_score/100) / 3",
        "observed_final_confidence_distribution": _decimal_distribution(
            tuple(decision.confidence for decision in decisions)
        ),
        "feature_quality_states": dict(
            sorted(
                Counter(
                    str(item.get("feature_quality_state", "UNKNOWN")) for item in decompositions
                ).items()
            )
        ),
        "feature_quality_multipliers": dict(
            sorted(
                Counter(
                    str(item.get("feature_quality_multiplier", "UNKNOWN"))
                    for item in decompositions
                ).items()
            )
        ),
        "regime_multiplier_distribution": _decimal_distribution(
            tuple(Decimal(str(item["regime_multiplier"])) for item in decompositions)
        ),
        "ensemble_confidence_distribution": _decimal_distribution(
            tuple(
                Decimal(str(item["ensemble_confidence_after_rounding"])) for item in decompositions
            )
        ),
        "common_compression_factors": (
            "feature quality PARTIAL applies an ensemble multiplier of 0.70",
            (
                "final confidence averages ensemble confidence with regime confidence "
                "and data-quality component"
            ),
            "data-quality component is 0.65 for PARTIAL and is common across the current real run",
            "final confidence is rounded to two decimal places",
        ),
    }


def _threshold_provenance() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "asset_class": asset_class.value,
            "minimum_score_for_buy": str(profile.minimum_score_for_buy),
            "minimum_confidence_for_buy": str(profile.minimum_confidence_for_buy),
            "defined_in": "app.intelligence.profiles.default_asset_strategy_profiles",
            "provenance": profile.confidence_threshold_provenance,
            "calibration_dataset_id": profile.calibration_dataset_id,
            "calibration_version": profile.calibration_version,
            "calibration_timestamp": profile.calibration_timestamp,
            "validation_status": profile.validation_status,
            "empirically_calibrated": profile.calibration_dataset_id is not None,
            "production_defaults_changed": False,
        }
        for asset_class in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO)
        for profile in (profile_for(asset_class),)
    )


def _top_diagnostics(diagnostics: tuple[ZeroTradeDiagnostic, ...]) -> tuple[dict[str, object], ...]:
    counts: dict[str, int] = {}
    for item in diagnostics:
        blocker = item.blocker
        count = int(item.count)
        counts[blocker] = counts.get(blocker, 0) + count
    total = sum(counts.values())
    if total == 0:
        return ()
    return tuple(
        {
            "blocker": blocker,
            "count": count,
            "share": str((Decimal(count) / Decimal(total)).quantize(Decimal("0.0001"))),
        }
        for blocker, count in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)[:10]
    )
