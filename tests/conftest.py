"""Deterministic synthetic fixtures; IDs do not represent real eToro instruments."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.config.models import RiskPolicyConfig
from app.domain.enums import (
    Currency,
    HoldingPeriod,
    MarketStatus,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem, InstrumentMetadata, MarketQuote, NewsItem, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.reporting.audit import InMemoryAuditSink
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager

TEST_INSTRUMENT_ID = 999_999_991
OTHER_TEST_INSTRUMENT_ID = 999_999_992


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def portfolio(now: datetime) -> PortfolioSnapshot:
    positions = (
        Position(
            position_id="synthetic-position-target",
            instrument_id=TEST_INSTRUMENT_ID,
            symbol="TEST",
            settlement_type=SettlementType.REAL,
            units=Decimal("2"),
            average_entry_price=Decimal("10"),
            market_price=Decimal("10"),
        ),
        Position(
            position_id="synthetic-position-other",
            instrument_id=OTHER_TEST_INSTRUMENT_ID,
            symbol="OTHER",
            settlement_type=SettlementType.REAL,
            units=Decimal("2"),
            average_entry_price=Decimal("10"),
            market_price=Decimal("10"),
        ),
    )
    return PortfolioSnapshot(
        as_of=now,
        currency=Currency.USD,
        cash=Decimal("160"),
        positions=positions,
        reported_total_value=Decimal("200"),
        peak_value=Decimal("210"),
    )


@pytest.fixture
def proposal(now: datetime) -> TradeProposal:
    return TradeProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000101"),
        idempotency_key="synthetic-proposal-0001",
        created_at=now,
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        side=TradeSide.BUY,
        intent=TradeIntent.INCREASE,
        amount=Decimal("10"),
        currency=Currency.USD,
        target_weight=Decimal("0.15"),
        current_weight=Decimal("0.10"),
        leverage=1,
        settlement_type=SettlementType.REAL,
        reason="synthetic test proposal",
        evidence=(
            EvidenceItem(
                source="synthetic-fixture",
                timestamp=now,
                summary="deterministic evidence",
                confidence=Decimal("0.90"),
            ),
        ),
        confidence=Decimal("0.80"),
        risk_factors=("synthetic risk",),
        invalidation_conditions=("synthetic invalidation",),
        expected_holding_period=HoldingPeriod.MONTHS,
    )


@pytest.fixture
def price(now: datetime) -> PriceSnapshot:
    return PriceSnapshot(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        price=Decimal("10"),
        as_of=now,
        source="synthetic-fixture",
    )


@pytest.fixture
def instrument(now: datetime) -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        settlement_type=SettlementType.REAL,
        is_valid=True,
        is_tradable=True,
        allows_long=True,
        allows_short=False,
        allowed_leverages=(1,),
        min_position_amount=Decimal("1"),
        metadata_as_of=now,
        source="synthetic-fixture",
    )


@pytest.fixture
def market_quote(now: datetime) -> MarketQuote:
    return MarketQuote(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        price=Decimal("10"),
        as_of=now,
        currency=Currency.USD,
        source="synthetic-fixture",
        previous_close=Decimal("9.80"),
        bid=Decimal("9.99"),
        ask=Decimal("10.01"),
        market_status=MarketStatus.OPEN,
    )


@pytest.fixture
def news_item(now: datetime) -> NewsItem:
    return NewsItem(
        news_id="synthetic-news-1",
        source="synthetic-fixture",
        timestamp=now,
        headline="Synthetic positive evidence",
        summary="A deterministic non-investment test record",
        asset_relevance=("TEST",),
        sentiment=Decimal("0.50"),
        importance=Decimal("0.70"),
        confidence=Decimal("0.90"),
    )


@pytest.fixture
def context(
    now: datetime,
    portfolio: PortfolioSnapshot,
    price: PriceSnapshot,
    instrument: InstrumentMetadata,
) -> RiskContext:
    return RiskContext(
        evaluated_at=now,
        portfolio=portfolio,
        price=price,
        instrument=instrument,
        market_data_available=True,
        news_data_available=True,
        daily_new_trade_count=0,
        recent_idempotency_keys=frozenset(),
        api_state_consistent=True,
    )


@pytest.fixture
def kill_switch(now: datetime) -> KillSwitch:
    return KillSwitch(active=False, reason="test setup", clock=lambda: now)


@pytest.fixture
def audit_sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def risk_manager(
    now: datetime,
    kill_switch: KillSwitch,
    audit_sink: InMemoryAuditSink,
) -> RiskManager:
    return RiskManager(
        RiskPolicyConfig(),
        kill_switch,
        audit_sink=audit_sink,
        authorization_key=b"test-risk-authorization-key-32b!",
        clock=lambda: now,
    )
