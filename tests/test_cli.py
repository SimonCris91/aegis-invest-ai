import json
import os
from pathlib import Path

import pytest

from app.brokers.etoro.readiness import DEFAULT_READINESS_STORE_PATH
from app.main.__main__ import main
from app.storage.sqlite import SqliteRecordStore


@pytest.mark.parametrize("command", ["health", "portfolio", "analyze", "paper-status"])
def test_safe_cli_commands_are_branded_and_never_offer_real_execution(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main((command,), values={}) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["application"] == "Aegis Invest AI"
    assert payload["real_execution_available"] is False


def test_health_reports_agent_and_fail_closed_defaults(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AEGIS_"):
            monkeypatch.delenv(name, raising=False)

    main(("health",), values={})
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "agent": "Aegis Agent",
        "application": "Aegis Invest AI",
        "environment": "DEMO",
        "kill_switch": True,
        "paper_trading_available": True,
        "production_trading_enabled": False,
        "real_execution_available": False,
        "status": "ok",
    }


def test_etoro_demo_preflight_cli_fails_closed_without_credentials(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(("etoro-demo-preflight",), values={"ETORO_API_ENABLED": "true"}) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["application"] == "Aegis Invest AI"
    assert payload["pre_flight"] == "FAIL"
    assert payload["blocker_code"] == "CREDENTIALS"
    assert payload["demo_write_performed"] is False
    assert payload["real_write_performed"] is False
    assert payload["real_execution_available"] is False


def test_etoro_readiness_cli_configures_persistent_store(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(("etoro-readiness",), values={}) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["application"] == "Aegis Invest AI"
    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["record_store_path"] == str(DEFAULT_READINESS_STORE_PATH)
    assert (tmp_path / DEFAULT_READINESS_STORE_PATH).exists()
    restarted = SqliteRecordStore(tmp_path / DEFAULT_READINESS_STORE_PATH)
    assert restarted.list("etoro-readiness-report")[0]["status"] == "NOT_CONFIGURED"


def test_etoro_transport_check_cli_does_not_expose_identity(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(("etoro-transport-check",), values={}) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["application"] == "Aegis Invest AI"
    assert payload["category"] == "CREDENTIALS"
    assert payload["endpoint"] == "/api/v1/me"
    assert payload["http_status"] is None
    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["transport_mode"] == "SYSTEM_PROXY"
    assert len(payload["proxy_environment"]) == 8
    assert "api-secret" not in json.dumps(payload)
    assert "user-secret" not in json.dumps(payload)
