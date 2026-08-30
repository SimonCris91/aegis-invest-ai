"""Step 7.7 universal market scanner and asset policy tests."""

import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

import app.agent.service
import app.policies.engine
import app.scanner.catalog
import app.scanner.models
import app.scanner.ports
import app.scanner.ranking
import app.scanner.service
from app.brokers.etoro.client import BASE, SEARCH_PATH, EtoroReadClient
from app.brokers.etoro.mapping import map_universal_instrument_search
from app.brokers.etoro.scanner_adapter import EtoroMarketScannerAdapter
from app.brokers.fake import FakeMarketScannerAdapter
from app.brokers.models import (
    AccountKind,
    BrokerAccountContext,
    BrokerCapabilities,
    BrokerIdentity,
    DemoEligibility,
    DemoPortfolioSnapshot,
)
from app.brokers.registry import default_broker_registry
from app.config.models import ApplicationConfig, MarketScannerConfig, RiskPolicyConfig
from app.domain.enums import (
    AssetClass,
    Currency,
    MarketStatus,
    OperatingMode,
    RiskDecisionStatus,
    SettlementType,
    TradeSide,
)
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.domain.universe import (
    BrokerEligibilitySnapshot,
    CandidateState,
    DataQualityStatus,
    MarketScanResult,
    UniversalInstrument,
)
from app.main.__main__ import main
from app.policies.defaults import DEFAULT_POLICY_VERSION, conservative_asset_policies
from app.policies.engine import AssetPolicyEngine
from app.policies.models import AssetPolicy
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.scanner.catalog import InstrumentCatalog
from app.scanner.models import ScannerLimits
from app.scanner.ranking import OpportunityRankingEngine
from app.scanner.runtime import build_market_scan_report
from app.scanner.service import OpenMarketCandidateScanner
from app.storage.sqlite import SqliteRecordStore
from tests.conftest import TEST_INSTRUMENT_ID


class EtoroSequencedClient:
    def __init__(
        self,
        *,
        instruments: tuple[UniversalInstrument, ...],
        quote: MarketQuote,
        eligibility: DemoEligibility,
        identity: BrokerIdentity | None = None,
        demo: DemoPortfolioSnapshot | None = None,
    ) -> None:
        self._instruments = instruments
        self._quote = quote
        self._eligibility = eligibility
        self._identity = identity or BrokerIdentity(
            stable_user_id="stable-user-0001",
            demo_account_id=222,
            real_account_id=111,
        )
        self._demo = demo or DemoPortfolioSnapshot(
            context=BrokerAccountContext(
                stable_user_id="stable-user-0001",
                account_id=222,
                kind=AccountKind.DEMO,
            ),
            as_of=_now(),
            currency=Currency.USD,
            cash=Decimal("1000"),
            total_value=Decimal("1000"),
            account_balance=Decimal("1000"),
            positions=(),
        )
        self.calls: list[str] = []

    def identity(self) -> BrokerIdentity:
        self.calls.append("identity")
        return self._identity

    def demo_account(self, identity: BrokerIdentity) -> DemoPortfolioSnapshot:
        self.calls.append("demo_account")
        return self._demo

    def discover_instruments(
        self,
        *,
        as_of: datetime | None = None,
        page_size: int = 50,
        page_number: int = 1,
        search_text: str | None = None,
    ) -> tuple[UniversalInstrument, ...]:
        self.calls.append(f"discover:{page_number}:{page_size}:{search_text or ''}")
        return self._instruments[:page_size]

    def quote(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> MarketQuote:
        self.calls.append(f"quote:{instrument_id}:{symbol}:{currency.value}")
        return self._quote

    def demo_eligibility(
        self, instrument_id: int, symbol: str, *, currency: Currency = Currency.USD
    ) -> DemoEligibility:
        self.calls.append(f"eligibility:{instrument_id}:{symbol}:{currency.value}")
        return self._eligibility


def _now() -> datetime:
    return datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _capabilities(provider: str = "fake-broker") -> BrokerCapabilities:
    return BrokerCapabilities(
        provider=provider,
        mode=OperatingMode.SHADOW,
        authenticated_reads=True,
        demo_execution=False,
        real_execution=False,
    )


def _eligibility(
    instrument: UniversalInstrument,
    *,
    verified: bool = True,
    allow_open: bool | None = True,
    currency: Currency = Currency.USD,
    settlement_type: SettlementType = SettlementType.REAL,
    leverages: tuple[int, ...] = (1,),
    minimum: Decimal = Decimal("5"),
) -> BrokerEligibilitySnapshot:
    return BrokerEligibilitySnapshot(
        broker=instrument.broker,
        broker_instrument_id=instrument.broker_instrument_id,
        symbol=instrument.symbol,
        checked_at=_now(),
        currency=currency,
        verified=verified,
        allow_open=allow_open,
        allow_close=True,
        minimum_order_value=minimum,
        allowed_order_quantity_types=("amount",),
        settlement_type=settlement_type,
        leverage_configs=leverages,
    )


def _instrument(
    symbol: str,
    asset_class: AssetClass,
    *,
    instrument_id: int = TEST_INSTRUMENT_ID,
    status: MarketStatus = MarketStatus.OPEN,
    currency: Currency | None = Currency.USD,
    settlement_type: SettlementType | None = SettlementType.REAL,
    price_as_of: datetime | None = None,
    bid: Decimal | None = Decimal("9.99"),
    ask: Decimal | None = Decimal("10.01"),
    last_price: Decimal | None = Decimal("10"),
) -> UniversalInstrument:
    timestamp = price_as_of or _now()
    return UniversalInstrument(
        broker="fake",
        broker_instrument_id=str(instrument_id),
        symbol=symbol,
        display_name=f"{symbol} Test",
        asset_class=asset_class,
        currency=currency,
        market_status=status,
        tradeable=True,
        buy_allowed=True,
        sell_allowed=True,
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=settlement_type,
        minimum_order_value=Decimal("5"),
        bid=bid,
        ask=ask,
        last_price=last_price,
        price_timestamp=timestamp if last_price is not None else None,
        metadata_timestamp=_now(),
        tags=("test",),
    )


def _quote(
    instrument: UniversalInstrument,
    *,
    as_of: datetime | None = None,
    previous_close: Decimal | None = Decimal("9.80"),
    market_status: MarketStatus | None = None,
) -> MarketQuote:
    instrument_id = instrument.numeric_instrument_id
    assert instrument_id is not None
    return MarketQuote(
        instrument_id=instrument_id,
        symbol=instrument.symbol,
        price=instrument.last_price or Decimal("10"),
        bid=instrument.bid,
        ask=instrument.ask,
        as_of=as_of or _now(),
        currency=instrument.currency or Currency.USD,
        source="test",
        previous_close=previous_close,
        market_status=market_status or instrument.market_status,
    )


def _portfolio(
    *,
    cash: Decimal = Decimal("900"),
    position: Position | None = None,
    total: Decimal = Decimal("1000"),
) -> PortfolioSnapshot:
    positions = () if position is None else (position,)
    return PortfolioSnapshot(
        as_of=_now(),
        currency=Currency.USD,
        cash=cash,
        positions=positions,
        reported_total_value=total,
        peak_value=total,
    )


def _scanner(
    instruments: tuple[UniversalInstrument, ...],
    *,
    quotes: dict[str, MarketQuote] | None = None,
    eligibilities: dict[str, BrokerEligibilitySnapshot] | None = None,
    store: SqliteRecordStore | None = None,
    policy_engine: AssetPolicyEngine | None = None,
    shortlist: int = 20,
) -> OpenMarketCandidateScanner:
    discovery_limit = max(len(instruments), 1)
    effective_shortlist = min(shortlist, discovery_limit)
    adapter = FakeMarketScannerAdapter(
        capabilities=_capabilities(),
        instruments=instruments,
        quotes=quotes,
        eligibilities=eligibilities,
    )
    return OpenMarketCandidateScanner(
        adapter=adapter,
        policy_engine=policy_engine
        or AssetPolicyEngine(
            conservative_asset_policies(),
            policy_version=DEFAULT_POLICY_VERSION,
        ),
        ranking_engine=OpportunityRankingEngine(ranking_version="test-ranking-v1"),
        limits=ScannerLimits(
            discovery_limit=discovery_limit,
            ranked_shortlist_limit=effective_shortlist,
            deep_analysis_limit=min(effective_shortlist, 5),
        ),
        store=store,
    )


def test_universal_catalog_queries_multiple_asset_classes() -> None:
    instruments = (
        _instrument("EQ", AssetClass.EQUITY),
        _instrument("ETF", AssetClass.ETF, instrument_id=2),
        _instrument("BTC", AssetClass.CRYPTO, instrument_id=3, status=MarketStatus.CONTINUOUS_24_7),
        _instrument("EURUSD", AssetClass.FOREX, instrument_id=4),
        _instrument("SPX", AssetClass.INDEX, instrument_id=5),
        _instrument("GOLD", AssetClass.COMMODITY, instrument_id=6),
        _instrument("CFD", AssetClass.CFD, instrument_id=7),
        _instrument("UNK", AssetClass.UNKNOWN, instrument_id=8),
    )
    catalog = InstrumentCatalog(instruments)

    assert len(catalog.all()) == 8
    assert catalog.query(asset_class=AssetClass.CRYPTO)[0].symbol == "BTC"
    assert catalog.query(market_status=MarketStatus.CONTINUOUS_24_7)[0].symbol == "BTC"
    assert catalog.query(currency=Currency.USD)
    assert {item.asset_class for item in catalog.query_policy_compatible(_policy_engine())} == {
        AssetClass.CRYPTO,
        AssetClass.EQUITY,
        AssetClass.ETF,
    }


def test_default_policies_are_conservative_and_configurable() -> None:
    engine = _policy_engine()

    assert engine.policy_for(AssetClass.EQUITY).enabled
    assert engine.policy_for(AssetClass.ETF).long_allowed
    assert engine.policy_for(AssetClass.CRYPTO).max_new_trade_exposure == Decimal("0.05")
    assert not engine.policy_for(AssetClass.FOREX).enabled
    assert not engine.policy_for(AssetClass.CFD).enabled
    assert not engine.policy_for(AssetClass.FUTURE).enabled
    assert not engine.policy_for(AssetClass.OPTION).enabled
    assert not engine.policy_for(AssetClass.EQUITY).leverage_allowed
    assert not engine.policy_for(AssetClass.EQUITY).short_allowed

    forex_policy = AssetPolicy(
        asset_class=AssetClass.FOREX,
        enabled=True,
        long_allowed=True,
        require_broker_eligibility=False,
    )
    flexible = AssetPolicyEngine((forex_policy,), policy_version="future-policy-v1")
    assert flexible.policy_for(AssetClass.FOREX).enabled


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (MarketStatus.OPEN, CandidateState.OPEN_AND_ALLOWED),
        (MarketStatus.CLOSED, CandidateState.MARKET_CLOSED),
        (MarketStatus.PRE_MARKET, CandidateState.MARKET_CLOSED),
        (MarketStatus.AFTER_HOURS, CandidateState.MARKET_CLOSED),
        (MarketStatus.HALTED, CandidateState.MARKET_CLOSED),
        (MarketStatus.UNKNOWN, CandidateState.UNKNOWN),
    ],
)
def test_market_status_is_per_instrument_not_global(
    status: MarketStatus, expected: CandidateState
) -> None:
    equity = _instrument("EQ", AssetClass.EQUITY, status=status)
    result = _scan_one(equity)

    assert result.candidates[0].candidate_state is expected
    assert result.broker_write_calls == 0


def test_continuous_market_can_be_available_without_being_auto_buy() -> None:
    crypto = _instrument(
        "BTC",
        AssetClass.CRYPTO,
        instrument_id=3,
        status=MarketStatus.CONTINUOUS_24_7,
    )

    result = _scan_one(crypto)

    assert result.candidates[0].candidate_state is CandidateState.OPEN_AND_ALLOWED
    assert result.ranked_candidates[0].instrument.symbol == "BTC"
    assert result.ranked_candidates[0].risk_factors


def test_policy_disabled_leverage_short_and_cfd_blocks_are_distinct() -> None:
    engine = _policy_engine()
    portfolio = _portfolio()
    forex = _with_eligibility(_instrument("EURUSD", AssetClass.FOREX))
    equity = _with_eligibility(_instrument("EQ", AssetClass.EQUITY))
    cfd = _with_eligibility(_instrument("CFD", AssetClass.CFD, settlement_type=SettlementType.CFD))
    features = app.scanner.service.OpenMarketCandidateScanner._features(
        equity,
        quote=_quote(equity),
        portfolio=portfolio,
    )

    forex_decision = engine.evaluate(forex, portfolio=portfolio, as_of=_now(), features=features)
    short_decision = engine.evaluate(
        equity,
        portfolio=portfolio,
        as_of=_now(),
        features=features,
        side=TradeSide.SELL,
    )
    leverage_decision = engine.evaluate(
        equity,
        portfolio=portfolio,
        as_of=_now(),
        features=features,
        requested_leverage=Decimal("2"),
    )
    cfd_decision = engine.evaluate(cfd, portfolio=portfolio, as_of=_now(), features=features)

    assert forex_decision.candidate_state is CandidateState.OPEN_BUT_POLICY_BLOCKED
    assert "FOREX policy is disabled" in forex_decision.reasons
    assert short_decision.candidate_state is CandidateState.OPEN_BUT_POLICY_BLOCKED
    assert "short exposure is disabled by asset policy" in short_decision.reasons
    assert leverage_decision.candidate_state is CandidateState.OPEN_BUT_POLICY_BLOCKED
    assert "requested leverage exceeds asset policy" in leverage_decision.reasons
    assert cfd_decision.candidate_state is CandidateState.OPEN_BUT_POLICY_BLOCKED
    assert "CFD policy is disabled" in cfd_decision.reasons


def test_broker_ineligible_freshness_and_fx_blocks_are_not_collapsed() -> None:
    ineligible = _with_eligibility(
        _instrument("NOPE", AssetClass.EQUITY),
        verified=False,
        allow_open=False,
    )
    stale = _instrument(
        "STALE",
        AssetClass.EQUITY,
        instrument_id=2,
        price_as_of=_now() - timedelta(minutes=10),
    )
    foreign = _with_eligibility(
        _instrument("EUR", AssetClass.EQUITY, instrument_id=3, currency=Currency.EUR),
        currency=Currency.EUR,
    )

    assert _scan_one(ineligible).candidates[0].candidate_state is CandidateState.BROKER_INELIGIBLE
    assert _scan_one(stale).candidates[0].candidate_state is CandidateState.STALE_DATA
    assert _scan_one(foreign).candidates[0].candidate_state is CandidateState.FX_UNAVAILABLE


def test_missing_price_or_optional_bid_ask_affects_data_quality() -> None:
    missing = _with_eligibility(
        _instrument("MISS", AssetClass.EQUITY, bid=None, ask=None, last_price=None)
    )
    partial = _with_eligibility(
        _instrument("PART", AssetClass.EQUITY, instrument_id=2, bid=None, ask=None)
    )

    missing_result = _scanner(
        (missing,),
        quotes={},
        eligibilities={missing.key: cast(BrokerEligibilitySnapshot, missing.broker_eligibility)},
    ).scan(portfolio=_portfolio(), as_of=_now())
    partial_result = _scan_one(partial)

    assert missing_result.candidates[0].data_quality is DataQualityStatus.INSUFFICIENT
    assert missing_result.candidates[0].candidate_state is CandidateState.INSUFFICIENT_METADATA
    assert partial_result.candidates[0].data_quality is DataQualityStatus.PARTIAL
    assert partial_result.candidates[0].candidate_state is CandidateState.OPEN_AND_ALLOWED


def test_portfolio_concentration_and_drawdown_block_risk_budget() -> None:
    instrument = _with_eligibility(_instrument("EQ", AssetClass.EQUITY))
    position = Position(
        position_id="p1",
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="EQ",
        settlement_type=SettlementType.REAL,
        units=Decimal("30"),
        average_entry_price=Decimal("10"),
        market_price=Decimal("10"),
    )
    concentrated = _portfolio(cash=Decimal("700"), position=position, total=Decimal("1000"))
    drawdown = concentrated.model_copy(update={"peak_value": Decimal("1500")})

    concentrated_state = _scan_one(instrument, portfolio=concentrated).candidates[0]
    drawdown_state = _scan_one(instrument, portfolio=drawdown).candidates[0]

    assert concentrated_state.candidate_state is CandidateState.RISK_BUDGET_BLOCKED
    assert (
        "current instrument exposure exceeds asset policy" in concentrated_state.rejection_reasons
    )
    assert drawdown_state.candidate_state is CandidateState.RISK_BUDGET_BLOCKED
    assert "portfolio drawdown exceeds asset policy budget" in drawdown_state.rejection_reasons


def test_ranking_is_deterministic_and_top_n_limits_agent_surface() -> None:
    strong = _with_eligibility(
        _instrument("STRONG", AssetClass.EQUITY, instrument_id=1, bid=Decimal("9.99"))
    )
    weak = _with_eligibility(
        _instrument(
            "WEAK",
            AssetClass.EQUITY,
            instrument_id=2,
            bid=Decimal("9.50"),
            ask=Decimal("10.50"),
        )
    )
    result = _scanner(
        (weak, strong),
        quotes={weak.key: _quote(weak), strong.key: _quote(strong)},
        eligibilities={
            weak.key: cast(BrokerEligibilitySnapshot, weak.broker_eligibility),
            strong.key: cast(BrokerEligibilitySnapshot, strong.broker_eligibility),
        },
        shortlist=1,
    ).scan(portfolio=_portfolio(), as_of=_now())

    assert len(result.ranked_candidates) == 1
    assert result.ranked_candidates[0].instrument.symbol == "STRONG"
    assert result.ranked_candidates[0].rank == 1
    assert result.ranked_candidates[0].candidate_score > Decimal("0")


def test_scanner_performs_zero_writes_and_persists_secret_free_summary(tmp_path: Path) -> None:
    path = tmp_path / "scan.sqlite3"
    store = SqliteRecordStore(path)
    instrument = _with_eligibility(_instrument("EQ", AssetClass.EQUITY))
    result = _scanner((instrument,), store=store).scan(portfolio=_portfolio(), as_of=_now())

    assert result.broker_write_calls == 0
    assert result.persisted_record_id is not None
    persisted = store.list("market-scan-summary")[0]
    payload = json.dumps(persisted, sort_keys=True)
    assert "api-secret" not in payload
    assert "user-secret" not in payload
    assert "x-api-key" not in payload
    assert persisted["broker_write_calls"] == 0


def test_scanner_works_with_fake_and_etoro_adapter_without_core_changes() -> None:
    instrument = _with_eligibility(_instrument("EQ", AssetClass.EQUITY))
    fake_result = _scanner((instrument,)).scan(portfolio=_portfolio(), as_of=_now())
    demo_eligibility = DemoEligibility(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="EQ",
        currency=Currency.USD,
        minimum_position=Decimal("5"),
        allow_open=True,
        settlement_type=SettlementType.REAL,
        leverage=1,
        verified=True,
    )
    client = EtoroSequencedClient(
        instruments=(instrument.model_copy(update={"broker": "etoro"}),),
        quote=_quote(instrument),
        eligibility=demo_eligibility,
    )
    adapter = EtoroMarketScannerAdapter(cast(EtoroReadClient, client))
    etoro_result = OpenMarketCandidateScanner(
        adapter=adapter,
        policy_engine=_policy_engine(),
        ranking_engine=OpportunityRankingEngine(ranking_version="test-ranking-v1"),
        limits=ScannerLimits(discovery_limit=1, ranked_shortlist_limit=1, deep_analysis_limit=1),
    ).scan(portfolio=_portfolio(), as_of=_now())

    assert fake_result.total_discovered == 1
    assert etoro_result.total_discovered == 1
    assert etoro_result.broker_write_calls == 0
    assert all(
        "eligibility" in call or "quote" in call or "discover" in call for call in client.calls
    )


def test_etoro_universal_mapping_uses_provider_metadata_not_ticker_inference() -> None:
    raw = {
        "items": [
            _search_item(1, "AAPL", "Stocks"),
            _search_item(2, "SPY", "ETF"),
            _search_item(3, "BTC", "Crypto"),
            _search_item(4, "EURUSD", "Currencies"),
            _search_item(5, "SPX500", "Indices"),
            _search_item(6, "GOLD", "Commodities"),
            _search_item(7, "X", "CFD"),
            _search_item(8, "MYSTERY", "Something Else"),
            {"instrumentId": 0, "internalSymbolFull": "BAD", "instrumentType": "Stocks"},
            {"internalSymbolFull": "MISSING", "instrumentType": "Stocks"},
        ]
    }

    instruments = map_universal_instrument_search(raw, as_of=_now())

    assert [item.asset_class for item in instruments] == [
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.CRYPTO,
        AssetClass.FOREX,
        AssetClass.INDEX,
        AssetClass.COMMODITY,
        AssetClass.CFD,
        AssetClass.UNKNOWN,
    ]
    assert all(item.broker == "etoro" for item in instruments)


def test_read_client_uses_documented_search_for_broad_discovery() -> None:
    class Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request(
            self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
        ) -> object:
            self.calls.append((method, url))
            return type(
                "Response",
                (),
                {
                    "status": 200,
                    "headers": {},
                    "body": json.dumps({"items": [_search_item(1, "AAPL", "Stocks")]}).encode(),
                    "json": lambda self: json.loads(self.body.decode("utf-8")),
                },
            )()

    from app.brokers.etoro.auth import EtoroCredentials
    from app.brokers.etoro.http import DisciplinedHttpClient, HttpTransport

    transport = Transport()
    client = EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(cast(HttpTransport, transport), max_read_attempts=1),
    )

    instruments = client.discover_instruments(as_of=_now(), page_size=25, page_number=2)

    assert instruments[0].symbol == "AAPL"
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1].startswith(BASE + SEARCH_PATH)
    assert "fields=" in transport.calls[0][1]
    assert "pageSize=25" in transport.calls[0][1]
    assert "pageNumber=2" in transport.calls[0][1]
    assert "internalSymbolFull=" not in transport.calls[0][1]


def test_core_scanner_imports_no_etoro_implementation() -> None:
    for module in (
        app.scanner.catalog,
        app.scanner.models,
        app.scanner.ports,
        app.scanner.ranking,
        app.scanner.service,
    ):
        assert "app.brokers.etoro" not in inspect.getsource(module)


def test_scanner_ranking_policy_and_agent_cannot_submit_orders() -> None:
    for module in (
        app.scanner.service,
        app.scanner.ranking,
        app.policies.engine,
        app.agent.service,
    ):
        source = inspect.getsource(module)
        assert "submit_demo" not in source
        assert "post_once" not in source
        assert "market-open-orders" not in source


def test_agent_can_receive_ranked_multi_asset_candidates_without_secrets(
    now: datetime,
    portfolio: PortfolioSnapshot,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
) -> None:
    candidate = _scan_one(
        _with_eligibility(_instrument("TEST", AssetClass.EQUITY))
    ).ranked_candidates[0]
    from app.agent.context import AegisAgentContext
    from app.agent.safety import build_sanitized_ai_payload

    context = AegisAgentContext(
        portfolio=portfolio,
        quotes=(market_quote,),
        news=(news_item,),
        instruments=(instrument,),
        candidates=(candidate,),
        analysis_timestamp=now,
        strategy=ApplicationConfig().strategy,
    )
    payload = build_sanitized_ai_payload(context)

    assert payload["ranked_candidates"][0]["symbol"] == "TEST"
    assert "authorization" not in json.dumps(payload).casefold()
    assert "api-secret" not in json.dumps(payload)


def test_risk_manager_default_blocks_cfd_but_explicit_policy_can_change_future_behavior(
    now: datetime,
    portfolio: PortfolioSnapshot,
    price: PriceSnapshot,
) -> None:
    proposal = _proposal(now, settlement_type=SettlementType.CFD, asset_class=AssetClass.CFD)
    instrument = _risk_instrument(
        now,
        settlement_type=SettlementType.CFD,
        asset_class=AssetClass.CFD,
    )
    default_manager = RiskManager(
        RiskPolicyConfig(),
        KillSwitch(active=False, reason="test", clock=lambda: now),
        authorization_key=b"step77-risk-manager-default-key!",
        clock=lambda: now,
    )
    context = RiskContext(
        evaluated_at=now,
        portfolio=portfolio,
        price=price.model_copy(update={"instrument_id": proposal.instrument_id}),
        instrument=instrument,
        market_data_available=True,
        news_data_available=True,
        daily_new_trade_count=0,
    )

    blocked = default_manager.evaluate(proposal, context)
    assert blocked.decision.status is RiskDecisionStatus.REJECTED

    cfd_policy = AssetPolicy(
        asset_class=AssetClass.CFD,
        enabled=True,
        long_allowed=True,
        require_broker_eligibility=False,
    )
    future_manager = RiskManager(
        RiskPolicyConfig(),
        KillSwitch(active=False, reason="test", clock=lambda: now),
        authorization_key=b"step77-risk-manager-future-key-32-bytes",
        asset_policy_engine=AssetPolicyEngine((cfd_policy,), policy_version="future-cfd-policy"),
        clock=lambda: now,
    )
    approved = future_manager.evaluate(proposal, context)
    assert approved.decision.status is RiskDecisionStatus.APPROVED


def test_scan_markets_cli_runtime_fails_closed_without_credentials() -> None:
    payload = build_market_scan_report(ApplicationConfig(), values={})

    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["category"] == "CREDENTIALS"
    assert payload["broker_write_calls"] == 0
    assert payload["real_execution_available"] is False


def test_scan_markets_runtime_with_etoro_stub_is_live_verified_and_persistent(
    tmp_path: Path,
) -> None:
    instrument = _instrument("EQ", AssetClass.EQUITY).model_copy(update={"broker": "etoro"})
    client = EtoroSequencedClient(
        instruments=(instrument,),
        quote=_quote(instrument),
        eligibility=DemoEligibility(
            instrument_id=TEST_INSTRUMENT_ID,
            symbol="EQ",
            currency=Currency.USD,
            minimum_position=Decimal("5"),
            allow_open=True,
            settlement_type=SettlementType.REAL,
            leverage=1,
            verified=True,
        ),
    )
    store = SqliteRecordStore(tmp_path / "scan.sqlite3")

    payload = build_market_scan_report(
        ApplicationConfig(etoro_api_enabled=True),
        values={"ETORO_API_KEY": "api-secret", "ETORO_USER_KEY": "user-secret"},
        client=cast(EtoroReadClient, client),
        store=store,
        clock=_now,
    )

    assert payload["status"] == "LIVE_VERIFIED"
    assert payload["markets_scanned"] == 1
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert store.list("market-scan-summary")
    serialized = json.dumps(payload, sort_keys=True)
    assert "api-secret" not in serialized
    assert "user-secret" not in serialized
    assert "stable-user-0001" not in serialized


def test_scan_markets_runtime_blocks_when_demo_execution_is_enabled() -> None:
    payload = build_market_scan_report(
        ApplicationConfig(
            operating_mode=OperatingMode.ETORO_DEMO,
            etoro_api_enabled=True,
            etoro_demo_execution_enabled=True,
        ),
        values={"ETORO_API_KEY": "api-secret", "ETORO_USER_KEY": "user-secret"},
        clock=_now,
    )

    assert payload["status"] == "BLOCKED"
    assert payload["category"] == "DEMO_EXECUTION_ENABLED"
    assert payload["broker_write_calls"] == 0


def test_scan_markets_cli_is_read_only_and_secret_free_when_not_configured(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(("scan-markets",), values={}) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["broker_write_calls"] == 0
    assert payload["demo_execution_enabled"] is False
    assert payload["real_execution_available"] is False
    assert "api-secret" not in output
    assert "user-secret" not in output


def test_default_broker_registry_prepares_future_brokers_without_live_integration() -> None:
    registry = default_broker_registry()

    assert registry.get("etoro") is not None
    assert registry.get("fake") is not None
    ibkr = registry.get("interactive-brokers")
    alpaca = registry.get("alpaca")
    assert ibkr is not None and not ibkr.live_adapter_available
    assert alpaca is not None and not alpaca.live_adapter_available
    assert all(not entry.capabilities.real_execution for entry in registry.list())


def test_scanner_configuration_is_typed_and_ordered() -> None:
    config = MarketScannerConfig(
        discovery_limit=10,
        ranked_shortlist_limit=5,
        deep_analysis_limit=2,
        etoro_search_text="crypto",
        etoro_max_pages=2,
    )

    assert config.etoro_search_text == "crypto"
    with pytest.raises(ValueError):
        MarketScannerConfig(
            discovery_limit=5,
            ranked_shortlist_limit=10,
            deep_analysis_limit=2,
        )


def _policy_engine() -> AssetPolicyEngine:
    return AssetPolicyEngine(conservative_asset_policies(), policy_version=DEFAULT_POLICY_VERSION)


def _with_eligibility(
    instrument: UniversalInstrument,
    *,
    verified: bool = True,
    allow_open: bool | None = True,
    currency: Currency = Currency.USD,
) -> UniversalInstrument:
    return instrument.model_copy(
        update={
            "broker_eligibility": _eligibility(
                instrument,
                verified=verified,
                allow_open=allow_open,
                currency=currency,
            ),
            "currency": currency,
        }
    )


def _scan_one(
    instrument: UniversalInstrument, *, portfolio: PortfolioSnapshot | None = None
) -> MarketScanResult:
    eligibility = instrument.broker_eligibility or _eligibility(
        instrument,
        currency=instrument.currency or Currency.USD,
    )
    return _scanner(
        (instrument,),
        quotes={instrument.key: _quote(instrument, as_of=instrument.price_timestamp or _now())},
        eligibilities={instrument.key: eligibility},
    ).scan(portfolio=portfolio or _portfolio(), as_of=_now())


def _search_item(instrument_id: int, symbol: str, instrument_type: str) -> dict[str, object]:
    return {
        "instrumentId": instrument_id,
        "internalSymbolFull": symbol,
        "displayname": f"{symbol} Display",
        "instrumentType": instrument_type,
        "isOpen": True,
        "isExchangeOpen": True,
        "isCurrentlyTradable": True,
        "isBuyEnabled": True,
        "isHiddenFromClient": False,
        "isDelisted": False,
        "isActiveInPlatform": True,
        "currentRate": "10",
    }


def _proposal(
    now: datetime,
    *,
    settlement_type: SettlementType,
    asset_class: AssetClass,
) -> TradeProposal:
    from uuid import UUID

    from app.domain.enums import HoldingPeriod, TradeIntent
    from app.domain.market import EvidenceItem

    return TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000777"),
        idempotency_key="step77-proposal-key",
        created_at=now,
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        asset_class=asset_class,
        side=TradeSide.BUY,
        intent=TradeIntent.INCREASE,
        amount=Decimal("10"),
        currency=Currency.USD,
        target_weight=Decimal("0.15"),
        current_weight=Decimal("0.10"),
        leverage=1,
        settlement_type=settlement_type,
        reason="step77 policy test",
        evidence=(
            EvidenceItem(
                source="test",
                timestamp=now,
                summary="evidence",
                confidence=Decimal("0.90"),
            ),
        ),
        confidence=Decimal("0.90"),
        risk_factors=("market risk",),
        invalidation_conditions=("test invalidation",),
        expected_holding_period=HoldingPeriod.MONTHS,
    )


def _risk_instrument(
    now: datetime,
    *,
    settlement_type: SettlementType,
    asset_class: AssetClass,
) -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        asset_class=asset_class,
        settlement_type=settlement_type,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("1"),
        metadata_as_of=now,
        source="test",
    )
