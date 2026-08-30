import os
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.loader import ConfigLoadError, load_config, load_runtime_values
from app.config.models import ApplicationConfig, RiskPolicyConfig, TargetAllocations
from app.domain.enums import BrokerProviderMode, Environment, EtoroTransportMode


def test_default_config_is_demo_and_fail_closed() -> None:
    config = ApplicationConfig()

    assert config.environment is Environment.DEMO
    assert config.kill_switch is True
    assert config.production_trading_enabled is False
    assert config.initial_capital_eur == Decimal("200")


def test_production_environment_is_rejected_in_this_build() -> None:
    with pytest.raises(ValidationError, match="PRODUCTION is disabled"):
        ApplicationConfig(environment=Environment.PRODUCTION)


def test_production_toggle_is_rejected_in_this_build() -> None:
    with pytest.raises(ValidationError, match="production trading cannot be enabled"):
        ApplicationConfig(production_trading_enabled=True)


def test_environment_loader_accepts_explicit_demo_settings() -> None:
    config = load_config(
        {
            "AEGIS_ENVIRONMENT": "demo",
            "AEGIS_KILL_SWITCH": "false",
            "AEGIS_LOG_LEVEL": "debug",
            "AEGIS_ETORO_TRANSPORT_MODE": "direct",
        }
    )

    assert config.environment is Environment.DEMO
    assert config.kill_switch is False
    assert config.log_level == "DEBUG"
    assert config.etoro_transport_mode is EtoroTransportMode.DIRECT


def test_environment_loader_rejects_production() -> None:
    with pytest.raises(ConfigLoadError, match="PRODUCTION is disabled"):
        load_config({"AEGIS_ENVIRONMENT": "PRODUCTION"})


def test_environment_loader_rejects_ambiguous_boolean() -> None:
    with pytest.raises(ConfigLoadError, match="must be a boolean"):
        load_config({"AEGIS_KILL_SWITCH": "perhaps"})


def test_target_allocations_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match="sum exactly to 1"):
        TargetAllocations(cash=Decimal("0.20"))


def test_trade_limit_cannot_exceed_position_limit() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        RiskPolicyConfig(
            max_trade_size=Decimal("0.30"),
            max_single_position=Decimal("0.25"),
        )


def test_provider_defaults_are_offline_and_brokerless() -> None:
    config = load_config({})

    assert config.providers.broker is BrokerProviderMode.NONE
    assert config.providers.etoro_read_enabled is False
    assert config.paper_trading.enabled is True
    assert config.strategy.confidence_profile == "V1_LEGACY"


def test_confidence_profile_is_explicit_and_reversible() -> None:
    absent = load_config({})
    legacy = load_config({"AEGIS_CONFIDENCE_PROFILE": "V1_LEGACY"})
    guarded = load_config({"AEGIS_CONFIDENCE_PROFILE": "V2_B_GUARDED"})
    legacy_with_whitespace = load_config({"AEGIS_CONFIDENCE_PROFILE": " V1_LEGACY "})
    guarded_with_whitespace = load_config({"AEGIS_CONFIDENCE_PROFILE": " V2_B_GUARDED "})

    assert absent.strategy.confidence_profile == "V1_LEGACY"
    assert legacy.strategy.confidence_profile == "V1_LEGACY"
    assert guarded.strategy.confidence_profile == "V2_B_GUARDED"
    assert legacy_with_whitespace.strategy.confidence_profile == "V1_LEGACY"
    assert guarded_with_whitespace.strategy.confidence_profile == "V2_B_GUARDED"


def test_unknown_confidence_profile_fails_closed() -> None:
    with pytest.raises(ConfigLoadError, match="confidence profile"):
        load_config({"AEGIS_CONFIDENCE_PROFILE": "V2_B_AUTO"})


def test_etoro_read_selection_must_be_explicit_and_consistent() -> None:
    with pytest.raises(ConfigLoadError, match="must agree"):
        load_config({"AEGIS_ETORO_READ_ENABLED": "true"})


def test_unknown_provider_fails_closed() -> None:
    with pytest.raises(ConfigLoadError):
        load_config({"AEGIS_MARKET_DATA_PROVIDER": "invented"})


def test_unknown_etoro_transport_mode_fails_closed() -> None:
    with pytest.raises(ConfigLoadError):
        load_config({"AEGIS_ETORO_TRANSPORT_MODE": "browser-spoof"})


def test_dotenv_loader_reads_local_file_without_mutating_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ETORO_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "# synthetic test values",
                "AEGIS_KILL_SWITCH=false",
                "ETORO_API_ENABLED=true",
                "ETORO_API_KEY='synthetic-api-secret'",
                "ETORO_USER_KEY=",
            )
        ),
        encoding="utf-8",
    )

    values = load_runtime_values(values={}, env_file=env_file)
    config = load_config(values)

    assert values["ETORO_API_KEY"] == "synthetic-api-secret"
    assert values["ETORO_USER_KEY"] == ""
    assert "ETORO_API_KEY" not in os.environ
    assert config.etoro_api_enabled
    assert config.kill_switch is False


def test_process_environment_overrides_dotenv_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AEGIS_LOG_LEVEL=debug\n", encoding="utf-8")

    values = load_runtime_values(values={"AEGIS_LOG_LEVEL": "warning"}, env_file=env_file)
    config = load_config(values)

    assert config.log_level == "WARNING"


@pytest.mark.parametrize(
    "content",
    [
        "NOT_A_VALID_LINE",
        "1BAD=value",
        "ETORO_API_KEY=one\nETORO_API_KEY=two\n",
        'ETORO_API_KEY="unterminated\n',
    ],
)
def test_dotenv_loader_rejects_ambiguous_or_unsafe_syntax(tmp_path: Path, content: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigLoadError):
        load_runtime_values(values={}, env_file=env_file)
