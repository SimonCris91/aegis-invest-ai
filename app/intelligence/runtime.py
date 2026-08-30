"""Safe offline runtime for the Step 7.8 intelligence engine."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.models import ApplicationConfig
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.market import MarketQuote
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import (
    BrokerEligibilitySnapshot,
    CandidateState,
    DataQualityStatus,
    OpportunityCandidate,
    OpportunityFeatures,
    UniversalInstrument,
)
from app.domain.versions import RANKING_VERSION, SCANNER_VERSION
from app.intelligence.models import AegisOpportunityAnalysis, MarketBar, TimeFrame
from app.intelligence.research import (
    DEFAULT_STRATEGY_RESEARCH_STORE_PATH,
    StrategyResearchStore,
    default_strategy_research_store,
)
from app.intelligence.service import AegisOpportunityIntelligenceEngine


def build_strategy_intelligence_report(
    config: ApplicationConfig,
    *,
    store: StrategyResearchStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    now = (clock or (lambda: datetime.now(UTC)))()
    if config.etoro_demo_execution_enabled:
        return {
            "status": "BLOCKED",
            "category": "DEMO_EXECUTION_ENABLED",
            "reason": "strategy-intelligence requires ETORO_DEMO_EXECUTION_ENABLED=false",
            "record_store_path": str(DEFAULT_STRATEGY_RESEARCH_STORE_PATH),
            "broker_write_calls": 0,
            "demo_execution_enabled": config.etoro_demo_execution_enabled,
            "real_execution_available": False,
        }
    analysis = _offline_analysis(now, confidence_profile=config.strategy.confidence_profile)
    research_store = store or default_strategy_research_store(DEFAULT_STRATEGY_RESEARCH_STORE_PATH)
    record_id = research_store.record_analysis(analysis)
    return {
        "status": "OFFLINE_VERIFIED",
        "engine": "Aegis Strategy & Opportunity Intelligence",
        "record_store_path": str(DEFAULT_STRATEGY_RESEARCH_STORE_PATH),
        "research_record_id": record_id,
        "instrument": analysis.candidate.instrument.symbol,
        "asset_class": analysis.candidate.asset_class.value,
        "decision": analysis.decision.value,
        "opportunity_score": str(analysis.opportunity_score.overall_score),
        "score_band": analysis.opportunity_score.band.value,
        "confidence": str(analysis.opportunity_score.confidence),
        "confidence_profile": config.strategy.confidence_profile,
        "confidence_model_version": analysis.opportunity_score.confidence_model_version,
        "confidence_semantics_version": analysis.opportunity_score.confidence_semantics_version,
        "market_regime": {
            "trend": analysis.regime.trend.value,
            "volatility": analysis.regime.volatility.value,
            "risk_environment": analysis.regime.risk_environment.value,
        },
        "portfolio_fit": analysis.portfolio_fit.status.value,
        "feature_quality": analysis.features.quality.value,
        "missing_timeframes": tuple(item.value for item in analysis.features.missing_timeframes),
        "broker_write_calls": 0,
        "demo_execution_enabled": config.etoro_demo_execution_enabled,
        "real_execution_available": False,
    }


def _offline_analysis(
    now: datetime, *, confidence_profile: str = "V1_LEGACY"
) -> AegisOpportunityAnalysis:
    instrument = _offline_instrument(now)
    quote = MarketQuote(
        instrument_id=777001,
        symbol="SYNTH-EQ",
        price=Decimal("62.50"),
        previous_close=Decimal("61.90"),
        bid=Decimal("62.45"),
        ask=Decimal("62.55"),
        as_of=now,
        currency=Currency.USD,
        source="synthetic-offline",
        market_status=MarketStatus.OPEN,
    )
    candidate = OpportunityCandidate(
        candidate_id="synthetic-intelligence-candidate",
        broker="fake",
        instrument=instrument,
        asset_class=AssetClass.EQUITY,
        market_status=MarketStatus.OPEN,
        quote=quote,
        broker_eligibility=instrument.broker_eligibility,
        policy_allowed=True,
        policy_version="asset-policy-v1",
        candidate_state=CandidateState.OPEN_AND_ALLOWED,
        data_quality=DataQualityStatus.GOOD,
        candidate_score=Decimal("70"),
        opportunity_factors=("offline deterministic trend fixture",),
        risk_factors=("synthetic research only",),
        rejection_reasons=(),
        features=OpportunityFeatures(
            mid_price=Decimal("62.50"),
            spread=Decimal("0.10"),
            spread_percentage=Decimal("0.0016"),
            short_term_momentum=Decimal("0.01"),
        ),
        confidence=Decimal("0.70"),
        rank=1,
        scanner_version=SCANNER_VERSION,
        ranking_version=RANKING_VERSION,
        timestamp=now,
    )
    portfolio = PortfolioSnapshot(
        as_of=now,
        currency=Currency.USD,
        cash=Decimal("900"),
        positions=(),
        reported_total_value=Decimal("900"),
        peak_value=Decimal("900"),
    )
    bars = _trend_bars(instrument, now=now, count=40)
    return AegisOpportunityIntelligenceEngine(
        confidence_profile=confidence_profile
    ).analyze_candidate(
        candidate=candidate,
        portfolio=portfolio,
        bars_by_timeframe={TimeFrame.ONE_DAY: bars},
        as_of=now,
        required_timeframes=(TimeFrame.ONE_DAY, TimeFrame.ONE_WEEK),
    )


def _offline_instrument(now: datetime) -> UniversalInstrument:
    eligibility = BrokerEligibilitySnapshot(
        broker="fake",
        broker_instrument_id="777001",
        symbol="SYNTH-EQ",
        checked_at=now,
        currency=Currency.USD,
        verified=True,
        allow_open=True,
        allow_close=True,
        minimum_order_value=Decimal("5"),
        settlement_type=SettlementType.REAL,
        leverage_configs=(1,),
    )
    return UniversalInstrument(
        broker="fake",
        broker_instrument_id="777001",
        symbol="SYNTH-EQ",
        display_name="Synthetic Equity",
        asset_class=AssetClass.EQUITY,
        currency=Currency.USD,
        market_status=MarketStatus.OPEN,
        tradeable=True,
        buy_allowed=True,
        sell_allowed=True,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("5"),
        bid=Decimal("62.45"),
        ask=Decimal("62.55"),
        last_price=Decimal("62.50"),
        price_timestamp=now,
        metadata_timestamp=now,
        broker_eligibility=eligibility,
    )


def _trend_bars(
    instrument: UniversalInstrument, *, now: datetime, count: int
) -> tuple[MarketBar, ...]:
    bars: list[MarketBar] = []
    for index in range(count):
        close = Decimal("50") + Decimal(index) * Decimal("0.30")
        open_price = close - Decimal("0.10")
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=now - timedelta(days=count - index),
                timeframe=TimeFrame.ONE_DAY,
                open=open_price,
                high=close + Decimal("0.20"),
                low=open_price - Decimal("0.20"),
                close=close,
                volume=Decimal("100000") + Decimal(index) * Decimal("1000"),
                currency=Currency.USD,
                source="synthetic-offline",
            )
        )
    return tuple(bars)
