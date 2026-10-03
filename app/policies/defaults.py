"""Conservative default asset policies."""

from decimal import Decimal

from app.config.models import RiskPolicyConfig
from app.domain.enums import AssetClass, MarketStatus
from app.policies.engine import AssetPolicyEngine
from app.policies.models import AssetPolicy

DEFAULT_POLICY_VERSION = "asset-policy-v3-crypto-exposure-cap"


def conservative_asset_policies(
    *, minimum_cash_reserve: Decimal | None = None
) -> tuple[AssetPolicy, ...]:
    reserve = (
        RiskPolicyConfig().min_cash_reserve
        if minimum_cash_reserve is None
        else minimum_cash_reserve
    )
    enabled_long = {
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.CRYPTO,
    }
    policies: list[AssetPolicy] = []
    for asset_class in AssetClass:
        if asset_class in enabled_long:
            exposure = Decimal("0.02") if asset_class is AssetClass.CRYPTO else Decimal("0.25")
            trade = Decimal("0.02") if asset_class is AssetClass.CRYPTO else Decimal("0.10")
            statuses = (
                (MarketStatus.OPEN, MarketStatus.CONTINUOUS_24_7)
                if asset_class is AssetClass.CRYPTO
                else (MarketStatus.OPEN,)
            )
            policies.append(
                AssetPolicy(
                    asset_class=asset_class,
                    enabled=True,
                    long_allowed=True,
                    short_allowed=False,
                    leverage_allowed=False,
                    max_leverage=Decimal("1"),
                    max_position_exposure=exposure,
                    max_new_trade_exposure=trade,
                    minimum_cash_reserve=reserve,
                    max_daily_new_trades=3,
                    minimum_confidence=Decimal("0.75")
                    if asset_class is AssetClass.CRYPTO
                    else Decimal("0.70"),
                    maximum_spread=Decimal("0.03")
                    if asset_class is AssetClass.CRYPTO
                    else Decimal("0.02"),
                    volatility_limit=Decimal("0.15")
                    if asset_class is AssetClass.CRYPTO
                    else Decimal("0.08"),
                    maximum_drawdown_budget=Decimal("0.15")
                    if asset_class is AssetClass.CRYPTO
                    else Decimal("0.20"),
                    actionable_market_statuses=statuses,
                    settlement_constraints=("fully-funded",),
                    broker_capability_requirements=("authenticated-read", "demo-eligibility"),
                )
            )
        else:
            policies.append(
                AssetPolicy(
                    asset_class=asset_class,
                    enabled=False,
                    long_allowed=False,
                    short_allowed=False,
                    leverage_allowed=False,
                    max_leverage=Decimal("1"),
                    max_position_exposure=Decimal("0.10"),
                    max_new_trade_exposure=Decimal("0.05"),
                    minimum_cash_reserve=Decimal("0.20"),
                    max_daily_new_trades=0,
                    minimum_confidence=Decimal("0.90"),
                    maximum_spread=Decimal("0.01"),
                    volatility_limit=Decimal("0.05"),
                    maximum_drawdown_budget=Decimal("0.05"),
                    actionable_market_statuses=(MarketStatus.OPEN, MarketStatus.CONTINUOUS_24_7),
                    settlement_constraints=("explicit-opt-in-required",),
                    broker_capability_requirements=("policy-enabled",),
                )
            )
    return tuple(policies)


def default_asset_policy_engine(
    *, minimum_cash_reserve: Decimal | None = None
) -> AssetPolicyEngine:
    return AssetPolicyEngine(
        conservative_asset_policies(minimum_cash_reserve=minimum_cash_reserve),
        policy_version=DEFAULT_POLICY_VERSION,
    )
