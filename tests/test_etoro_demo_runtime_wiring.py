from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.demo import DEMO_ORDER_URL
from app.brokers.etoro.demo_pilot import (
    EtoroDemoSubmissionPackage,
    RiskCheckedEtoroDemoSubmissionGateway,
)
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse, TransportError
from app.brokers.models import PreflightDecision
from app.config.models import ApplicationConfig
from app.domain.enums import (
    AssetClass,
    BrokerExecutionMode,
    Currency,
    ExecutionPolicy,
    HoldingPeriod,
    MarketStatus,
    OperatingMode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import AuthorizedCapitalEnvelope, RiskContext
from app.domain.universe import UniversalInstrument
from app.execution.gate import RiskEnforcedExecutionGate
from app.intelligence.models import AegisDecision, FeatureQuality, MarketBar, TimeFrame
from app.orchestration.active_intelligence import (
    ActiveIntelligenceCycleRecord,
    CycleChangeClassification,
    DataHealthState,
)
from app.orchestration.active_runtime import (
    AegisEtoroAutomaticDemoRuntime,
    _DiagnosticReadOnlyHttpClient,
    _acquisition_audit_summary,
    _cash_reserve_aware_order_cap,
    _global_demo_selection_blockers,
    _execution_quote_check_time,
    _enrich_candidate_news_with_gdelt,
    _fair_execution_candidate_order,
    _build_live_submission_packages,
    _verified_crypto_exposure_headroom,
)
from app.news.intelligence import NewsProviderStatus, NewsSourceQuality, RawNewsItem
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.scanner.active import ActiveScannerBucket, ActiveScannerCandidate, ActiveScannerResult
from app.storage.sqlite import SqliteRecordStore


@pytest.mark.parametrize(
    ("offset", "wait_adjustment", "expected_wait", "expected_fresh"),
    [
        (0, 0, None, True),
        (2, 0, 2, True),
        (5, 0, 5, True),
        (6, 0, None, False),
        (-300, 0, None, True),
        (-301, 0, None, False),
        (2, -1, 2, False),
        (2, 301, 2, False),
    ],
)
def test_execution_quote_clock_wait_keeps_strict_freshness(
    monkeypatch, offset, wait_adjustment, expected_wait, expected_fresh
) -> None:
    from app.orchestration import active_runtime

    current = _now()
    quote_timestamp = current + timedelta(seconds=offset)
    waits = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current

    def advance(seconds):
        nonlocal current
        waits.append(seconds)
        current += timedelta(seconds=seconds + wait_adjustment)

    monkeypatch.setattr(active_runtime, "datetime", Clock)
    monkeypatch.setattr(active_runtime, "sleep", advance)
    checked_at = _execution_quote_check_time(quote_timestamp)

    assert waits == ([] if expected_wait is None else [expected_wait])
    assert (timedelta(0) <= checked_at - quote_timestamp <= timedelta(seconds=300)) is expected_fresh
    assert quote_timestamp == _now() + timedelta(seconds=offset)


def test_execution_package_checks_are_interleaved_by_asset_class() -> None:
    def candidate(symbol: str, asset_class: AssetClass) -> ActiveScannerCandidate:
        return _top().model_copy(update={"symbol": symbol, "asset_class": asset_class})

    ordered = _fair_execution_candidate_order(
        (
            candidate("BTC", AssetClass.CRYPTO),
            candidate("ETH", AssetClass.CRYPTO),
            candidate("AAPL", AssetClass.EQUITY),
            candidate("SPY", AssetClass.ETF),
            candidate("MSFT", AssetClass.EQUITY),
        )
    )

    assert [item.symbol for item in ordered] == ["AAPL", "SPY", "BTC", "MSFT", "ETH"]


def test_candidate_targeted_gdelt_enrichment_only_adds_fresh_linked_evidence(monkeypatch) -> None:
    from app.orchestration import active_runtime

    class CandidateProvider:
        provider_name = "GDELT_DOC"
        last_status = NewsProviderStatus.AVAILABLE
        last_diagnostics = {}
        read_calls = 1

        def __init__(self, **kwargs):
            self.query = kwargs["query"]

        def fetch_global_news(self, *, as_of):
            return (RawNewsItem(
                headline="TEST shares rise after product launch",
                source="Example News",
                published_at=as_of - timedelta(minutes=1),
                source_quality=NewsSourceQuality.SECONDARY_MEDIA,
                provider="GDELT_DOC",
            ),)

    monkeypatch.setattr(active_runtime, "GdeltNewsProvider", CandidateProvider)
    cycle = _cycle().model_copy(update={"news_provider_status": "PARTIAL"})

    enriched = _enrich_candidate_news_with_gdelt(
        cycle=cycle,
        scanner_result=_scanner_result((_top(),)),
        instruments=(_instrument(),),
        enabled="true",
    )

    assert enriched.news_asset_contexts["TEST"]["freshness"] == "NEWS_FRESH"
    assert enriched.news_provider_diagnostics["CANDIDATE_GDELT"]["fresh_contexts"] == ("TEST",)


def test_candidate_gdelt_query_prefers_asset_name_over_ambiguous_short_ticker(monkeypatch) -> None:
    from app.orchestration import active_runtime

    instrument = _instrument().model_copy(
        update={"symbol": "CI", "display_name": "Cigna Group"}
    )
    candidate = _top().model_copy(update={"symbol": "CI"})
    observed = {}

    class CandidateProvider:
        provider_name = "GDELT_DOC"
        last_status = NewsProviderStatus.AVAILABLE
        last_diagnostics = {}
        read_calls = 1

        def __init__(self, **kwargs):
            observed["query"] = kwargs["query"]

        def fetch_global_news(self, *, as_of):
            return (RawNewsItem(
                headline="Cigna Group shares rise after earnings",
                source="Example News",
                published_at=as_of - timedelta(minutes=1),
                source_quality=NewsSourceQuality.SECONDARY_MEDIA,
                provider="GDELT_DOC",
            ),)

    monkeypatch.setattr(active_runtime, "GdeltNewsProvider", CandidateProvider)
    enriched = _enrich_candidate_news_with_gdelt(
        cycle=_cycle().model_copy(update={"news_provider_status": "PARTIAL"}),
        scanner_result=_scanner_result((candidate,)),
        instruments=(instrument,),
        enabled="true",
    )

    assert '"Cigna Group"' in observed["query"]
    assert '"CI"' not in observed["query"]
    assert enriched.news_asset_contexts["CI"]["freshness"] == "NEWS_FRESH"


def test_candidate_gdelt_diagnostics_preserve_sanitized_retry_after(monkeypatch) -> None:
    from app.news.intelligence import NewsProviderError
    from app.orchestration import active_runtime

    class RateLimitedCandidateProvider:
        provider_name = "GDELT_DOC"
        last_status = NewsProviderStatus.RATE_LIMITED
        last_diagnostics = {
            "status": "RATE_LIMITED",
            "http_status": 429,
            "provider_error_message": "provider cooldown",
            "retry_after_seconds": 120,
            "sanitized_endpoint": "https://api.gdeltproject.org/api/v2/doc/doc",
        }
        read_calls = 0

        def __init__(self, **kwargs):
            pass

        def fetch_global_news(self, *, as_of):
            raise NewsProviderError(
                "rate limited",
                status=NewsProviderStatus.RATE_LIMITED,
                http_status=429,
                provider_error_message="provider cooldown",
                retry_after="120",
            )

    monkeypatch.setattr(active_runtime, "GdeltNewsProvider", RateLimitedCandidateProvider)
    monkeypatch.setattr(active_runtime, "_RUNTIME_CANDIDATE_GDELT_PROVIDERS", {})
    enriched = _enrich_candidate_news_with_gdelt(
        cycle=_cycle().model_copy(update={"news_provider_status": "PARTIAL"}),
        scanner_result=_scanner_result((_top(),)),
        instruments=(_instrument(),),
        enabled="true",
    )

    diagnostic = enriched.news_provider_diagnostics["CANDIDATE_GDELT"]
    assert diagnostic["provider_details"] == {
        "http_status": 429,
        "provider_error_message": "provider cooldown",
        "retry_after_seconds": 120,
    }


def test_candidate_targeted_gdelt_uses_canonical_candidates_when_buckets_are_missing(monkeypatch) -> None:
    from app.orchestration import active_runtime

    class CandidateProvider:
        provider_name = "GDELT_DOC"
        last_status = NewsProviderStatus.AVAILABLE
        last_diagnostics = {}
        read_calls = 1

        def __init__(self, **kwargs):
            pass

        def fetch_global_news(self, *, as_of):
            return (RawNewsItem(
                headline="TEST shares rise after product launch",
                source="Example News",
                published_at=as_of - timedelta(minutes=1),
                source_quality=NewsSourceQuality.SECONDARY_MEDIA,
                provider="GDELT_DOC",
            ),)

    monkeypatch.setattr(active_runtime, "GdeltNewsProvider", CandidateProvider)
    rehydrated = _scanner_result((_top(),)).model_copy(
        update={"top_opportunities": (), "watchlist": ()}
    )

    enriched = _enrich_candidate_news_with_gdelt(
        cycle=_cycle().model_copy(update={"news_provider_status": "PARTIAL"}),
        scanner_result=rehydrated,
        instruments=(_instrument(),),
        enabled="true",
    )

    assert enriched.news_provider_diagnostics["CANDIDATE_GDELT"]["candidates_checked"] == ("TEST",)
    assert enriched.news_asset_contexts["TEST"]["freshness"] == "NEWS_FRESH"


def test_candidate_targeted_alpaca_enriches_equities_before_gdelt(monkeypatch) -> None:
    from app.orchestration import active_runtime

    class CandidateAlpacaProvider:
        provider_name = "ALPACA_NEWS"
        last_status = NewsProviderStatus.AVAILABLE
        last_diagnostics = {}
        read_calls = 1

        def __init__(self, **kwargs):
            self.symbols = kwargs["symbols"]

        def set_tickers(self, tickers):
            self.tickers = tickers

        def fetch_global_news(self, *, as_of):
            return (RawNewsItem(
                headline="TEST company announces earnings",
                summary="TEST",
                source="Financial wire",
                published_at=as_of - timedelta(minutes=1),
                source_quality=NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
                provider="ALPACA_NEWS",
            ),)

    monkeypatch.setattr(active_runtime, "AlpacaNewsProvider", CandidateAlpacaProvider)
    enriched = _enrich_candidate_news_with_gdelt(
        cycle=_cycle().model_copy(update={"news_provider_status": "PARTIAL"}),
        scanner_result=_scanner_result((_top(),)),
        instruments=(_instrument(),),
        enabled="true",
        values={"ALPACA_API_KEY_ID": "test", "ALPACA_API_SECRET_KEY": "test"},
    )

    assert enriched.news_asset_contexts["TEST"]["freshness"] == "NEWS_FRESH"
    assert enriched.news_provider_diagnostics["CANDIDATE_ALPACA"]["fresh_contexts"] == ("TEST",)
    assert "CANDIDATE_GDELT" not in enriched.news_provider_diagnostics


def test_candidate_targeted_alpaca_maps_etoro_rth_ticker_to_provider_and_back(monkeypatch) -> None:
    from app.orchestration import active_runtime

    instrument = _instrument().model_copy(
        update={
            "symbol": "AMAT.RTH",
            "display_name": "Applied Materials",
            "asset_class": AssetClass.EQUITY,
        }
    )
    candidate = _top().model_copy(
        update={"symbol": "AMAT.RTH", "asset_class": AssetClass.EQUITY}
    )

    class CandidateAlpacaProvider:
        provider_name = "ALPACA_NEWS"
        last_status = NewsProviderStatus.AVAILABLE
        last_diagnostics = {}
        read_calls = 1

        def __init__(self, **kwargs):
            self.symbols = kwargs["symbols"]

        def set_tickers(self, tickers):
            self.tickers = tickers

        def fetch_global_news(self, *, as_of):
            assert self.symbols == ("AMAT",)
            return (RawNewsItem(
                headline="AMAT announces quarterly results",
                summary="AMAT",
                source="Financial wire",
                published_at=as_of - timedelta(minutes=1),
                source_quality=NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
                provider="ALPACA_NEWS",
            ),)

    monkeypatch.setattr(active_runtime, "AlpacaNewsProvider", CandidateAlpacaProvider)
    enriched = _enrich_candidate_news_with_gdelt(
        cycle=_cycle().model_copy(update={"news_provider_status": "PARTIAL"}),
        scanner_result=_scanner_result((candidate,)),
        instruments=(instrument,),
        enabled="true",
        values={"ALPACA_API_KEY_ID": "test", "ALPACA_API_SECRET_KEY": "test"},
    )

    assert enriched.news_asset_contexts["AMAT.RTH"]["freshness"] == "NEWS_FRESH"
    assert enriched.news_provider_diagnostics["CANDIDATE_ALPACA"]["provider_symbols"] == {
        "AMAT.RTH": "AMAT"
    }
    assert enriched.news_provider_diagnostics["CANDIDATE_ALPACA"]["fresh_contexts"] == (
        "AMAT.RTH",
    )
    assert "CANDIDATE_GDELT" not in enriched.news_provider_diagnostics


def test_candidate_targeted_alpaca_covers_crypto_with_usd_pair_alias(monkeypatch) -> None:
    from app.orchestration import active_runtime

    instrument = _instrument().model_copy(
        update={
            "symbol": "PUMP",
            "display_name": "Pump.Fun",
            "asset_class": AssetClass.CRYPTO,
        }
    )
    candidate = _top().model_copy(
        update={"symbol": "PUMP", "asset_class": AssetClass.CRYPTO}
    )

    class CandidateAlpacaProvider:
        provider_name = "ALPACA_NEWS"
        last_status = NewsProviderStatus.AVAILABLE
        last_diagnostics = {}
        read_calls = 1

        def __init__(self, **kwargs):
            self.symbols = kwargs["symbols"]

        def set_tickers(self, tickers):
            self.tickers = tickers

        def fetch_global_news(self, *, as_of):
            assert self.symbols == ("PUMPUSD",)
            return (RawNewsItem(
                headline="PUMPUSD crypto token rises after protocol upgrade",
                summary="PUMPUSD",
                source="Financial wire",
                published_at=as_of - timedelta(minutes=1),
                source_quality=NewsSourceQuality.MAJOR_FINANCIAL_NEWS,
                provider="ALPACA_NEWS",
            ),)

    monkeypatch.setattr(active_runtime, "AlpacaNewsProvider", CandidateAlpacaProvider)
    enriched = _enrich_candidate_news_with_gdelt(
        cycle=_cycle().model_copy(update={"news_provider_status": "PARTIAL"}),
        scanner_result=_scanner_result((candidate,)),
        instruments=(instrument,),
        enabled="true",
        values={"ALPACA_API_KEY_ID": "test", "ALPACA_API_SECRET_KEY": "test"},
    )

    assert enriched.news_asset_contexts["PUMP"]["freshness"] == "NEWS_FRESH"
    assert enriched.news_provider_diagnostics["CANDIDATE_ALPACA"]["provider_symbols"] == {
        "PUMP": "PUMPUSD"
    }
    assert enriched.news_provider_diagnostics["CANDIDATE_ALPACA"]["fresh_contexts"] == (
        "PUMP",
    )
    assert "CANDIDATE_GDELT" not in enriched.news_provider_diagnostics


@pytest.mark.parametrize(
    (
        "offset",
        "skew_limit",
        "reaches_eligibility",
        "eligibility_error",
        "expected_reason",
        "market_status",
    ),
    [
        (2, 5, True, ValueError, "ELIGIBILITY_READ_FAILED:ValueError", MarketStatus.OPEN),
        (2, 5, True, None, "BROKER_EXPLICITLY_DISALLOWS_OPENING", MarketStatus.OPEN),
        (2, 0, False, ValueError, "QUOTE_NOT_FRESH_FOR_EXECUTION", MarketStatus.OPEN),
        (6, 5, False, ValueError, "QUOTE_NOT_FRESH_FOR_EXECUTION", MarketStatus.OPEN),
        (-301, 5, False, ValueError, "QUOTE_NOT_FRESH_FOR_EXECUTION", MarketStatus.OPEN),
        (0, 5, False, ValueError, "MARKET_CLOSED", MarketStatus.CLOSED),
    ],
)
def test_live_packages_wait_for_small_clock_skew_before_eligibility(
    monkeypatch,
    offset,
    skew_limit,
    reaches_eligibility,
    eligibility_error,
    expected_reason,
    market_status,
) -> None:
    from app.brokers.etoro.demo_pilot import EtoroDemoPilotSettings
    from app.brokers.etoro.mapping import EtoroEligibilityDenied
    from app.orchestration import active_runtime

    current = _now()
    quote = SimpleNamespace(as_of=current + timedelta(seconds=offset))
    eligibility_calls = []
    quote_calls = []
    waits = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current

    def advance(seconds):
        nonlocal current
        waits.append(seconds)
        current += timedelta(seconds=seconds)

    def read_quote(*args, **kwargs):
        quote_calls.append(current)
        return quote

    def eligibility(*args, **kwargs):
        eligibility_calls.append(current)
        if eligibility_error is None:
            raise EtoroEligibilityDenied("broker explicitly disallows opening this instrument")
        raise eligibility_error("fixture ends at eligibility; no order permitted")

    client = SimpleNamespace(
        identity=lambda: None,
        demo_account=lambda _: SimpleNamespace(currency=Currency.EUR),
        resolve_instrument_id=lambda *a, **kw: SimpleNamespace(
            instrument_id=1001,
            internal_symbol_full="TEST",
            verified=True,
            structurally_supported=True,
            instrument_type="stock",
            market_status=market_status,
        ),
        quote=read_quote,
        demo_eligibility=eligibility,
    )
    monkeypatch.setattr(active_runtime, "datetime", Clock)
    monkeypatch.setattr(active_runtime, "sleep", advance)
    diagnostics = {}
    quote_diagnostics = {}
    packages = _build_live_submission_packages(
        cycle=_cycle(),
        scanner_result=_scanner_result((_top(),)),
        client=client,
        instrument_ids_by_symbol={"TEST": 1001},
        config=ApplicationConfig(authorized_capital_eur=Decimal("200")),
        settings=EtoroDemoPilotSettings(enabled=True, notional_eur=Decimal("200")),
        registry=SimpleNamespace(managed_demo_exposure=lambda _: Decimal("0")),
        diagnostics=diagnostics,
        quote_diagnostics=quote_diagnostics,
        maximum_future_quote_skew_seconds=skew_limit,
    )

    assert packages == {}
    assert bool(eligibility_calls) is reaches_eligibility
    if market_status is MarketStatus.CLOSED:
        assert waits == []
        assert quote_calls == []
        assert quote_diagnostics["TEST"]["status"] == "MARKET_CLOSED"
        assert quote_diagnostics["TEST"]["attempts"] == 0
    elif reaches_eligibility:
        assert waits == [2]
        assert len(quote_calls) == 1
        assert eligibility_calls[0] >= quote.as_of
        assert diagnostics["TEST"] == expected_reason
    else:
        assert waits == []
        assert diagnostics["TEST"] == expected_reason
    assert quote_diagnostics["TEST"]["attempts"] == len(quote_calls)
    assert quote_diagnostics["TEST"]["max_age_seconds"] == 300
    if market_status is not MarketStatus.CLOSED:
        assert quote_diagnostics["TEST"]["quote_as_of"] == quote.as_of.isoformat()
        assert quote_diagnostics["TEST"]["status"] in {
            "FRESH",
            "STALE",
            "FUTURE_TIMESTAMP",
        }


def test_candidate_execution_diagnostics_join_blockers_to_symbol() -> None:
    from app.orchestration.active_runtime import _candidate_execution_diagnostics

    cycle = _cycle().model_copy(update={
        "news_asset_contexts": {
            "TEST": {
                "freshness": "NEWS_SOURCE_UNAVAILABLE",
                "material_event_count": 0,
                "event_risk": "0",
            }
        }
    })
    rows = _candidate_execution_diagnostics(
        cycle=cycle,
        scanner_result=_scanner_result((_top(),)),
        package_diagnostics={"TEST": "PACKAGE_READY"},
        quote_diagnostics={"TEST": {
            "status": "FRESH",
            "quote_as_of": _now().isoformat(),
            "age_seconds": 2.0,
            "max_age_seconds": 300,
        }},
        pilot_result={"submissions": [{
            "symbol": "TEST",
            "sanitized_status": "RISK_MANAGER_REJECTED",
            "submitted": False,
            "risk_violation_codes": ["NEWS_DATA_UNAVAILABLE"],
            "preflight_reasons": [],
        }]},
    )

    assert len(rows) == 1
    assert rows[0]["symbol"] == "TEST"
    assert rows[0]["package_status"] == "PACKAGE_READY"
    assert rows[0]["quote"]["status"] == "FRESH"
    assert rows[0]["news"]["freshness"] == "NEWS_SOURCE_UNAVAILABLE"
    assert rows[0]["submission"]["risk_violation_codes"] == ("NEWS_DATA_UNAVAILABLE",)


def test_live_package_reserve_uses_full_portfolio_above_authorized_capital(monkeypatch) -> None:
    from app.brokers.etoro.demo_pilot import EtoroDemoPilotSettings
    from app.domain.portfolio import Position
    from app.orchestration import active_runtime
    from app.policies.defaults import default_asset_policy_engine

    config = ApplicationConfig(authorized_capital_eur=Decimal("200"))
    portfolio = PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.EUR,
        cash=Decimal("17"),
        positions=(Position(
            position_id="other",
            instrument_id=1002,
            symbol="OTHER",
            settlement_type=SettlementType.REAL,
            units=Decimal("233"),
            average_entry_price=Decimal("1"),
            market_price=Decimal("1"),
        ),),
    )
    quote = SimpleNamespace(as_of=_now())
    quote.model_copy = lambda **kwargs: quote
    client = SimpleNamespace(
        identity=lambda: None,
        demo_account=lambda _: SimpleNamespace(currency=Currency.EUR),
        resolve_instrument_id=lambda *a, **kw: SimpleNamespace(
            instrument_id=1001, internal_symbol_full="TEST", verified=True,
            structurally_supported=True, instrument_type="stock",
        ),
        quote=lambda *a, **kw: quote,
        demo_eligibility=lambda *a, **kw: None,
    )
    monkeypatch.setattr(active_runtime, "_execution_quote_check_time", lambda *a, **kw: _now())
    monkeypatch.setattr(active_runtime, "_preflight_market_status", lambda _: MarketStatus.OPEN)
    monkeypatch.setattr(active_runtime, "_instrument_from_eligibility", lambda *a: None)
    monkeypatch.setattr(active_runtime, "_portfolio_from_demo_snapshot", lambda *a, **kw: portfolio)
    diagnostics, sizing = {}, {}

    packages = _build_live_submission_packages(
        cycle=_cycle(), scanner_result=_scanner_result((_top(),)), client=client,
        instrument_ids_by_symbol={"TEST": 1001}, config=config,
        settings=EtoroDemoPilotSettings(enabled=True, notional_eur=Decimal("200")),
        registry=SimpleNamespace(managed_demo_exposure=lambda _: Decimal("0")),
        diagnostics=diagnostics, sizing_diagnostics=sizing,
    )

    reserve = max(config.risk.min_cash_reserve,
                  default_asset_policy_engine().policy_for(AssetClass.EQUITY).minimum_cash_reserve)
    assert portfolio.total_value == Decimal("250")
    assert packages == {}
    assert diagnostics["TEST"] == "DEMO_BUYING_POWER_UNAVAILABLE"
    assert sizing["TEST"]["zero_caps"] == ("cash_reserve_cap",)
    assert Decimal(sizing["TEST"]["cash_reserve_required"]) == portfolio.total_value * reserve
    assert Decimal(sizing["TEST"]["cash_reserve_shortfall"]) == (
        portfolio.total_value * reserve + Decimal("1") - portfolio.cash
    )


def test_live_package_does_not_mark_subminimum_safe_amount_ready(monkeypatch) -> None:
    from app.brokers.etoro.demo_pilot import EtoroDemoPilotSettings
    from app.domain.portfolio import Position
    from app.orchestration import active_runtime

    portfolio = PortfolioSnapshot(
        as_of=_now(), currency=Currency.EUR, cash=Decimal("22.99"),
        positions=(Position(
            position_id="other", instrument_id=1002, symbol="OTHER",
            settlement_type=SettlementType.REAL, units=Decimal("227.01"),
            average_entry_price=Decimal("1"), market_price=Decimal("1"),
        ),),
    )
    quote = SimpleNamespace(as_of=_now())
    quote.model_copy = lambda **kwargs: quote
    client = SimpleNamespace(
        identity=lambda: None,
        demo_account=lambda _: SimpleNamespace(currency=Currency.EUR),
        resolve_instrument_id=lambda *a, **kw: SimpleNamespace(
            instrument_id=1001, internal_symbol_full="TEST", verified=True,
            structurally_supported=True, instrument_type="stock",
        ),
        quote=lambda *a, **kw: quote,
        demo_eligibility=lambda *a, **kw: SimpleNamespace(minimum_position=Decimal("5")),
    )
    monkeypatch.setattr(active_runtime, "_execution_quote_check_time", lambda *a, **kw: _now())
    monkeypatch.setattr(active_runtime, "_preflight_market_status", lambda _: MarketStatus.OPEN)
    monkeypatch.setattr(active_runtime, "_instrument_from_eligibility", lambda *a: None)
    monkeypatch.setattr(active_runtime, "_portfolio_from_demo_snapshot", lambda *a, **kw: portfolio)
    diagnostics, sizing = {}, {}

    packages = _build_live_submission_packages(
        cycle=_cycle(), scanner_result=_scanner_result((_top(),)), client=client,
        instrument_ids_by_symbol={"TEST": 1001},
        config=ApplicationConfig(authorized_capital_eur=Decimal("200")),
        settings=EtoroDemoPilotSettings(enabled=True, notional_eur=Decimal("200")),
        registry=SimpleNamespace(managed_demo_exposure=lambda _: Decimal("0")),
        diagnostics=diagnostics, sizing_diagnostics=sizing,
    )

    assert packages == {}
    assert diagnostics["TEST"] == "SAFE_ORDER_BELOW_VERIFIED_MINIMUM"
    assert sizing["TEST"]["status"] == "SAFE_ORDER_BELOW_VERIFIED_MINIMUM"
    assert sizing["TEST"]["safe_order_amount"] == "4.49"
    assert sizing["TEST"]["verified_minimum_position"] == "5"
    assert sizing["TEST"]["limiting_caps"] == ("cash_reserve_cap",)


def test_crypto_portfolio_cap_uses_live_exposure_and_verified_catalog_classes() -> None:
    snapshot = SimpleNamespace(
        total_value=Decimal("100"),
        positions=(
            SimpleNamespace(instrument_id=10, current_exposure=Decimal("55")),
            SimpleNamespace(instrument_id=5, current_exposure=Decimal("20")),
        ),
    )
    catalog = (
        {"instrumentID": 10, "instrumentTypeID": 10},
        {"instrumentID": 5, "instrumentTypeID": 5},
    )

    assert _verified_crypto_exposure_headroom(snapshot, catalog) == Decimal("0.00")
    snapshot.positions[0].current_exposure = Decimal("42")
    assert _verified_crypto_exposure_headroom(snapshot, catalog) == Decimal("8.00")
    assert _verified_crypto_exposure_headroom(snapshot, catalog[:1]) is None
    assert _verified_crypto_exposure_headroom(
        snapshot, catalog + ({"instrumentID": 10, "instrumentTypeID": 6},)
    ) is None


def test_acquisition_audit_summary_keeps_counts_without_repeating_instrument_rows() -> None:
    acquisition = {
        "acquisition_status": "PARTIAL",
        "acquisition_instruments_requested": 2,
        "acquisition_outcome_counts": {"UPDATED": 1, "PROVIDER_UNAVAILABLE": 1},
        "acquisition_results": [
            {"symbol": "AAA", "outcome": "UPDATED"},
            {"symbol": "BBB", "outcome": "PROVIDER_UNAVAILABLE"},
        ],
    }

    summary = _acquisition_audit_summary(acquisition)

    assert summary == {
        "acquisition_status": "PARTIAL",
        "acquisition_instruments_requested": 2,
        "acquisition_outcome_counts": {"UPDATED": 1, "PROVIDER_UNAVAILABLE": 1},
        "acquisition_results_count": 2,
    }
    assert len(acquisition["acquisition_results"]) == 2


class FakeCycleProducer:
    def __init__(
        self,
        *,
        cycle: ActiveIntelligenceCycleRecord | None,
        scanner_result: ActiveScannerResult | None,
    ) -> None:
        self._cycle = cycle
        self.last_scanner_result = scanner_result
        self.calls = 0

    def run_if_new_bar_cycle(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: dict[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal = Decimal("200"),
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ActiveIntelligenceCycleRecord | None:
        self.calls += 1
        return self._cycle

    def run_cycle(self, **_: object) -> ActiveIntelligenceCycleRecord:
        raise AssertionError("legacy run_cycle must not be called by automatic Demo runtime")


class ScopedCycleProducer(FakeCycleProducer):
    """Test double that exposes the production asset-class scope argument."""

    def __init__(
        self,
        *,
        cycle: ActiveIntelligenceCycleRecord | None,
        scanner_result: ActiveScannerResult | None,
    ) -> None:
        super().__init__(cycle=cycle, scanner_result=scanner_result)
        self.scopes: list[frozenset[AssetClass] | None] = []

    def run_if_new_bar_cycle(
        self,
        *,
        scheduled_at: datetime,
        instruments: tuple[UniversalInstrument, ...],
        bars_by_symbol: dict[str, tuple[MarketBar, ...]],
        portfolio: PortfolioSnapshot,
        timeframe: TimeFrame,
        shadow_capital: Decimal = Decimal("200"),
        asset_classes: frozenset[AssetClass] | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> ActiveIntelligenceCycleRecord | None:
        self.scopes.append(asset_classes)
        return self._cycle


class StubEtoroTransport:
    def __init__(self, response: HttpResponse | None = None, *, error: bool = False) -> None:
        self.response = response or HttpResponse(202, {}, b'{"orderId":"order-1"}')
        self.error = error
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        if self.error:
            raise TransportError("synthetic post failure")
        return self.response


def test_diagnostic_http_client_blocks_broker_write_before_transport() -> None:
    transport = StubEtoroTransport()
    client = _DiagnosticReadOnlyHttpClient(transport)

    with pytest.raises(RuntimeError, match="DIAGNOSTIC_READ_ONLY_BROKER_WRITE_FORBIDDEN"):
        client.post_once("https://api.etoro.com/demo/order", {}, {"amount": 10})

    assert transport.calls == []


def test_no_cycle_never_reaches_demo_write_path(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=None, scanner_result=None)
    runtime = _runtime(tmp_path, producer=producer, values=_armed_values(), gateway=None)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "NO_CYCLE"
    assert producer.calls == 1
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0
    assert result["execution_admission_gate_reached"] is False


def test_runtime_requests_one_global_scan_instead_of_crypto_first_scope(
    tmp_path: Path,
) -> None:
    producer = ScopedCycleProducer(cycle=None, scanner_result=None)
    runtime = _runtime(tmp_path, producer=producer, values={}, gateway=None, enabled=False)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "NO_CYCLE"
    assert producer.scopes == [None]


def test_accepted_cycle_zero_top_does_not_invoke_demo(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result(()))
    runtime = _runtime(tmp_path, producer=producer, values=_armed_values(), gateway=None)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "NO_TOP_OPPORTUNITY"
    assert result["demo_broker_write_calls"] == 0


def test_accepted_cycle_projects_news_digest_into_runtime_result(tmp_path: Path) -> None:
    event = {"headline": "Verified material event", "impact_score": "0.82"}
    asset_context = {"freshness": "NEWS_FRESH", "material_event_count": 1}
    cycle = _cycle().model_copy(
        update={
            "news_event_digest": (event,),
            "news_asset_contexts": {"TEST": asset_context},
            "global_risk_context": {"freshness": "NEWS_FRESH"},
        }
    )
    producer = FakeCycleProducer(cycle=cycle, scanner_result=_scanner_result(()))
    runtime = _runtime(tmp_path, producer=producer, values=_armed_values(), gateway=None)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["news_event_digest"] == (event,)
    assert result["news_asset_contexts"] == {"TEST": asset_context}
    assert result["global_risk_context"] == {"freshness": "NEWS_FRESH"}


def test_accepted_top_disabled_pilot_submits_zero(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(tmp_path, producer=producer, values={}, gateway=None, enabled=False)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "DEMO_PILOT_DISABLED"
    assert result["eligible_count"] == 1
    assert result["demo_broker_write_calls"] == 0


def test_read_only_execution_mode_blocks_demo_before_packaging(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=None,
        execution_mode=BrokerExecutionMode.READ_ONLY,
        package_provider=lambda *_: (_ for _ in ()).throw(
            AssertionError("READ_ONLY must not package or submit")
        ),
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "EXECUTION_MODE_READ_ONLY"
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_advisory_policy_blocks_autonomous_demo_before_packaging(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=None,
        execution_policy=ExecutionPolicy.ADVISORY,
        package_provider=lambda *_: (_ for _ in ()).throw(
            AssertionError("ADVISORY must not package or submit")
        ),
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "EXECUTION_POLICY_NOT_AUTONOMOUS"
    assert result["top_opportunity_count"] == 1
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_kill_switch_blocks_autonomous_demo_before_packaging(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=None,
        kill_switch=True,
        package_provider=lambda *_: (_ for _ in ()).throw(
            AssertionError("kill switch must block packaging and submission")
        ),
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "KILL_SWITCH_ACTIVE"
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_accepted_top_invalid_notional_submits_zero(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values={"AEGIS_ETORO_DEMO_PILOT_NOTIONAL_EUR": "bad"},
        gateway=None,
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["blockers"] == ("MISSING_PILOT_NOTIONAL",)
    assert result["demo_broker_write_calls"] == 0


def test_accepted_top_without_verified_execution_package_fails_closed(
    tmp_path: Path,
) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    transport = StubEtoroTransport()
    gateway = _gateway(store, transport)
    runtime = _runtime(tmp_path, producer=producer, values=_armed_values(), gateway=gateway)

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "BLOCKED"
    assert result["blockers"] == ("VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE",)
    assert transport.calls == []
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def test_accepted_top_all_gates_pass_posts_exactly_once(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    transport = StubEtoroTransport()
    risk_manager = RiskManager(
        ApplicationConfig().risk,
        KillSwitch(active=False, reason="test", clock=_now),
        authorization_key=b"runtime-demo-risk-authorization-key!",
        clock=_now,
    )
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=KillSwitch(active=False, reason="test", clock=_now),
        registry=store,
        clock=_now,
    )
    package = _package(_intent_key(_cycle().cycle_id))
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=gateway,
        store=store,
        package_provider=lambda cycle, result: {"TEST": package},
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "SUBMITTED"
    assert result["submitted_count"] == 1
    assert result["demo_broker_write_calls"] == 1
    assert result["broker_write_calls_real"] == 0
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][1] == DEMO_ORDER_URL
    assert producer.calls == 1
    assert result["execution_admission_gate_reached"] is True


def test_restart_duplicate_blocks_second_demo_post(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "store.sqlite3")
    cycle = _cycle()
    first_transport = StubEtoroTransport()
    second_transport = StubEtoroTransport()
    package = _package(_intent_key(cycle.cycle_id))
    first = _runtime_with_real_gateway(
        tmp_path,
        store,
        cycle,
        first_transport,
        package_provider=lambda cycle, result: {"TEST": package},
    )
    second = _runtime_with_real_gateway(
        tmp_path,
        store,
        cycle,
        second_transport,
        package_provider=lambda cycle, result: {"TEST": package},
    )

    first_result = first.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )
    second_result = second.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert first_result["status"] == "SUBMITTED"
    assert second_result["blockers"] == ("DUPLICATE_CYCLE_CANDIDATE",)
    assert len(first_transport.calls) == 1
    assert second_transport.calls == []


def test_alpaca_only_mapping_fails_closed_without_demo_post(tmp_path: Path) -> None:
    producer = FakeCycleProducer(cycle=_cycle(), scanner_result=_scanner_result((_top(),)))
    transport = StubEtoroTransport()
    gateway = _gateway(SqliteRecordStore(tmp_path / "store.sqlite3"), transport)
    runtime = _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=gateway,
    )

    result = runtime.run_once(
        scheduled_at=_now(),
        instruments=(_instrument(broker_instrument_id="ALPACA_ONLY:TEST"),),
        bars_by_symbol={"TEST": _bars()},
        portfolio=_portfolio(),
        timeframe=TimeFrame.ONE_HOUR,
    )

    assert result["status"] == "BLOCKED"
    assert result["blockers"] == ("AMBIGUOUS_INSTRUMENT_MAPPING",)
    assert transport.calls == []
    assert result["demo_broker_write_calls"] == 0
    assert result["broker_write_calls_real"] == 0


def _runtime_with_real_gateway(
    tmp_path: Path,
    store: SqliteRecordStore,
    cycle: ActiveIntelligenceCycleRecord,
    transport: StubEtoroTransport,
    package_provider=None,
) -> AegisEtoroAutomaticDemoRuntime:
    producer = FakeCycleProducer(cycle=cycle, scanner_result=_scanner_result((_top(),)))
    gateway = _gateway(store, transport)
    return _runtime(
        tmp_path,
        producer=producer,
        values=_armed_values(),
        gateway=gateway,
        store=store,
        package_provider=package_provider,
    )


def _gateway(
    store: SqliteRecordStore,
    transport: StubEtoroTransport,
) -> RiskCheckedEtoroDemoSubmissionGateway:
    risk_manager = RiskManager(
        ApplicationConfig().risk,
        KillSwitch(active=False, reason="test", clock=_now),
        authorization_key=b"runtime-demo-risk-authorization-key!",
        clock=_now,
    )
    gateway = RiskCheckedEtoroDemoSubmissionGateway(
        environment=OperatingMode.ETORO_DEMO,
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        risk_manager=risk_manager,
        gate=RiskEnforcedExecutionGate(risk_manager),
        kill_switch=KillSwitch(active=False, reason="test", clock=_now),
        registry=store,
        clock=_now,
    )
    return gateway


def _runtime(
    tmp_path: Path,
    *,
    producer: FakeCycleProducer,
    values: dict[str, str],
    gateway,
    enabled: bool = True,
    store: SqliteRecordStore | None = None,
    package_provider=None,
    execution_mode: BrokerExecutionMode | None = None,
    execution_policy: ExecutionPolicy | None = None,
    kill_switch: bool = False,
) -> AegisEtoroAutomaticDemoRuntime:
    config = ApplicationConfig(
        operating_mode=OperatingMode.ETORO_DEMO,
        broker_execution_mode=(
            execution_mode
            or (BrokerExecutionMode.DEMO_EXECUTION if enabled else BrokerExecutionMode.READ_ONLY)
        ),
        authorized_capital_eur=Decimal("50"),
        etoro_api_enabled=True,
        etoro_demo_execution_enabled=enabled,
        etoro_demo_automatic_pilot_enabled=enabled,
        execution_policy=(
            execution_policy
            or (ExecutionPolicy.AUTONOMOUS if enabled else ExecutionPolicy.ADVISORY)
        ),
        kill_switch=kill_switch,
    )
    return AegisEtoroAutomaticDemoRuntime(
        config=config,
        values=values,
        orchestrator=producer,
        registry=store or SqliteRecordStore(tmp_path / "store.sqlite3"),
        gateway=gateway,
        package_provider=package_provider,
    )


def _armed_values() -> dict[str, str]:
    return {"AEGIS_ETORO_DEMO_PILOT_NOTIONAL_EUR": "10"}


def _package(idempotency_key: str) -> EtoroDemoSubmissionPackage:
    proposal = TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000901"),
        idempotency_key=idempotency_key,
        created_at=_now(),
        instrument_id=1001,
        symbol="TEST",
        asset_class=AssetClass.EQUITY,
        side=TradeSide.BUY,
        intent=TradeIntent.OPEN,
        amount=Decimal("10"),
        currency=Currency.EUR,
        target_weight=Decimal("0.05"),
        current_weight=Decimal("0"),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="accepted TOP_OPPORTUNITY runtime pilot",
        evidence=(
            EvidenceItem(
                source="fixture",
                timestamp=_now(),
                summary="causal",
                confidence=Decimal("0.9"),
            ),
        ),
        confidence=Decimal("0.80"),
        risk_factors=(),
        invalidation_conditions=("runtime test invalidation",),
        expected_holding_period=HoldingPeriod.DAYS,
    )
    return EtoroDemoSubmissionPackage(
        proposal=proposal,
        risk_context=RiskContext(
            evaluated_at=_now(),
            portfolio=_portfolio(),
            price=None,
            instrument=None,
            market_data_available=False,
            news_data_available=True,
            daily_new_trade_count=0,
            recent_idempotency_keys=frozenset(),
        ).model_copy(
            update={
                "price": _price(),
                "instrument": _instrument_metadata(),
                "market_data_available": True,
                "capital_envelope": AuthorizedCapitalEnvelope(
                    authorized_capital_eur=Decimal("50"), managed_exposure_eur=Decimal("0")
                ),
            }
        ),
        preflight=PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")),
    )


def _price():
    from app.domain.market import PriceSnapshot

    return PriceSnapshot(
        instrument_id=1001,
        symbol="TEST",
        price=Decimal("100"),
        as_of=_now(),
        source="fixture",
    )


def _instrument_metadata():
    from app.domain.market import InstrumentMetadata

    return InstrumentMetadata(
        instrument_id=1001,
        symbol="TEST",
        asset_class=AssetClass.EQUITY,
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("1"),
        metadata_as_of=_now(),
        source="fixture",
    )


def _intent_key(cycle_id: str) -> str:
    return f"etoro-demo-pilot:{cycle_id}:TEST:OPEN"


def _now() -> datetime:
    return datetime(2026, 8, 31, 10, tzinfo=UTC)


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.EUR,
        cash=Decimal("200"),
        reported_total_value=Decimal("200"),
        peak_value=Decimal("200"),
    )


def _instrument(*, broker_instrument_id: str = "1001") -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id=broker_instrument_id,
        symbol="TEST",
        display_name="TEST",
        asset_class=AssetClass.EQUITY,
        currency=Currency.EUR,
        exchange="TEST",
        market_status=MarketStatus.OPEN,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=_now(),
    )


def _bars() -> tuple[MarketBar, ...]:
    return (
        MarketBar(
            instrument=_instrument(),
            timestamp=_now(),
            timeframe=TimeFrame.ONE_HOUR,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
            currency=Currency.EUR,
            source="fixture",
            data_quality=FeatureQuality.GOOD,
        ),
    )


def _top() -> ActiveScannerCandidate:
    return ActiveScannerCandidate(
        symbol="TEST",
        full_asset_name="TEST",
        asset_class=AssetClass.EQUITY,
        timestamp=_now(),
        timeframe=TimeFrame.ONE_HOUR,
        current_market_state=MarketStatus.OPEN,
        opportunity_score=Decimal("75"),
        confidence=Decimal("0.8"),
        regime=("UPTREND",),
        decision=AegisDecision.BUY,
        bucket=ActiveScannerBucket.TOP_OPPORTUNITIES,
        data_quality_state=FeatureQuality.GOOD,
        current_position_state="NO_POSITION",
        freshness="FRESH",
        provider_provenance=("fixture",),
        affordable_fractionally=True,
        proposed_capital_allocation=Decimal("10"),
        remaining_simulated_cash=Decimal("190"),
        existing_exposure=Decimal("0"),
        diversification_concentration_impact="NEUTRAL",
        rank=1,
    )


def _scanner_result(candidates: tuple[ActiveScannerCandidate, ...]) -> ActiveScannerResult:
    return ActiveScannerResult(
        as_of=_now(),
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
        candidates=candidates,
        top_opportunities=_candidates_for_bucket(
            candidates,
            ActiveScannerBucket.TOP_OPPORTUNITIES,
        ),
        watchlist=_candidates_for_bucket(candidates, ActiveScannerBucket.WATCHLIST),
        no_trade=_candidates_for_bucket(candidates, ActiveScannerBucket.NO_TRADE),
        rejected=_candidates_for_bucket(candidates, ActiveScannerBucket.REJECTED),
        duplicate_decisions_prevented=0,
        existing_positions_monitored=0,
    )


def test_global_selection_coverage_ignores_non_actionable_no_trade_rows() -> None:
    no_trade = _top().model_copy(
        update={
            "symbol": "BTLN",
            "full_asset_name": "Brightline Interactive Inc",
            "decision": AegisDecision.IGNORE,
            "bucket": ActiveScannerBucket.NO_TRADE,
        }
    )
    cycle = _cycle().model_copy(
        update={
            "news_events_fresh": 1,
            "news_provider_status": "PARTIAL",
        }
    )
    instrument = _instrument().model_copy(update={"symbol": "BTLN", "exchange": "NYSE"})

    assert _global_demo_selection_blockers(
        cycle=cycle,
        scanner_result=_scanner_result((no_trade,)),
        instruments=(instrument,),
    ) == ()


def test_global_selection_coverage_ignores_hold_watchlist_rows() -> None:
    hold = _top().model_copy(
        update={
            "symbol": "ASYS",
            "full_asset_name": "Amtech Systems",
            "decision": AegisDecision.HOLD,
            "bucket": ActiveScannerBucket.WATCHLIST,
        }
    )
    cycle = _cycle().model_copy(
        update={
            "news_events_fresh": 1,
            "news_provider_status": "PARTIAL",
        }
    )
    instrument = _instrument().model_copy(update={"symbol": "ASYS", "exchange": "NASDAQ"})

    assert _global_demo_selection_blockers(
        cycle=cycle,
        scanner_result=_scanner_result((hold,)),
        instruments=(instrument,),
    ) == ()


def test_global_selection_coverage_does_not_deadlock_one_open_non_crypto_region() -> None:
    cycle = _cycle().model_copy(
        update={
            "news_events_fresh": 1,
            "news_provider_status": "PARTIAL",
        }
    )
    instrument = _instrument().model_copy(update={"symbol": "ASYS", "exchange": "NASDAQ"})
    candidate = _top().model_copy(update={"symbol": "ASYS"})

    assert _global_demo_selection_blockers(
        cycle=cycle,
        scanner_result=_scanner_result((candidate,)),
        instruments=(instrument,),
    ) == ()


def _candidates_for_bucket(
    candidates: tuple[ActiveScannerCandidate, ...],
    bucket: ActiveScannerBucket,
) -> tuple[ActiveScannerCandidate, ...]:
    return tuple(candidate for candidate in candidates if candidate.bucket is bucket)


def _cycle() -> ActiveIntelligenceCycleRecord:
    return ActiveIntelligenceCycleRecord(
        cycle_id="cycle-" + "b" * 64,
        scheduled_at=_now(),
        started_at=_now(),
        completed_at=_now(),
        market_data_timestamp=_now(),
        news_cutoff_timestamp=_now(),
        symbols_evaluated=("TEST",),
        positions_monitored=0,
        fresh_news_events=0,
        duplicate_events_ignored=0,
        material_events=0,
        global_risk_context={},
        top_opportunities=("TEST",),
        watchlist=(),
        no_trade=(),
        rejected=(),
        data_health_state=DataHealthState.HEALTHY,
        decision_change_events=(),
        change_classification=CycleChangeClassification.MARKET_STATE_CHANGED,
        scanner_result={},
        shadow_capital=Decimal("200"),
        available_simulated_cash=Decimal("200"),
        existing_exposure=Decimal("0"),
        allocation_diagnostics=(),
        broker_write_calls=0,
        scan_cycle_timestamp=_now(),
    )


def test_live_demo_order_cap_stays_below_risk_managers_cash_reserve_boundary() -> None:
    cash = Decimal("9739.58")
    portfolio_value = Decimal("96031.40")
    reserve_fraction = Decimal("0.10")

    cap = _cash_reserve_aware_order_cap(
        cash=cash,
        reference_value=portfolio_value,
        reserve_fraction=reserve_fraction,
    )

    assert cap == Decimal("135.44")
    assert cash - cap > portfolio_value * reserve_fraction


def test_live_demo_order_cap_blocks_when_only_rounding_buffer_remains() -> None:
    cash = Decimal("9501.00")
    portfolio_value = Decimal("95000")

    cap = _cash_reserve_aware_order_cap(
        cash=cash,
        reference_value=portfolio_value,
        reserve_fraction=Decimal("0.10"),
    )

    assert cap == Decimal("0.00")
