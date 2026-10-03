"""Fail-closed configuration models for the current Demo-only build."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import (
    AIProviderMode,
    BrokerExecutionMode,
    BrokerProviderMode,
    Currency,
    Environment,
    EtoroTransportMode,
    ExecutionPolicy,
    OperatingMode,
    ProviderMode,
)


class TargetAllocations(BaseModel):
    """Strategic target weights; these are not immediate order instructions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    global_equity_etf: Decimal = Field(default=Decimal("0.40"), ge=0, le=1)
    gold: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)
    nvidia: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    cash: Decimal = Field(default=Decimal("0.25"), ge=0, le=1)

    @model_validator(mode="after")
    def allocations_sum_to_one(self) -> "TargetAllocations":
        total = self.global_equity_etf + self.gold + self.nvidia + self.cash
        if total != Decimal("1"):
            raise ValueError("target allocations must sum exactly to 1")
        return self


class RiskPolicyConfig(BaseModel):
    """Deterministic hard limits enforced by the Risk Manager."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_single_position: Decimal = Field(default=Decimal("0.25"), gt=0, lt=1)
    max_trade_size: Decimal = Field(default=Decimal("0.10"), gt=0, lt=1)
    min_cash_reserve: Decimal = Field(default=Decimal("0.07"), ge=0, lt=1)
    max_daily_new_trades: int = Field(default=3, ge=0)
    minimum_confidence: Decimal = Field(default=Decimal("0.70"), ge=0, le=1)
    max_price_age_seconds: int = Field(default=300, gt=0)
    authorization_ttl_seconds: int = Field(default=120, gt=0, le=600)
    portfolio_value_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)

    @model_validator(mode="after")
    def validate_relative_limits(self) -> "RiskPolicyConfig":
        if self.max_trade_size > self.max_single_position:
            raise ValueError("max_trade_size cannot exceed max_single_position")
        if self.max_single_position + self.min_cash_reserve > Decimal("1"):
            raise ValueError("position and cash limits cannot exceed total portfolio value")
        return self


class AegisStrategyConfig(BaseModel):
    """Conservative deterministic strategy inputs, never execution permissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_supporting_news: int = Field(default=1, ge=1)
    minimum_news_sentiment: Decimal = Field(default=Decimal("0.20"), ge=-1, le=1)
    maximum_quote_age_seconds: int = Field(default=300, gt=0)
    maximum_news_age_seconds: int = Field(default=86_400, gt=0)
    proposed_trade_weight: Decimal = Field(default=Decimal("0.05"), gt=0, le=Decimal("0.10"))
    target_position_weight: Decimal = Field(default=Decimal("0.15"), gt=0, le=Decimal("0.25"))
    baseline_confidence: Decimal = Field(default=Decimal("0.75"), ge=0, le=1)
    confidence_profile: str = Field(default="V1_LEGACY", min_length=1)
    exit_policy_profile: str = Field(default="EXITPOLICY_V1_LEGACY", min_length=1)

    @model_validator(mode="after")
    def confidence_profile_is_explicitly_supported(self) -> "AegisStrategyConfig":
        if self.confidence_profile not in {"V1_LEGACY", "V2_B_GUARDED"}:
            raise ValueError("confidence profile must be V1_LEGACY or V2_B_GUARDED")
        if self.exit_policy_profile not in {"EXITPOLICY_V1_LEGACY", "EXITPOLICY_V2_GUARDED"}:
            raise ValueError(
                "exit policy profile must be EXITPOLICY_V1_LEGACY or EXITPOLICY_V2_GUARDED"
            )
        return self


class MarketScannerConfig(BaseModel):
    """Read-only opportunity scanner limits and discovery hints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    discovery_limit: int = Field(default=50, ge=1, le=500)
    ranked_shortlist_limit: int = Field(default=20, ge=1, le=100)
    deep_analysis_limit: int = Field(default=5, ge=1, le=20)
    etoro_search_text: str | None = Field(default=None, min_length=1)
    etoro_max_pages: int = Field(default=1, ge=1, le=10)
    active_cycle_minimum_coverage_ratio: Decimal | None = Field(default=None, gt=0, le=1)
    live_acquisition_batch_size: int = Field(default=64, ge=1, le=1000)
    live_acquisition_concurrency: int = Field(default=4, ge=1, le=16)

    @model_validator(mode="after")
    def limits_are_ordered(self) -> "MarketScannerConfig":
        if self.deep_analysis_limit > self.ranked_shortlist_limit:
            raise ValueError("deep_analysis_limit cannot exceed ranked_shortlist_limit")
        if self.ranked_shortlist_limit > self.discovery_limit:
            raise ValueError("ranked_shortlist_limit cannot exceed discovery_limit")
        return self


class ProviderConfig(BaseModel):
    """Provider selection only; secret values are deliberately not modeled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market_data: ProviderMode = ProviderMode.FIXTURE
    news: ProviderMode = ProviderMode.FIXTURE
    ai: AIProviderMode = AIProviderMode.DETERMINISTIC
    broker: BrokerProviderMode = BrokerProviderMode.NONE
    etoro_read_enabled: bool = False

    @model_validator(mode="after")
    def read_only_broker_selection_is_consistent(self) -> "ProviderConfig":
        if self.etoro_read_enabled != (self.broker is BrokerProviderMode.ETORO_READ_ONLY):
            raise ValueError("eToro read selection and ETORO_READ_ENABLED must agree")
        return self


class PaperTradingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    fee_rate: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.05"))
    slippage_rate: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.05"))


class ApplicationConfig(BaseModel):
    """Top-level configuration. Production execution is unavailable in this build."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Environment = Environment.DEMO
    operating_mode: OperatingMode = OperatingMode.OFFLINE_PAPER
    initial_capital_eur: Decimal = Field(default=Decimal("200"), gt=0)
    # Kept under the legacy field name for config/API compatibility; the
    # explicit currency below is authoritative for all live comparisons.
    authorized_capital_eur: Decimal | None = Field(default=None, gt=0)
    authorized_capital_currency: Currency = Currency.EUR
    target_allocations: TargetAllocations = Field(default_factory=TargetAllocations)
    risk: RiskPolicyConfig = Field(default_factory=RiskPolicyConfig)
    strategy: AegisStrategyConfig = Field(default_factory=AegisStrategyConfig)
    scanner: MarketScannerConfig = Field(default_factory=MarketScannerConfig)
    providers: ProviderConfig = Field(default_factory=ProviderConfig)
    paper_trading: PaperTradingConfig = Field(default_factory=PaperTradingConfig)
    kill_switch: bool = True
    production_trading_enabled: bool = False
    etoro_api_enabled: bool = False
    etoro_demo_execution_enabled: bool = False
    etoro_demo_automatic_pilot_enabled: bool = False
    demo_smoke_test_opt_in: bool = False
    etoro_transport_mode: EtoroTransportMode = EtoroTransportMode.SYSTEM_PROXY
    execution_policy: ExecutionPolicy = ExecutionPolicy.ADVISORY
    broker_execution_mode: BrokerExecutionMode = BrokerExecutionMode.READ_ONLY
    log_level: str = "INFO"

    @property
    def authorized_capital(self) -> Decimal | None:
        """Configured authorization amount in ``authorized_capital_currency``."""
        return self.authorized_capital_eur

    @model_validator(mode="after")
    def enforce_demo_only_build(self) -> "ApplicationConfig":
        if self.broker_execution_mode is BrokerExecutionMode.REAL_EXECUTION:
            raise ValueError("REAL_EXECUTION is unavailable in this build")
        if self.broker_execution_mode is BrokerExecutionMode.DEMO_EXECUTION and (
            self.operating_mode is not OperatingMode.ETORO_DEMO
            or not self.etoro_api_enabled
            or not self.etoro_demo_execution_enabled
        ):
            raise ValueError(
                "DEMO_EXECUTION requires ETORO_DEMO mode and explicitly enabled Demo API"
            )
        if self.environment is Environment.PRODUCTION:
            raise ValueError("PRODUCTION is disabled in this build")
        if self.production_trading_enabled:
            raise ValueError("production trading cannot be enabled in this build")
        if self.etoro_demo_execution_enabled and (
            self.operating_mode is not OperatingMode.ETORO_DEMO or not self.etoro_api_enabled
        ):
            raise ValueError("Demo execution requires ETORO_DEMO mode and enabled API")
        if self.demo_smoke_test_opt_in and not self.etoro_demo_execution_enabled:
            raise ValueError("Demo smoke-test opt-in requires Demo execution to be enabled")
        if self.etoro_demo_automatic_pilot_enabled and (
            self.operating_mode is not OperatingMode.ETORO_DEMO or not self.etoro_api_enabled
        ):
            raise ValueError("automatic Demo pilot requires ETORO_DEMO mode and enabled API")
        if self.etoro_demo_automatic_pilot_enabled and not self.etoro_demo_execution_enabled:
            raise ValueError(
                "automatic Demo pilot requires Demo execution to be explicitly enabled"
            )
        if self.execution_policy is ExecutionPolicy.AUTONOMOUS and (
            self.operating_mode is not OperatingMode.ETORO_DEMO
            or not self.etoro_demo_execution_enabled
        ):
            raise ValueError("AUTONOMOUS policy is Demo-only and disabled by default")
        if self.execution_policy is ExecutionPolicy.CONFIRM and self.operating_mode not in {
            OperatingMode.OFFLINE_PAPER,
            OperatingMode.ETORO_DEMO,
        }:
            raise ValueError("CONFIRM policy is available only for paper or Demo execution")
        if (
            self.operating_mode in {OperatingMode.ETORO_DEMO, OperatingMode.ETORO_REAL_READ_ONLY}
            and not self.etoro_api_enabled
        ):
            raise ValueError("eToro modes require the API to be explicitly enabled")
        return self
