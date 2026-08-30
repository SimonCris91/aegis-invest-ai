import inspect
from pathlib import Path

import pytest

import app.agent
import app.agent.context
import app.agent.models
import app.agent.ports
import app.agent.safety
import app.agent.service
import app.paper_trading.engine
from app.config.models import ApplicationConfig
from app.domain.enums import Environment
from app.etoro.ports import EtoroReadClient


def test_agent_modules_have_no_execution_broker_or_risk_imports() -> None:
    modules = (
        app.agent.context,
        app.agent.models,
        app.agent.ports,
        app.agent.safety,
        app.agent.service,
    )
    prohibited = (
        "app.etoro",
        "app.execution",
        "app.paper_trading",
        "app.risk",
        "RiskAuthorization",
        "KillSwitch",
    )

    for module in modules:
        source = inspect.getsource(module)
        assert not any(value in source for value in prohibited)


def test_paper_engine_has_no_broker_dependency() -> None:
    source = inspect.getsource(app.paper_trading.engine)

    assert "app.etoro" not in source
    assert "EtoroReadClient" not in source


def test_etoro_protocol_remains_read_only() -> None:
    methods = {
        name
        for name, value in inspect.getmembers(EtoroReadClient)
        if inspect.isfunction(value) and not name.startswith("_")
    }

    assert methods == {"get_instrument_metadata", "get_portfolio", "get_price"}


def test_production_configuration_remains_impossible() -> None:
    with pytest.raises(ValueError):
        ApplicationConfig(environment=Environment.PRODUCTION)
    with pytest.raises(ValueError):
        ApplicationConfig(production_trading_enabled=True)


def test_mcp_write_tools_remain_disabled() -> None:
    root = Path(__file__).resolve().parents[1]
    config = (root / ".codex" / "config.toml").read_text(encoding="utf-8")

    assert 'disabled_tools = ["execute-write", "place-trade", "place-close"]' in config
    assert "enabled_tools" not in config


def test_env_file_is_ignored_and_example_contains_only_empty_secret_placeholders() -> None:
    root = Path(__file__).resolve().parents[1]

    ignore_lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore_lines
    assert ".env.*" in ignore_lines
    assert "!.env.example" in ignore_lines

    values = {
        line.split("=", maxsplit=1)[0]: line.split("=", maxsplit=1)[1]
        for line in (root / ".env.example").read_text(encoding="utf-8").splitlines()
        if "API_KEY=" in line
    }
    assert values
    assert set(values.values()) == {""}
