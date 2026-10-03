"""Environment-backed configuration loading with safe local .env support."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from os import environ
from pathlib import Path

from pydantic import ValidationError

from app.config.dotenv import DotenvLoadError, load_dotenv_values
from app.config.models import (
    AegisStrategyConfig,
    ApplicationConfig,
    MarketScannerConfig,
    PaperTradingConfig,
    ProviderConfig,
    RiskPolicyConfig,
)
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


class ConfigLoadError(ValueError):
    """Raised when environment configuration is invalid or unsafe."""


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _parse_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigLoadError(f"{name} must be a boolean value")


def _parse_positive_int(name: str, value: str, *, default: int) -> int:
    raw = value.strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ConfigLoadError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigLoadError(f"{name} must be a positive integer")
    return parsed


def _parse_optional_positive_decimal(name: str, value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ConfigLoadError(f"{name} must be a positive decimal") from exc
    if parsed <= 0:
        raise ConfigLoadError(f"{name} must be a positive decimal")
    return parsed


def _parse_fraction(name: str, value: str | None, *, default: str) -> Decimal:
    raw = default if value is None or not value.strip() else value.strip()
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigLoadError(f"{name} must be a decimal fraction") from exc
    if parsed < 0 or parsed >= 1:
        raise ConfigLoadError(f"{name} must be between 0 and 1")
    return parsed


def _parse_broker_execution_mode(value: str | None) -> BrokerExecutionMode:
    raw = "" if value is None else value.strip().upper()
    if not raw:
        return BrokerExecutionMode.READ_ONLY
    try:
        return BrokerExecutionMode(raw)
    except ValueError:
        return BrokerExecutionMode.READ_ONLY


def load_runtime_values(
    values: Mapping[str, str] | None = None,
    *,
    env_file: Path | None = None,
) -> dict[str, str]:
    """Return process values overlaid on local .env values, without mutating os.environ."""

    if values is not None and env_file is None:
        return dict(values)

    source = dict(environ if values is None else values)
    dotenv_enabled = _parse_bool("AEGIS_DOTENV_ENABLED", source.get("AEGIS_DOTENV_ENABLED", "true"))
    if not dotenv_enabled:
        return source

    path = DEFAULT_ENV_FILE if env_file is None else env_file
    try:
        dotenv_values = load_dotenv_values(path)
    except DotenvLoadError as exc:
        raise ConfigLoadError(str(exc)) from exc

    merged = dict(dotenv_values)
    merged.update(source)
    return merged


def load_config(values: Mapping[str, str] | None = None) -> ApplicationConfig:
    """Load the small, non-secret configuration surface used in this phase."""

    source = load_runtime_values() if values is None else values
    raw_environment = source.get("AEGIS_ENVIRONMENT", Environment.DEMO.value)
    raw_kill_switch = source.get("AEGIS_KILL_SWITCH", "true")
    raw_paper_enabled = source.get("AEGIS_PAPER_TRADING_ENABLED", "true")
    log_level = source.get("AEGIS_LOG_LEVEL", "INFO").upper()

    try:
        environment = Environment(raw_environment.upper())
    except ValueError as exc:
        raise ConfigLoadError("AEGIS_ENVIRONMENT must be DEMO or PRODUCTION") from exc

    try:
        market_data = ProviderMode(source.get("AEGIS_MARKET_DATA_PROVIDER", "fixture").lower())
        news = ProviderMode(source.get("AEGIS_NEWS_PROVIDER", "fixture").lower())
        ai = AIProviderMode(source.get("AEGIS_AI_PROVIDER", "deterministic").lower())
        broker = BrokerProviderMode(source.get("AEGIS_BROKER_PROVIDER", "none").lower())
        etoro_read_enabled = _parse_bool(
            "AEGIS_ETORO_READ_ENABLED", source.get("AEGIS_ETORO_READ_ENABLED", "false")
        )
        operating_mode = OperatingMode(source.get("AEGIS_OPERATING_MODE", "OFFLINE_PAPER").upper())
        api_enabled = _parse_bool("ETORO_API_ENABLED", source.get("ETORO_API_ENABLED", "false"))
        demo_enabled = _parse_bool(
            "ETORO_DEMO_EXECUTION_ENABLED", source.get("ETORO_DEMO_EXECUTION_ENABLED", "false")
        )
        automatic_demo_pilot_enabled = _parse_bool(
            "AEGIS_ETORO_DEMO_AUTOMATIC_PILOT_ENABLED",
            source.get("AEGIS_ETORO_DEMO_AUTOMATIC_PILOT_ENABLED", "false"),
        )
        smoke_opt_in = _parse_bool(
            "ETORO_DEMO_SMOKE_TEST_OPT_IN",
            source.get("ETORO_DEMO_SMOKE_TEST_OPT_IN", "false"),
        )
        etoro_transport_mode = EtoroTransportMode(
            source.get("AEGIS_ETORO_TRANSPORT_MODE", "SYSTEM_PROXY").upper()
        )
        execution_policy = ExecutionPolicy(source.get("AEGIS_EXECUTION_POLICY", "ADVISORY").upper())
        broker_execution_mode = _parse_broker_execution_mode(
            source.get("AEGIS_BROKER_EXECUTION_MODE")
        )
        raw_capital_currency = source.get("AEGIS_AUTHORIZED_CAPITAL_CURRENCY")
        if raw_capital_currency is None:
            raw_capital_currency = (
                "USD"
                if source.get("AEGIS_AUTHORIZED_CAPITAL_USD")
                and not source.get("AEGIS_AUTHORIZED_CAPITAL_EUR")
                else "EUR"
            )
        try:
            authorized_capital_currency = Currency(raw_capital_currency.strip().upper())
        except ValueError as exc:
            raise ConfigLoadError(
                "AEGIS_AUTHORIZED_CAPITAL_CURRENCY must be EUR or USD"
            ) from exc
        raw_authorized_capital = source.get("AEGIS_AUTHORIZED_CAPITAL")
        if raw_authorized_capital is None:
            raw_authorized_capital = source.get(
                f"AEGIS_AUTHORIZED_CAPITAL_{authorized_capital_currency.value}"
            )
        if raw_authorized_capital is None:
            # Backward compatibility for installations that still use the
            # original EUR-named variable with an explicit currency setting.
            raw_authorized_capital = source.get("AEGIS_AUTHORIZED_CAPITAL_EUR")
        authorized_capital = _parse_optional_positive_decimal(
            "AEGIS_AUTHORIZED_CAPITAL", raw_authorized_capital
        )
        confidence_profile = source.get("AEGIS_CONFIDENCE_PROFILE", "V1_LEGACY").strip().upper()
        exit_policy_profile = (
            source.get("AEGIS_EXIT_POLICY_PROFILE", "EXITPOLICY_V1_LEGACY").strip().upper()
        )
        min_cash_reserve = _parse_fraction(
            "AEGIS_MIN_CASH_RESERVE",
            source.get("AEGIS_MIN_CASH_RESERVE"),
            default="0.07",
        )
        return ApplicationConfig(
            environment=environment,
            operating_mode=operating_mode,
            authorized_capital_eur=authorized_capital,
            authorized_capital_currency=authorized_capital_currency,
            etoro_api_enabled=api_enabled,
            etoro_demo_execution_enabled=demo_enabled,
            etoro_demo_automatic_pilot_enabled=automatic_demo_pilot_enabled,
            demo_smoke_test_opt_in=smoke_opt_in,
            etoro_transport_mode=etoro_transport_mode,
            execution_policy=execution_policy,
            broker_execution_mode=broker_execution_mode,
            kill_switch=_parse_bool("AEGIS_KILL_SWITCH", raw_kill_switch),
            log_level=log_level,
            providers=ProviderConfig(
                market_data=market_data,
                news=news,
                ai=ai,
                broker=broker,
                etoro_read_enabled=etoro_read_enabled,
            ),
            strategy=AegisStrategyConfig(
                confidence_profile=confidence_profile,
                exit_policy_profile=exit_policy_profile,
            ),
            risk=RiskPolicyConfig(min_cash_reserve=min_cash_reserve),
            scanner=MarketScannerConfig(
                discovery_limit=_parse_positive_int(
                    "AEGIS_SCANNER_DISCOVERY_LIMIT",
                    source.get("AEGIS_SCANNER_DISCOVERY_LIMIT", "50"),
                    default=50,
                ),
                ranked_shortlist_limit=_parse_positive_int(
                    "AEGIS_SCANNER_RANKED_SHORTLIST_LIMIT",
                    source.get("AEGIS_SCANNER_RANKED_SHORTLIST_LIMIT", "20"),
                    default=20,
                ),
                deep_analysis_limit=_parse_positive_int(
                    "AEGIS_SCANNER_DEEP_ANALYSIS_LIMIT",
                    source.get("AEGIS_SCANNER_DEEP_ANALYSIS_LIMIT", "5"),
                    default=5,
                ),
                etoro_search_text=source.get("AEGIS_ETORO_SCAN_SEARCH_TEXT", "").strip() or None,
                etoro_max_pages=_parse_positive_int(
                    "AEGIS_ETORO_SCAN_MAX_PAGES",
                    source.get("AEGIS_ETORO_SCAN_MAX_PAGES", "1"),
                    default=1,
                ),
                active_cycle_minimum_coverage_ratio=_parse_optional_positive_decimal(
                    "AEGIS_ACTIVE_CYCLE_MINIMUM_COVERAGE_RATIO",
                    source.get("AEGIS_ACTIVE_CYCLE_MINIMUM_COVERAGE_RATIO"),
                ),
                live_acquisition_batch_size=_parse_positive_int(
                    "AEGIS_LIVE_ACQUISITION_BATCH_SIZE",
                    source.get("AEGIS_LIVE_ACQUISITION_BATCH_SIZE", "64"),
                    default=64,
                ),
                live_acquisition_concurrency=_parse_positive_int(
                    "AEGIS_LIVE_ACQUISITION_CONCURRENCY",
                    source.get("AEGIS_LIVE_ACQUISITION_CONCURRENCY", "4"),
                    default=4,
                ),
            ),
            paper_trading=PaperTradingConfig(
                enabled=_parse_bool("AEGIS_PAPER_TRADING_ENABLED", raw_paper_enabled)
            ),
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigLoadError(str(exc)) from exc
