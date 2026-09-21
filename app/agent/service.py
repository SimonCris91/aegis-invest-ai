"""Conservative deterministic and validated-AI Aegis Agent implementations."""

from collections.abc import Callable
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from app.agent.context import AegisAgentContext
from app.agent.exit_policy import (
    ExitPolicy,
    ExitPolicyDecision,
    ExitPolicyReasonCode,
    ExitPolicyV2Guarded,
)
from app.agent.models import AegisAgentResult, AegisAnalysis, AIAnalysisResponse
from app.agent.ports import AIAnalysisProvider
from app.agent.safety import build_sanitized_ai_payload
from app.domain.enums import (
    HoldingPeriod,
    MarketStatus,
    RecommendedAction,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.market import EvidenceItem, InstrumentMetadata, MarketQuote, NewsItem
from app.domain.proposals import TradeProposal
from app.intelligence.models import AegisDecision, AegisOpportunityAnalysis


class AgentAnalysisError(RuntimeError):
    """Raised when an agent cannot produce a safe validated result."""


class DeterministicAegisAgent:
    """Offline baseline that prefers HOLD and never consumes external text as instructions."""

    def __init__(
        self,
        exit_policy: ExitPolicy | ExitPolicyV2Guarded | None = None,
        *,
        allow_news_unavailable_demo: bool = False,
    ) -> None:
        self._exit_policy = exit_policy or ExitPolicy()
        self._allow_news_unavailable_demo = allow_news_unavailable_demo

    def analyze(self, context: AegisAgentContext) -> AegisAgentResult:
        if context.intelligence_reports:
            return self._analyze_intelligence(context)

        if not context.quotes or not context.instruments or (
            not context.news and not self._allow_news_unavailable_demo
        ):
            return self._hold(context, "insufficient normalized evidence")

        quote = self._selected_quote(context)
        instrument = self._instrument_for(quote, context.instruments)
        if instrument is None or not self._instrument_is_safe(instrument, quote):
            return self._hold(context, "instrument metadata is incomplete or ineligible")
        if not quote.is_fresh(
            as_of=context.analysis_timestamp,
            max_age_seconds=context.strategy.maximum_quote_age_seconds,
        ):
            return self._hold(context, "quote freshness is outside the allowed interval")
        if quote.market_status is not MarketStatus.OPEN:
            return self._hold(context, "market is not confirmed open")

        fresh_news = tuple(
            item
            for item in context.news
            if quote.symbol.casefold() in {symbol.casefold() for symbol in item.asset_relevance}
            and context.analysis_timestamp - item.timestamp
            <= timedelta(seconds=context.strategy.maximum_news_age_seconds)
        )
        supporting = tuple(
            item for item in fresh_news if item.sentiment >= context.strategy.minimum_news_sentiment
        )
        if (
            not self._allow_news_unavailable_demo
            and len(supporting) < context.strategy.minimum_supporting_news
        ):
            return self._hold(context, "evidence does not meet the conservative threshold")

        current_weight = context.portfolio.weight_for(instrument.instrument_id)
        if current_weight >= context.strategy.target_position_weight:
            return self._hold(context, "target exposure is already satisfied")
        if any(
            position.market_price < position.average_entry_price
            for position in context.portfolio.positions_for(instrument.instrument_id)
        ):
            return self._hold(context, "baseline agent does not average down")

        if context.minimum_trade_amount is None:
            amount = min(
                context.portfolio.total_value * context.strategy.proposed_trade_weight,
                context.portfolio.total_value
                * (context.strategy.target_position_weight - current_weight),
            )
        else:
            amount = context.minimum_trade_amount
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if amount <= 0:
            return self._hold(context, "calculated trade size is not positive")

        action = RecommendedAction.OPEN if current_weight == 0 else RecommendedAction.INCREASE
        analysis = AegisAnalysis(
            timestamp=context.analysis_timestamp,
            market_assessment="normalized market and news thresholds passed",
            portfolio_assessment="current exposure is below the configured target",
            opportunity_summary="a small fully funded long allocation is eligible for review",
            risk_summary=(
                "loss of capital remains possible; execution requires independent approval"
            ),
            confidence=context.strategy.baseline_confidence,
            supporting_factors=(
                tuple(item.headline for item in supporting)
                if supporting
                else ("explicit Demo override: news provider unavailable",)
            ),
            risk_factors=("market risk", "model risk"),
            recommended_action=action,
            rationale="deterministic conservative thresholds were satisfied",
            symbol=quote.symbol,
        )
        return AegisAgentResult(
            analysis=analysis,
            proposal=self._proposal(
                context=context,
                quote=quote,
                instrument=instrument,
                action=action,
                amount=amount,
                confidence=context.strategy.baseline_confidence,
                reason=analysis.rationale,
                horizon=HoldingPeriod.MONTHS,
                supporting_news=supporting,
            ),
        )

    @staticmethod
    def _hold(context: AegisAgentContext, rationale: str) -> AegisAgentResult:
        return AegisAgentResult(
            analysis=AegisAnalysis(
                timestamp=context.analysis_timestamp,
                market_assessment="no actionable market conclusion",
                portfolio_assessment="portfolio left unchanged",
                opportunity_summary="no proposal",
                risk_summary="insufficient evidence makes inaction the safe result",
                confidence=Decimal("0"),
                supporting_factors=(),
                risk_factors=(rationale,),
                recommended_action=RecommendedAction.HOLD,
                rationale=rationale,
            )
        )

    @staticmethod
    def _instrument_for(
        quote: MarketQuote, instruments: tuple[InstrumentMetadata, ...]
    ) -> InstrumentMetadata | None:
        return next(
            (
                item
                for item in instruments
                if item.instrument_id == quote.instrument_id
                and item.symbol.casefold() == quote.symbol.casefold()
            ),
            None,
        )

    @staticmethod
    def _selected_quote(context: AegisAgentContext) -> MarketQuote:
        allowed_candidate_symbols = [
            candidate.instrument.symbol.casefold()
            for candidate in sorted(
                context.candidates,
                key=lambda item: item.rank or 999_999,
            )
            if candidate.policy_allowed
        ]
        for symbol in allowed_candidate_symbols:
            quote = next(
                (item for item in context.quotes if item.symbol.casefold() == symbol),
                None,
            )
            if quote is not None:
                return quote
        return sorted(context.quotes, key=lambda item: item.symbol.casefold())[0]

    @staticmethod
    def _instrument_is_safe(instrument: InstrumentMetadata, quote: MarketQuote) -> bool:
        return (
            instrument.is_valid
            and instrument.is_tradable
            and instrument.allows_long
            and instrument.settlement_type is SettlementType.REAL
            and 1 in instrument.allowed_leverages
            and instrument.instrument_id == quote.instrument_id
        )

    @staticmethod
    def _proposal(
        *,
        context: AegisAgentContext,
        quote: MarketQuote,
        instrument: InstrumentMetadata,
        action: RecommendedAction,
        amount: Decimal,
        confidence: Decimal,
        reason: str,
        horizon: HoldingPeriod,
        supporting_news: tuple[NewsItem, ...],
    ) -> TradeProposal:
        intent = TradeIntent(action.value)
        side = (
            TradeSide.BUY
            if action in {RecommendedAction.OPEN, RecommendedAction.INCREASE}
            else TradeSide.SELL
        )
        identity = "|".join(
            (
                quote.symbol,
                action.value,
                str(amount),
                context.analysis_timestamp.isoformat(),
            )
        )
        proposal_id = uuid5(NAMESPACE_URL, f"aegis:{identity}")
        evidence = tuple(
            EvidenceItem(
                source=item.source,
                timestamp=item.timestamp,
                summary=item.summary,
                confidence=item.confidence,
            )
            for item in supporting_news
        )
        return TradeProposal(
            proposal_id=proposal_id,
            idempotency_key=f"aegis-{proposal_id.hex}",
            created_at=context.analysis_timestamp,
            instrument_id=instrument.instrument_id,
            symbol=quote.symbol,
            asset_class=instrument.asset_class,
            side=side,
            intent=intent,
            amount=amount,
            currency=context.portfolio.currency,
            target_weight=context.strategy.target_position_weight,
            current_weight=context.portfolio.weight_for(instrument.instrument_id),
            leverage=1,
            settlement_type=SettlementType.REAL,
            reason=reason,
            evidence=evidence,
            confidence=confidence,
            risk_factors=("market risk", "model risk"),
            invalidation_conditions=("normalized supporting evidence no longer holds",),
            expected_holding_period=horizon,
        )

    def _analyze_intelligence(self, context: AegisAgentContext) -> AegisAgentResult:
        analysis_report = max(
            context.intelligence_reports,
            key=lambda item: (
                item.opportunity_score.overall_score,
                item.opportunity_score.confidence,
            ),
        )
        quote = analysis_report.candidate.quote
        if quote is None:
            return self._hold(context, "opportunity intelligence lacks a reference quote")
        instrument = self._instrument_for(quote, context.instruments)
        if instrument is None or not self._instrument_is_safe(instrument, quote):
            return self._hold(context, "opportunity instrument failed deterministic validation")

        position_state = next(
            (
                state
                for state in context.position_states
                if state.instrument_id == instrument.instrument_id
            ),
            None,
        )
        if (
            isinstance(self._exit_policy, ExitPolicyV2Guarded)
            and position_state is not None
            and position_state.cooldown_bars_remaining > 0
        ):
            return self._hold(
                context,
                "cooldown is active after a previous autonomous exit action",
            )
        exit_decision = self._exit_policy.evaluate(
            analysis=analysis_report,
            portfolio=context.portfolio,
            position_state=position_state,
        )
        if exit_decision.is_exit_action:
            if exit_decision.amount <= 0:
                return self._hold(context, "exit policy produced a non-positive amount")
            return self._exit_result(
                context=context,
                quote=quote,
                instrument=instrument,
                report=analysis_report,
                exit_decision=exit_decision,
            )
        current_weight = context.portfolio.weight_for(instrument.instrument_id)
        if current_weight > 0 and exit_decision.reason_code in {
            ExitPolicyReasonCode.COOLDOWN_BLOCKS_REENTRY.value,
            ExitPolicyReasonCode.PARAMETER_BUNDLE_REQUIRED.value,
        }:
            return self._hold(context, exit_decision.reason)

        if analysis_report.decision is not AegisDecision.BUY:
            return self._hold(
                context,
                (
                    f"opportunity intelligence returned {analysis_report.decision.value}; "
                    f"{exit_decision.reason}"
                ),
            )
        minimum = (
            context.minimum_trade_amount or analysis_report.candidate.instrument.minimum_order_value
        )
        if current_weight >= context.strategy.target_position_weight:
            return self._hold(context, "target exposure is already satisfied")
        target_remaining = (
            context.portfolio.total_value
            * (context.strategy.target_position_weight - current_weight)
        ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        policy_amount = (
            context.portfolio.total_value * context.strategy.proposed_trade_weight
        ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        amount = min(policy_amount, target_remaining)
        if minimum is not None:
            amount = max(minimum, amount)
        if amount <= 0:
            return self._hold(context, "calculated intelligence trade size is not positive")
        action = RecommendedAction.INCREASE if current_weight > 0 else RecommendedAction.OPEN
        analysis = AegisAnalysis(
            timestamp=context.analysis_timestamp,
            market_assessment=(
                f"{analysis_report.regime.trend.value} / {analysis_report.regime.volatility.value}"
            ),
            portfolio_assessment=analysis_report.portfolio_fit.status.value,
            opportunity_summary=(
                f"Aegis Opportunity Score {analysis_report.opportunity_score.overall_score} "
                f"({analysis_report.opportunity_score.band.value})"
            ),
            risk_summary="proposal still requires independent Risk Manager approval",
            confidence=analysis_report.opportunity_score.confidence,
            supporting_factors=analysis_report.ensemble.supporting_factors,
            risk_factors=analysis_report.ensemble.risk_factors,
            recommended_action=action,
            rationale="deterministic opportunity intelligence passed agent thresholds",
            symbol=quote.symbol,
        )
        return AegisAgentResult(
            analysis=analysis,
            proposal=self._proposal_from_intelligence(
                context=context,
                quote=quote,
                instrument=instrument,
                amount=amount,
                confidence=analysis_report.opportunity_score.confidence,
                reason=analysis.rationale,
                report=analysis_report,
                action=action,
            ),
        )

    def _exit_result(
        self,
        *,
        context: AegisAgentContext,
        quote: MarketQuote,
        instrument: InstrumentMetadata,
        report: AegisOpportunityAnalysis,
        exit_decision: ExitPolicyDecision,
    ) -> AegisAgentResult:
        analysis = AegisAnalysis(
            timestamp=context.analysis_timestamp,
            market_assessment=f"{report.regime.trend.value} / {report.regime.volatility.value}",
            portfolio_assessment=report.portfolio_fit.status.value,
            opportunity_summary=(
                f"Aegis Opportunity Score {report.opportunity_score.overall_score} "
                f"({report.opportunity_score.band.value})"
            ),
            risk_summary="exit proposal still requires independent Risk Manager approval",
            confidence=report.opportunity_score.confidence,
            supporting_factors=report.ensemble.supporting_factors,
            risk_factors=tuple(
                item
                for item in (
                    *report.ensemble.risk_factors,
                    exit_decision.reason_code,
                    exit_decision.reason,
                )
                if item
            ),
            recommended_action=exit_decision.action,
            rationale=exit_decision.reason,
            symbol=quote.symbol,
        )
        return AegisAgentResult(
            analysis=analysis,
            proposal=self._proposal_from_intelligence(
                context=context,
                quote=quote,
                instrument=instrument,
                amount=exit_decision.amount,
                confidence=report.opportunity_score.confidence,
                reason=exit_decision.reason,
                report=report,
                action=exit_decision.action,
            ),
        )

    @staticmethod
    def _proposal_from_intelligence(
        *,
        context: AegisAgentContext,
        quote: MarketQuote,
        instrument: InstrumentMetadata,
        amount: Decimal,
        confidence: Decimal,
        reason: str,
        report: AegisOpportunityAnalysis,
        action: RecommendedAction = RecommendedAction.OPEN,
    ) -> TradeProposal:
        proposal_id = uuid5(
            NAMESPACE_URL,
            (
                f"aegis:intelligence:{report.data_digest}:"
                f"{action.value}:{context.analysis_timestamp.isoformat()}"
            ),
        )
        evidence = (
            EvidenceItem(
                source="aegis-opportunity-intelligence",
                timestamp=context.analysis_timestamp,
                summary=(
                    f"score={report.opportunity_score.overall_score}; "
                    f"band={report.opportunity_score.band.value}; "
                    f"regime={report.regime.trend.value}; action={action.value}"
                ),
                confidence=confidence,
            ),
        )
        side = (
            TradeSide.BUY
            if action in {RecommendedAction.OPEN, RecommendedAction.INCREASE}
            else TradeSide.SELL
        )
        intent = TradeIntent(action.value)
        return TradeProposal(
            proposal_id=proposal_id,
            idempotency_key=f"aegis-{proposal_id.hex}",
            created_at=context.analysis_timestamp,
            instrument_id=instrument.instrument_id,
            symbol=quote.symbol,
            asset_class=instrument.asset_class,
            side=side,
            intent=intent,
            amount=amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN),
            currency=context.portfolio.currency,
            target_weight=(
                context.strategy.target_position_weight
                if action in {RecommendedAction.OPEN, RecommendedAction.INCREASE}
                else Decimal("0")
            ),
            current_weight=context.portfolio.weight_for(instrument.instrument_id),
            leverage=1,
            settlement_type=SettlementType.REAL,
            reason=reason,
            evidence=evidence,
            confidence=confidence,
            confidence_model_version=report.opportunity_score.confidence_model_version,
            confidence_semantics_version=report.opportunity_score.confidence_semantics_version,
            confidence_threshold=report.opportunity_score.confidence_threshold,
            confidence_threshold_provenance=(
                report.opportunity_score.confidence_threshold_provenance
            ),
            risk_factors=report.ensemble.risk_factors or ("market risk",),
            invalidation_conditions=("opportunity intelligence data digest changes",),
            expected_holding_period=HoldingPeriod.MONTHS,
        )


class AIBackedAegisAgent(DeterministicAegisAgent):
    """Optional adapter that validates provider data before proposal construction."""

    def __init__(
        self,
        provider: AIAnalysisProvider,
        *,
        response_validator: Callable[[object], AIAnalysisResponse] | None = None,
    ) -> None:
        self._provider = provider
        self._response_validator = response_validator or AIAnalysisResponse.model_validate

    def analyze(self, context: AegisAgentContext) -> AegisAgentResult:
        try:
            raw = self._provider.analyze(build_sanitized_ai_payload(context))
            response = self._response_validator(raw)
        except (ValidationError, TypeError, ValueError, RuntimeError) as exc:
            raise AgentAnalysisError("external AI output is invalid") from exc

        if response.action is RecommendedAction.HOLD:
            return self._hold(context, response.rationale)

        response_symbol = response.symbol
        if response_symbol is None:
            raise AgentAnalysisError("AI output is missing an active symbol")
        quote = next(
            (
                item
                for item in context.quotes
                if item.symbol.casefold() == response_symbol.casefold()
            ),
            None,
        )
        instrument = self._instrument_for(quote, context.instruments) if quote is not None else None
        if quote is None or instrument is None or not self._instrument_is_safe(instrument, quote):
            raise AgentAnalysisError("AI output references an unknown or unsupported instrument")

        amount = (context.portfolio.total_value * response.allocation_weight).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        analysis = AegisAnalysis(
            timestamp=context.analysis_timestamp,
            market_assessment="validated structured AI assessment",
            portfolio_assessment="validated against normalized portfolio context",
            opportunity_summary=response.rationale,
            risk_summary="external AI output remains untrusted until independent risk approval",
            confidence=response.confidence,
            supporting_factors=response.supporting_factors,
            risk_factors=response.risk_factors,
            recommended_action=response.action,
            rationale=response.rationale,
            symbol=response_symbol,
        )
        return AegisAgentResult(
            analysis=analysis,
            proposal=self._proposal(
                context=context,
                quote=quote,
                instrument=instrument,
                action=response.action,
                amount=amount,
                confidence=response.confidence,
                reason=response.rationale,
                horizon=response.horizon,
                supporting_news=context.news,
            ),
        )
