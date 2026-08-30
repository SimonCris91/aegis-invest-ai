from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from app.agent.context import AegisAgentContext
from app.agent.models import AegisAgentResult
from app.agent.ports import AegisAgent
from app.agent.service import AIBackedAegisAgent, DeterministicAegisAgent
from app.config.models import AegisStrategyConfig
from app.domain.enums import AegisRunStatus, AuditEventType, Currency, RiskDecisionStatus
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem
from app.execution.gate import AuthorizedTrade, RiskEnforcedExecutionGate
from app.market_data.providers import FakeMarketDataProvider
from app.news.providers import FakeNewsProvider
from app.orchestration.service import AegisInvestmentService
from app.paper_trading.engine import PaperTradingEngine, PaperTradingError
from app.paper_trading.models import PaperExecutionResult, PaperPortfolioState
from app.reporting.audit import InMemoryAuditSink
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager


class FailingAgent:
    def analyze(self, context: AegisAgentContext) -> AegisAgentResult:
        del context
        raise RuntimeError("synthetic agent failure")


class LowConfidenceAgent:
    def analyze(self, context: AegisAgentContext) -> AegisAgentResult:
        result = DeterministicAegisAgent().analyze(context)
        assert result.proposal is not None
        return result.model_copy(
            update={
                "analysis": result.analysis.model_copy(update={"confidence": Decimal("0.40")}),
                "proposal": result.proposal.model_copy(update={"confidence": Decimal("0.40")}),
            }
        )


class BrokenAIProvider:
    def analyze(self, normalized_context: Mapping[str, object]) -> Mapping[str, object]:
        del normalized_context
        return {"action": "unsupported"}


class FailingPaperEngine(PaperTradingEngine):
    def execute(
        self,
        admitted: AuthorizedTrade,
        quote: MarketQuote,
        *,
        at: datetime,
    ) -> PaperExecutionResult:
        del admitted, quote, at
        raise PaperTradingError("synthetic ledger failure")


def initial_state(now: datetime) -> PaperPortfolioState:
    return PaperPortfolioState(
        as_of=now,
        currency=Currency.USD,
        initial_cash=Decimal("200"),
        cash=Decimal("200"),
        peak_value=Decimal("200"),
    )


def build_service(
    *,
    now: datetime,
    quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    audit_sink: InMemoryAuditSink,
    agent: AegisAgent | None = None,
    market_unavailable: bool = False,
    news_unavailable: bool = False,
    failing_engine: bool = False,
) -> tuple[AegisInvestmentService, PaperTradingEngine]:
    market = FakeMarketDataProvider(
        quotes={"TEST": quote},
        instruments={"TEST": instrument},
        unavailable=market_unavailable,
    )
    news = FakeNewsProvider(items=(news_item,), unavailable=news_unavailable)
    gate = RiskEnforcedExecutionGate(risk_manager)
    engine_type = FailingPaperEngine if failing_engine else PaperTradingEngine
    engine = engine_type(initial_state=initial_state(now), admission_gate=gate)
    selected_agent = agent if agent is not None else DeterministicAegisAgent()
    service = AegisInvestmentService(
        market_data=market,
        news=news,
        agent=selected_agent,
        risk_manager=risk_manager,
        admission_gate=gate,
        paper_engine=engine,
        strategy=AegisStrategyConfig(),
        clock=lambda: now,
        audit_sink=audit_sink,
    )
    return service, engine


def test_full_pipeline_successful_hold(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    audit_sink: InMemoryAuditSink,
) -> None:
    negative = news_item.model_copy(update={"sentiment": Decimal("-0.5")})
    service, engine = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=negative,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
    )

    result = service.run("TEST")

    assert result.status is AegisRunStatus.HOLD
    assert result.trade_proposal is None
    assert result.paper_execution is None
    assert engine.state.trade_history == ()


def test_full_pipeline_executes_only_approved_paper_trade(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    audit_sink: InMemoryAuditSink,
) -> None:
    service, engine = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
    )

    result = service.run("TEST")

    assert result.status is AegisRunStatus.EXECUTED_PAPER
    assert result.risk_decision is not None
    assert result.risk_decision.status is RiskDecisionStatus.APPROVED
    assert result.paper_execution is not None
    assert engine.state.cash == Decimal("190.00")
    assert AuditEventType.PAPER_EXECUTION_COMPLETED in {
        event.event_type for event in audit_sink.events
    }


def test_kill_switch_causes_risk_rejection_and_no_execution(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    kill_switch: KillSwitch,
    audit_sink: InMemoryAuditSink,
) -> None:
    kill_switch.activate("synthetic halt")
    service, engine = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
    )

    result = service.run("TEST")

    assert result.status is AegisRunStatus.REJECTED
    assert result.paper_execution is None
    assert engine.state.trade_history == ()


def test_low_confidence_agent_proposal_is_rejected_by_risk(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    audit_sink: InMemoryAuditSink,
) -> None:
    service, engine = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
        agent=LowConfidenceAgent(),
    )

    result = service.run("TEST")

    assert result.status is AegisRunStatus.REJECTED
    assert engine.state.trade_history == ()


def test_market_and_news_failures_stop_before_agent_or_risk(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    audit_sink: InMemoryAuditSink,
) -> None:
    market_service, _ = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
        market_unavailable=True,
    )
    news_service, _ = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
        news_unavailable=True,
    )

    assert market_service.run("TEST").status is AegisRunStatus.FAILED_SAFE
    assert news_service.run("TEST").status is AegisRunStatus.FAILED_SAFE


def test_agent_and_ai_failures_stop_safely(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    audit_sink: InMemoryAuditSink,
) -> None:
    failing_service, _ = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
        agent=FailingAgent(),
    )
    ai_service, _ = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
        agent=AIBackedAegisAgent(BrokenAIProvider()),
    )

    assert failing_service.run("TEST").status is AegisRunStatus.FAILED_SAFE
    assert ai_service.run("TEST").status is AegisRunStatus.FAILED_SAFE


def test_paper_engine_failure_does_not_mutate_ledger(
    now: datetime,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    audit_sink: InMemoryAuditSink,
) -> None:
    service, engine = build_service(
        now=now,
        quote=market_quote,
        instrument=instrument,
        news_item=news_item,
        risk_manager=risk_manager,
        audit_sink=audit_sink,
        failing_engine=True,
    )

    result = service.run("TEST")

    assert result.status is AegisRunStatus.FAILED_SAFE
    assert engine.state.trade_history == ()
