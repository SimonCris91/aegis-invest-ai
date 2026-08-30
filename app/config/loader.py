"""Environment-backed configuration loading with safe local .env support."""

from collections.abc import Mapping
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
)
from app.domain.enums import (
    AIProviderMode,
    BrokerProviderMode,
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
        smoke_opt_in = _parse_bool(
            "ETORO_DEMO_SMOKE_TEST_OPT_IN",
            source.get("ETORO_DEMO_SMOKE_TEST_OPT_IN", "false"),
        )
        etoro_transport_mode = EtoroTransportMode(
            source.get("AEGIS_ETORO_TRANSPORT_MODE", "SYSTEM_PROXY").upper()
        )
        execution_policy = ExecutionPolicy(source.get("AEGIS_EXECUTION_POLICY", "ADVISORY").upper())
        confidence_profile = source.get("AEGIS_CONFIDENCE_PROFILE", "V1_LEGACY").strip().upper()
        exit_policy_profile = (
            source.get("AEGIS_EXIT_POLICY_PROFILE", "EXITPOLICY_V1_LEGACY").strip().upper()
        )
        return ApplicationConfig(
            environment=environment,
            operating_mode=operating_mode,
            etoro_api_enabled=api_enabled,
            etoro_demo_execution_enabled=demo_enabled,
            demo_smoke_test_opt_in=smoke_opt_in,
            etoro_transport_mode=etoro_transport_mode,
            execution_policy=execution_policy,
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
            ),
            paper_trading=PaperTradingConfig(
                enabled=_parse_bool("AEGIS_PAPER_TRADING_ENABLED", raw_paper_enabled)
            ),
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigLoadError(str(exc)) from exc
