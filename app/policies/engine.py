"""Deterministic policy decisions for broker-neutral instruments."""

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.enums import AssetClass, MarketStatus, TradeSide
from app.domain.portfolio import PortfolioSnapshot
from app.domain.universe import (
    CandidateState,
    DataQualityStatus,
    OpportunityFeatures,
    UniversalInstrument,
)
from app.policies.models import AssetPolicy, PolicyDecision


class AssetPolicyEngine:
    """Programmatic policy gate used before agent and risk-manager review."""

    def __init__(self, policies: Iterable[AssetPolicy], *, policy_version: str) -> None:
        self._policies = {policy.asset_class: policy for policy in policies}
        self._policy_version = policy_version

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def policy_for(self, asset_class: AssetClass) -> AssetPolicy:
        try:
            return self._policies[asset_class]
        except KeyError as exc:
            raise KeyError(f"no policy configured for {asset_class.value}") from exc

    def policy_snapshot(self) -> tuple[dict[str, object], ...]:
        return tuple(
            policy.model_dump(mode="json")
            for policy in sorted(self._policies.values(), key=lambda item: item.asset_class.value)
        )

    def evaluate(
        self,
        instrument: UniversalInstrument,
        *,
        portfolio: PortfolioSnapshot,
        as_of: datetime,
        features: OpportunityFeatures,
        side: TradeSide = TradeSide.BUY,
        requested_leverage: Decimal = Decimal("1"),
        fx_rate_available: bool | None = None,
        daily_new_trade_count: int = 0,
    ) -> PolicyDecision:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("policy evaluation time must include a timezone")
        policy = self.policy_for(instrument.asset_class)
        reasons: list[str] = []
        data_quality = self.assess_data_quality(instrument, as_of=as_of, policy=policy)
        state = CandidateState.OPEN_AND_ALLOWED

        if instrument.numeric_instrument_id is None or instrument.symbol.strip() == "":
            reasons.append("instrument metadata is insufficient")
            state = CandidateState.INSUFFICIENT_METADATA
        if instrument.asset_class is AssetClass.UNKNOWN:
            reasons.append("asset classification is unknown")
        if instrument.currency is None:
            reasons.append("instrument currency is unknown")
            state = CandidateState.INSUFFICIENT_METADATA
        if instrument.settlement_type is None:
            reasons.append("settlement type is unknown")
            state = CandidateState.INSUFFICIENT_METADATA
        if data_quality is DataQualityStatus.STALE:
            reasons.append("price data is stale")
            state = CandidateState.STALE_DATA
        elif data_quality is DataQualityStatus.INSUFFICIENT and policy.require_fresh_price:
            reasons.append("fresh price is unavailable")
            state = CandidateState.INSUFFICIENT_METADATA

        if (
            state is CandidateState.OPEN_AND_ALLOWED
            and instrument.market_status not in policy.actionable_market_statuses
        ):
            reasons.append(f"market status is {instrument.market_status.value}")
            state = (
                CandidateState.UNKNOWN
                if instrument.market_status is MarketStatus.UNKNOWN
                else CandidateState.MARKET_CLOSED
            )

        if (
            state is CandidateState.OPEN_AND_ALLOWED
            and instrument.currency is not None
            and instrument.currency is not portfolio.currency
            and fx_rate_available is not True
        ):
            reasons.append("fresh FX conversion is unavailable")
            state = CandidateState.FX_UNAVAILABLE

        eligibility = instrument.broker_eligibility
        if state is CandidateState.OPEN_AND_ALLOWED and policy.require_broker_eligibility:
            if eligibility is None:
                reasons.append("broker eligibility is unknown")
                state = CandidateState.BROKER_INELIGIBLE
            elif not eligibility.verified or eligibility.allow_open is not True:
                reasons.append("broker eligibility does not allow opening")
                state = CandidateState.BROKER_INELIGIBLE

        policy_blocked = False
        if not policy.enabled:
            reasons.append(f"{instrument.asset_class.value} policy is disabled")
            policy_blocked = True
        if side is TradeSide.BUY and not policy.long_allowed:
            reasons.append("long exposure is disabled by asset policy")
            policy_blocked = True
        if side is TradeSide.SELL and not policy.short_allowed:
            reasons.append("short exposure is disabled by asset policy")
            policy_blocked = True
        if requested_leverage != Decimal("1") and (
            not policy.leverage_allowed or requested_leverage > policy.max_leverage
        ):
            reasons.append("requested leverage exceeds asset policy")
            policy_blocked = True
        if instrument.max_leverage < requested_leverage:
            reasons.append("broker metadata does not support requested leverage")
            policy_blocked = True
        if policy.maximum_spread is not None and features.spread_percentage is not None:
            if features.spread_percentage > policy.maximum_spread:
                reasons.append("spread exceeds asset policy")
                policy_blocked = True
        if policy.volatility_limit is not None and features.volatility is not None:
            if features.volatility > policy.volatility_limit:
                reasons.append("volatility exceeds asset policy")
                policy_blocked = True

        risk_budget_blocked = False
        if features.current_portfolio_weight >= policy.max_position_exposure:
            reasons.append("current instrument exposure exceeds asset policy")
            risk_budget_blocked = True
        if (
            policy.maximum_drawdown_budget is not None
            and features.drawdown > policy.maximum_drawdown_budget
        ):
            reasons.append("portfolio drawdown exceeds asset policy budget")
            risk_budget_blocked = True
        if daily_new_trade_count >= policy.max_daily_new_trades:
            reasons.append("daily new trade budget is exhausted")
            risk_budget_blocked = True

        if state is CandidateState.OPEN_AND_ALLOWED and policy_blocked:
            state = CandidateState.OPEN_BUT_POLICY_BLOCKED
        if state is CandidateState.OPEN_AND_ALLOWED and risk_budget_blocked:
            state = CandidateState.RISK_BUDGET_BLOCKED

        allowed = state is CandidateState.OPEN_AND_ALLOWED and not policy_blocked
        return PolicyDecision(
            asset_class=instrument.asset_class,
            allowed=allowed,
            candidate_state=state,
            data_quality=data_quality,
            reasons=tuple(dict.fromkeys(reasons)),
            policy_version=self._policy_version,
            policy_enabled=policy.enabled,
        )

    @staticmethod
    def assess_data_quality(
        instrument: UniversalInstrument, *, as_of: datetime, policy: AssetPolicy
    ) -> DataQualityStatus:
        if instrument.price_timestamp is None or instrument.last_price is None:
            return DataQualityStatus.INSUFFICIENT
        age = as_of - instrument.price_timestamp
        if age < timedelta(0) or age > timedelta(seconds=policy.max_price_age_seconds):
            return DataQualityStatus.STALE
        if instrument.bid is None or instrument.ask is None:
            return DataQualityStatus.PARTIAL
        if instrument.market_status is MarketStatus.UNKNOWN:
            return DataQualityStatus.PARTIAL
        return DataQualityStatus.GOOD
