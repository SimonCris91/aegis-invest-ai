"""Deterministic Risk Manager and signed execution authorization gate."""

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from app.config.models import RiskPolicyConfig
from app.domain.audit import AuditEvent
from app.domain.enums import (
    AssetClass,
    AuditEventType,
    RiskDecisionStatus,
    RiskViolationCode,
    SettlementType,
    TradeIntent,
    TradeSide,
)
from app.domain.proposals import TradeProposal
from app.domain.risk import (
    RiskAuthorization,
    RiskContext,
    RiskDecision,
    RiskEvaluation,
    RiskViolation,
)
from app.intelligence.confidence import (
    CONFIDENCE_MODEL_V1,
    CONFIDENCE_MODEL_V2_B,
    CONFIDENCE_SEMANTICS_V1,
    CONFIDENCE_SEMANTICS_V2,
    V2_B_THRESHOLD,
    V2_B_THRESHOLD_PROVENANCE,
)
from app.policies.defaults import default_asset_policy_engine
from app.policies.engine import AssetPolicyEngine
from app.reporting.audit import AuditSink, NullAuditSink
from app.risk.kill_switch import KillSwitch


class RiskAuthorizationError(PermissionError):
    """Raised when an execution capability is forged, changed, or expired."""


class RiskManager:
    """The only component allowed to issue valid execution capabilities."""

    def __init__(
        self,
        policy: RiskPolicyConfig,
        kill_switch: KillSwitch,
        *,
        audit_sink: AuditSink | None = None,
        authorization_key: bytes | None = None,
        asset_policy_engine: AssetPolicyEngine | None = None,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._policy = policy
        self._kill_switch = kill_switch
        self._audit_sink = audit_sink or NullAuditSink()
        self._authorization_key = authorization_key or secrets.token_bytes(32)
        if len(self._authorization_key) < 32:
            raise ValueError("authorization_key must contain at least 32 bytes")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._asset_policy_engine = asset_policy_engine or default_asset_policy_engine()
        self._policy_digest = self._digest_json(
            {
                "risk": policy.model_dump(mode="json"),
                "asset_policy_version": self._asset_policy_engine.policy_version,
                "asset_policies": self._asset_policy_engine.policy_snapshot(),
            }
        )

    @property
    def policy_digest(self) -> str:
        return self._policy_digest

    def proposal_digest(self, proposal: TradeProposal) -> str:
        return self._proposal_digest(proposal)

    def evaluate(
        self,
        proposal: TradeProposal,
        context: RiskContext,
        *,
        issue_authorization: bool = True,
        ignore_kill_switch: bool = False,
    ) -> RiskEvaluation:
        if ignore_kill_switch and issue_authorization:
            raise ValueError(
                "kill-switch bypass is permitted only for non-execution preliminary evaluation"
            )
        violations: list[RiskViolation] = []
        seen_codes: set[RiskViolationCode] = set()

        def reject(code: RiskViolationCode, message: str) -> None:
            if code not in seen_codes:
                seen_codes.add(code)
                violations.append(RiskViolation(code=code, message=message))

        is_position_increase = proposal.intent in {TradeIntent.OPEN, TradeIntent.INCREASE}
        is_position_reduction = proposal.intent in {TradeIntent.REDUCE, TradeIntent.CLOSE}
        portfolio = context.portfolio
        total_value = portfolio.total_value
        current_value = portfolio.market_value_for(proposal.instrument_id)
        instrument_asset_class = proposal.asset_class
        if context.instrument is not None:
            instrument_asset_class = context.instrument.asset_class
        asset_policy = self._asset_policy_engine.policy_for(instrument_asset_class)
        policy_known = instrument_asset_class is not AssetClass.UNKNOWN
        minimum_confidence = (
            max(self._policy.minimum_confidence, asset_policy.minimum_confidence)
            if policy_known
            else self._policy.minimum_confidence
        )
        max_trade_size = (
            min(self._policy.max_trade_size, asset_policy.max_new_trade_exposure)
            if policy_known
            else self._policy.max_trade_size
        )
        max_single_position = (
            min(self._policy.max_single_position, asset_policy.max_position_exposure)
            if policy_known
            else self._policy.max_single_position
        )
        min_cash_reserve = (
            max(self._policy.min_cash_reserve, asset_policy.minimum_cash_reserve)
            if policy_known
            else self._policy.min_cash_reserve
        )
        max_daily_new_trades = (
            min(self._policy.max_daily_new_trades, asset_policy.max_daily_new_trades)
            if policy_known
            else self._policy.max_daily_new_trades
        )

        if context.critical_operational_error:
            self._kill_switch.activate(f"critical error: {context.critical_operational_error}")
            reject(
                RiskViolationCode.CRITICAL_OPERATIONAL_ERROR,
                "a critical operational error halted trading",
            )

        if self._kill_switch.state.active and not ignore_kill_switch:
            reject(
                RiskViolationCode.KILL_SWITCH_ACTIVE,
                "the global kill switch blocks every new order",
            )

        if policy_known and not asset_policy.enabled:
            reject(
                RiskViolationCode.INVALID_INSTRUMENT_METADATA,
                "asset policy is disabled for this instrument class",
            )

        requested_leverage = Decimal(proposal.leverage)
        if requested_leverage != Decimal("1") and (
            not policy_known
            or not asset_policy.leverage_allowed
            or requested_leverage > asset_policy.max_leverage
        ):
            reject(
                RiskViolationCode.LEVERAGE_NOT_ALLOWED,
                "leverage exceeds the deterministic asset policy",
            )

        if proposal.settlement_type is SettlementType.CFD:
            if not asset_policy.enabled:
                reject(RiskViolationCode.CFD_NOT_ALLOWED, "CFD policy is disabled")
        elif proposal.settlement_type is not SettlementType.REAL and not asset_policy.enabled:
            reject(
                RiskViolationCode.DERIVATIVE_NOT_ALLOWED,
                "derivative policy is disabled for this asset class",
            )

        opens_short = is_position_increase and proposal.side is TradeSide.SELL
        if opens_short and (not policy_known or not asset_policy.short_allowed):
            reject(
                RiskViolationCode.SHORT_SELLING_NOT_ALLOWED,
                "new positions and increases must be long buys",
            )
        if is_position_reduction and proposal.side is not TradeSide.SELL:
            reject(
                RiskViolationCode.INVALID_TRADE_DIRECTION,
                "reductions and closes must use the sell side",
            )
        if (
            proposal.side is TradeSide.SELL
            and proposal.amount > current_value
            and (not policy_known or not asset_policy.short_allowed)
        ):
            reject(
                RiskViolationCode.SHORT_SELLING_NOT_ALLOWED,
                "a sale cannot exceed the current long position value",
            )

        if proposal.is_martingale:
            reject(RiskViolationCode.MARTINGALE_NOT_ALLOWED, "martingale sizing is forbidden")

        actual_averaging_down = is_position_increase and any(
            position.market_price < position.average_entry_price
            for position in portfolio.positions_for(proposal.instrument_id)
        )
        if proposal.is_averaging_down or actual_averaging_down:
            is_blind = (
                not proposal.thesis_revalidated
                or not proposal.evidence
                or not proposal.invalidation_conditions
            )
            if is_blind:
                reject(
                    RiskViolationCode.BLIND_AVERAGING_DOWN,
                    "averaging down requires revalidated evidence and invalidation conditions",
                )

        if not context.market_data_available or context.price is None:
            reject(
                RiskViolationCode.MARKET_DATA_UNAVAILABLE,
                "required market data is unavailable",
            )
        elif (
            context.price.as_of > context.evaluated_at
            or context.evaluated_at - context.price.as_of
            > timedelta(seconds=self._policy.max_price_age_seconds)
        ):
            reject(RiskViolationCode.STALE_PRICE, "the market price is stale")

        if not context.news_data_available:
            reject(RiskViolationCode.NEWS_DATA_UNAVAILABLE, "required news data is unavailable")

        instrument = context.instrument
        instrument_is_invalid = (
            instrument is None
            or not instrument.is_valid
            or not instrument.is_tradable
            or instrument.instrument_id != proposal.instrument_id
            or instrument.symbol.casefold() != proposal.symbol.casefold()
            or (
                proposal.asset_class is not instrument.asset_class
                and proposal.asset_class.value != "UNKNOWN"
            )
            or instrument.settlement_type is not proposal.settlement_type
            or (proposal.side is TradeSide.BUY and not instrument.allows_long)
            or (opens_short and not instrument.allows_short)
            or proposal.leverage not in instrument.allowed_leverages
        )
        if instrument_is_invalid:
            reject(
                RiskViolationCode.INVALID_INSTRUMENT_METADATA,
                "instrument metadata is missing, inconsistent, or ineligible",
            )

        if context.price is not None and (
            context.price.instrument_id != proposal.instrument_id
            or context.price.symbol.casefold() != proposal.symbol.casefold()
        ):
            reject(
                RiskViolationCode.INVALID_INSTRUMENT_METADATA,
                "price metadata does not match the proposed instrument",
            )

        if total_value <= 0 or not portfolio.is_consistent(self._policy.portfolio_value_tolerance):
            reject(
                RiskViolationCode.INCONSISTENT_PORTFOLIO_STATE,
                "portfolio totals are invalid or inconsistent with the reported API state",
            )

        if not context.api_state_consistent:
            reject(
                RiskViolationCode.INCONSISTENT_API_STATE,
                "the upstream API state is inconsistent",
            )

        if proposal.idempotency_key in context.recent_idempotency_keys:
            reject(RiskViolationCode.DUPLICATE_ORDER, "the proposal idempotency key is duplicated")

        confidence_threshold = self._confidence_threshold_for(
            proposal=proposal,
            legacy_minimum_confidence=minimum_confidence,
            reject=reject,
        )
        if confidence_threshold is not None and proposal.confidence < confidence_threshold:
            reject(
                RiskViolationCode.CONFIDENCE_BELOW_MINIMUM,
                "proposal confidence is below the configured minimum",
            )

        if proposal.currency is not portfolio.currency:
            reject(
                RiskViolationCode.CURRENCY_MISMATCH,
                "proposal and portfolio currencies must match before risk calculation",
            )

        trade_weight = Decimal("0")
        post_position_weight = Decimal("0")
        post_cash_weight = Decimal("0")
        if is_position_increase and total_value > 0:
            trade_weight = proposal.amount / total_value
            post_position_weight = (current_value + proposal.amount) / total_value
            post_cash = portfolio.cash - proposal.amount
            post_cash_weight = post_cash / total_value

            if proposal.amount >= total_value or proposal.amount >= portfolio.cash:
                reject(RiskViolationCode.ALL_IN_NOT_ALLOWED, "all-in trades are forbidden")
            if trade_weight > max_trade_size:
                reject(
                    RiskViolationCode.MAX_TRADE_SIZE_EXCEEDED,
                    "new trade size exceeds the configured portfolio limit",
                )
            if post_position_weight > max_single_position:
                reject(
                    RiskViolationCode.MAX_POSITION_EXPOSURE_EXCEEDED,
                    "post-trade single-position exposure exceeds the configured limit",
                )
            if post_cash < 0 or post_cash_weight < min_cash_reserve:
                reject(
                    RiskViolationCode.MIN_CASH_RESERVE_BREACHED,
                    "post-trade cash would fall below the configured reserve",
                )
            if context.daily_new_trade_count >= max_daily_new_trades:
                reject(
                    RiskViolationCode.MAX_DAILY_TRADES_EXCEEDED,
                    "the daily new-trade limit has been reached",
                )

        status = RiskDecisionStatus.APPROVED if not violations else RiskDecisionStatus.REJECTED
        decision = RiskDecision(
            decision_id=self._uuid_factory(),
            proposal_id=proposal.proposal_id,
            status=status,
            evaluated_at=context.evaluated_at,
            violations=tuple(violations),
            metrics={
                "portfolio_value": str(total_value),
                "current_position_value": str(current_value),
                "trade_weight": str(trade_weight),
                "post_position_weight": str(post_position_weight),
                "post_cash_weight": str(post_cash_weight),
            },
        )

        authorization = None
        if status is RiskDecisionStatus.APPROVED and issue_authorization:
            authorization = self._issue_authorization(proposal, context.evaluated_at)

        self._audit_sink.record(
            AuditEvent(
                event_id=self._uuid_factory(),
                event_type=AuditEventType.RISK_EVALUATED,
                timestamp=context.evaluated_at,
                correlation_id=decision.decision_id,
                proposal_id=proposal.proposal_id,
                portfolio_value=total_value if total_value >= 0 else None,
                decision=status,
                confidence=proposal.confidence,
                risk_violations=tuple(violation.code for violation in violations),
                result=(
                    "risk-approved" if status is RiskDecisionStatus.APPROVED else "risk-rejected"
                ),
            )
        )
        return RiskEvaluation(
            decision=decision,
            authorization=authorization,
            authorization_deferred=status is RiskDecisionStatus.APPROVED
            and not issue_authorization,
        )

    def _confidence_threshold_for(
        self,
        *,
        proposal: TradeProposal,
        legacy_minimum_confidence: Decimal,
        reject: Callable[[RiskViolationCode, str], None],
    ) -> Decimal | None:
        if (
            proposal.confidence_model_version == CONFIDENCE_MODEL_V1
            and proposal.confidence_semantics_version == CONFIDENCE_SEMANTICS_V1
        ):
            return legacy_minimum_confidence
        if (
            proposal.confidence_model_version == CONFIDENCE_MODEL_V2_B
            and proposal.confidence_semantics_version == CONFIDENCE_SEMANTICS_V2
        ):
            if (
                proposal.confidence_threshold != V2_B_THRESHOLD
                or proposal.confidence_threshold_provenance != V2_B_THRESHOLD_PROVENANCE
            ):
                reject(
                    RiskViolationCode.INVALID_CONFIDENCE_SEMANTICS,
                    "V2_B proposal confidence metadata is missing or malformed",
                )
                return None
            return V2_B_THRESHOLD
        reject(
            RiskViolationCode.INVALID_CONFIDENCE_SEMANTICS,
            "proposal confidence model or semantics is not supported by the Risk Manager",
        )
        return None

    def assert_authorized(
        self,
        proposal: TradeProposal,
        authorization: RiskAuthorization,
        *,
        at: datetime | None = None,
    ) -> None:
        """Fail unless the capability was issued by this Risk Manager for this proposal."""

        check_time = at or self._clock()
        if check_time.tzinfo is None or check_time.utcoffset() is None:
            raise ValueError("authorization verification time must include a timezone")
        if authorization.expires_at <= check_time:
            raise RiskAuthorizationError("risk authorization has expired")
        if self._kill_switch.state.active:
            raise RiskAuthorizationError("the global kill switch is active")
        if authorization.proposal_id != proposal.proposal_id:
            raise RiskAuthorizationError("risk authorization belongs to another proposal")
        if authorization.proposal_digest != self._proposal_digest(proposal):
            raise RiskAuthorizationError("trade proposal changed after risk approval")
        if authorization.policy_digest != self._policy_digest:
            raise RiskAuthorizationError("risk policy changed after approval")
        expected = self._sign_authorization(authorization)
        if not hmac.compare_digest(expected, authorization.signature):
            raise RiskAuthorizationError("risk authorization signature is invalid")

    def _issue_authorization(
        self, proposal: TradeProposal, issued_at: datetime
    ) -> RiskAuthorization:
        unsigned = RiskAuthorization(
            authorization_id=self._uuid_factory(),
            proposal_id=proposal.proposal_id,
            proposal_digest=self._proposal_digest(proposal),
            policy_digest=self._policy_digest,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=self._policy.authorization_ttl_seconds),
            signature="0" * 64,
        )
        return unsigned.model_copy(update={"signature": self._sign_authorization(unsigned)})

    def _proposal_digest(self, proposal: TradeProposal) -> str:
        return self._digest_json(proposal.model_dump(mode="json"))

    def _sign_authorization(self, authorization: RiskAuthorization) -> str:
        payload = "|".join(
            (
                str(authorization.authorization_id),
                str(authorization.proposal_id),
                authorization.proposal_digest,
                authorization.policy_digest,
                authorization.issued_at.isoformat(),
                authorization.expires_at.isoformat(),
            )
        ).encode("utf-8")
        return hmac.new(self._authorization_key, payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _digest_json(value: object) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
