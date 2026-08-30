"""Safety-first orchestration ending exclusively in the local paper engine."""

from collections.abc import Callable
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.agent.context import AegisAgentContext
from app.agent.ports import AegisAgent
from app.agent.service import AgentAnalysisError
from app.config.models import AegisStrategyConfig
from app.domain.audit import AuditEvent
from app.domain.enums import (
    AegisRunStatus,
    AuditEventType,
    RiskDecisionStatus,
)
from app.domain.market import MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext, RiskDecision
from app.execution.gate import RiskEnforcedExecutionGate
from app.market_data.ports import MarketDataProvider
from app.market_data.providers import MarketDataError
from app.news.ports import NewsProvider
from app.news.providers import NewsDataError
from app.orchestration.models import AegisRunResult
from app.paper_trading.engine import PaperTradingEngine, PaperTradingError
from app.reporting.audit import AuditSink, NullAuditSink
from app.risk.manager import RiskManager


class AegisInvestmentService:
    """Coordinates data, agent, risk, admission, and paper simulation in order."""

    def __init__(
        self,
        *,
        market_data: MarketDataProvider,
        news: NewsProvider,
        agent: AegisAgent,
        risk_manager: RiskManager,
        admission_gate: RiskEnforcedExecutionGate,
        paper_engine: PaperTradingEngine,
        strategy: AegisStrategyConfig,
        clock: Callable[[], datetime],
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._market_data = market_data
        self._news = news
        self._agent = agent
        self._risk_manager = risk_manager
        self._admission_gate = admission_gate
        self._paper_engine = paper_engine
        self._strategy = strategy
        self._clock = clock
        self._audit_sink = audit_sink or NullAuditSink()

    def run(self, symbol: str) -> AegisRunResult:
        at = self._clock()
        run_id = uuid5(NAMESPACE_URL, f"aegis-run:{symbol.casefold()}:{at.isoformat()}")
        portfolio = self._paper_engine.portfolio_snapshot(as_of=at)
        self._audit(AuditEventType.AEGIS_RUN_STARTED, run_id, at, "started")
        self._audit(
            AuditEventType.PORTFOLIO_READ,
            run_id,
            at,
            "paper-portfolio-read",
            portfolio=portfolio,
        )

        try:
            quote = self._market_data.get_quote(
                symbol, as_of=at, expected_currency=portfolio.currency
            )
            instrument = self._market_data.resolve_instrument(symbol)
        except MarketDataError as exc:
            return self._failed(
                run_id,
                at,
                portfolio,
                AuditEventType.PROVIDER_FAILURE,
                "market-data failure",
                exc,
            )
        portfolio = self._paper_engine.portfolio_snapshot(as_of=at, quotes=(quote,))
        self._audit(AuditEventType.MARKET_DATA_READ, run_id, at, "market-data-read")

        try:
            news = self._news.get_news((symbol,), as_of=at)
        except NewsDataError as exc:
            return self._failed(
                run_id,
                at,
                portfolio,
                AuditEventType.PROVIDER_FAILURE,
                "news failure",
                exc,
                quotes=(quote,),
            )
        self._audit(AuditEventType.NEWS_READ, run_id, at, "news-read")

        context = AegisAgentContext(
            portfolio=portfolio,
            quotes=(quote,),
            news=news,
            instruments=(instrument,),
            analysis_timestamp=at,
            strategy=self._strategy,
        )
        self._audit(AuditEventType.AGENT_ANALYSIS_STARTED, run_id, at, "agent-started")
        try:
            agent_result = self._agent.analyze(context)
        except (AgentAnalysisError, ValueError, RuntimeError) as exc:
            return self._failed(
                run_id,
                at,
                portfolio,
                AuditEventType.AGENT_OUTPUT_INVALID,
                "agent failure",
                exc,
                quotes=(quote,),
                news=news,
            )
        self._audit(AuditEventType.AGENT_ANALYSIS_COMPLETED, run_id, at, "agent-completed")

        proposal = agent_result.proposal
        if proposal is None:
            self._audit(AuditEventType.AEGIS_RUN_COMPLETED, run_id, at, "hold")
            return AegisRunResult(
                timestamp=at,
                status=AegisRunStatus.HOLD,
                portfolio=portfolio,
                market_quotes=(quote,),
                news=news,
                agent_analysis=agent_result.analysis,
                data_sources=self._sources(quote, news),
            )

        self._audit(
            AuditEventType.PROPOSAL_CREATED,
            run_id,
            at,
            "proposal-created",
            portfolio=portfolio,
            proposal=proposal,
        )
        risk_context = RiskContext(
            evaluated_at=at,
            portfolio=portfolio,
            price=quote.to_price_snapshot(),
            instrument=instrument,
            market_data_available=True,
            news_data_available=True,
            daily_new_trade_count=self._paper_engine.daily_new_trade_count(at),
            recent_idempotency_keys=self._paper_engine.recent_idempotency_keys,
            api_state_consistent=True,
        )
        evaluation = self._risk_manager.evaluate(proposal, risk_context)
        risk_event = (
            AuditEventType.RISK_APPROVED
            if evaluation.decision.status is RiskDecisionStatus.APPROVED
            else AuditEventType.RISK_REJECTED
        )
        self._audit(
            risk_event,
            run_id,
            at,
            "risk-approved" if evaluation.authorization else "risk-rejected",
            portfolio=portfolio,
            proposal=proposal,
            decision=evaluation.decision,
        )
        if evaluation.authorization is None:
            self._audit(
                AuditEventType.PROPOSAL_REJECTED,
                run_id,
                at,
                "proposal-rejected",
                proposal=proposal,
                decision=evaluation.decision,
            )
            self._audit(AuditEventType.AEGIS_RUN_COMPLETED, run_id, at, "risk-rejected")
            return AegisRunResult(
                timestamp=at,
                status=AegisRunStatus.REJECTED,
                portfolio=portfolio,
                market_quotes=(quote,),
                news=news,
                agent_analysis=agent_result.analysis,
                trade_proposal=proposal,
                risk_decision=evaluation.decision,
                warnings=("independent Risk Manager rejected the proposal",),
                data_sources=self._sources(quote, news),
            )

        try:
            admitted = self._admission_gate.admit(proposal, evaluation.authorization, at=at)
            self._audit(
                AuditEventType.PAPER_EXECUTION_ADMITTED,
                run_id,
                at,
                "paper-admitted",
                proposal=proposal,
                decision=evaluation.decision,
            )
            paper_execution = self._paper_engine.execute(admitted, quote, at=at)
        except (PermissionError, PaperTradingError) as exc:
            self._audit(
                AuditEventType.PAPER_EXECUTION_REJECTED,
                run_id,
                at,
                "paper-rejected",
                proposal=proposal,
                decision=evaluation.decision,
            )
            self._audit(AuditEventType.AEGIS_RUN_COMPLETED, run_id, at, "failed-safe")
            return self._result(
                at=at,
                status=AegisRunStatus.FAILED_SAFE,
                portfolio=portfolio,
                quote=quote,
                news=news,
                analysis=agent_result.analysis,
                proposal=proposal,
                risk_decision=evaluation.decision,
                warning=f"paper execution failed safely: {type(exc).__name__}",
            )

        self._audit(
            AuditEventType.PAPER_EXECUTION_COMPLETED,
            run_id,
            at,
            "paper-completed",
            portfolio=paper_execution.portfolio.to_portfolio_snapshot(),
            proposal=proposal,
            decision=evaluation.decision,
        )
        self._audit(AuditEventType.AEGIS_RUN_COMPLETED, run_id, at, "paper-run-completed")
        return AegisRunResult(
            timestamp=at,
            status=AegisRunStatus.EXECUTED_PAPER,
            portfolio=paper_execution.portfolio.to_portfolio_snapshot(),
            market_quotes=(quote,),
            news=news,
            agent_analysis=agent_result.analysis,
            trade_proposal=proposal,
            risk_decision=evaluation.decision,
            paper_execution=paper_execution,
            data_sources=self._sources(quote, news),
        )

    def _failed(
        self,
        run_id: UUID,
        at: datetime,
        portfolio: PortfolioSnapshot,
        event_type: AuditEventType,
        message: str,
        error: Exception,
        *,
        quotes: tuple[MarketQuote, ...] = (),
        news: tuple[NewsItem, ...] = (),
    ) -> AegisRunResult:
        self._audit(event_type, run_id, at, message)
        self._audit(AuditEventType.AEGIS_RUN_COMPLETED, run_id, at, "failed-safe")
        return AegisRunResult(
            timestamp=at,
            status=AegisRunStatus.FAILED_SAFE,
            portfolio=portfolio,
            market_quotes=quotes,
            news=news,
            warnings=(f"{message}: {type(error).__name__}",),
            data_sources=self._sources(quotes[0], news) if quotes else (),
        )

    @staticmethod
    def _result(
        *,
        at: datetime,
        status: AegisRunStatus,
        portfolio: PortfolioSnapshot,
        quote: MarketQuote,
        news: tuple[NewsItem, ...],
        analysis: object,
        proposal: TradeProposal,
        risk_decision: RiskDecision,
        warning: str,
    ) -> AegisRunResult:
        from app.agent.models import AegisAnalysis

        if not isinstance(analysis, AegisAnalysis):
            raise TypeError("analysis must be an AegisAnalysis")
        return AegisRunResult(
            timestamp=at,
            status=status,
            portfolio=portfolio,
            market_quotes=(quote,),
            news=news,
            agent_analysis=analysis,
            trade_proposal=proposal,
            risk_decision=risk_decision,
            warnings=(warning,),
            data_sources=AegisInvestmentService._sources(quote, news),
        )

    @staticmethod
    def _sources(quote: MarketQuote, news: tuple[NewsItem, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys((quote.source, *(item.source for item in news))))

    def _audit(
        self,
        event_type: AuditEventType,
        run_id: UUID,
        at: datetime,
        result: str,
        *,
        portfolio: PortfolioSnapshot | None = None,
        proposal: TradeProposal | None = None,
        decision: RiskDecision | None = None,
    ) -> None:
        event_id = uuid5(NAMESPACE_URL, f"{run_id}:{event_type.value}:{result}")
        self._audit_sink.record(
            AuditEvent(
                event_id=event_id,
                event_type=event_type,
                timestamp=at,
                correlation_id=run_id,
                proposal_id=proposal.proposal_id if proposal is not None else None,
                portfolio_value=portfolio.total_value if portfolio is not None else None,
                decision=decision.status if decision is not None else None,
                confidence=proposal.confidence if proposal is not None else None,
                risk_violations=(
                    tuple(violation.code for violation in decision.violations)
                    if decision is not None
                    else ()
                ),
                result=result,
            )
        )
