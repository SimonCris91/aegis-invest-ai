from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.demo import (
    DEMO_ORDER_URL,
    DemoBoundEtoroExecutionClient,
    DemoExecutionError,
)
from app.brokers.etoro.demo_execution import (
    run_operational_demo_once,
    run_user_confirmed_demo_validation,
)
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse
from app.config.loader import load_config
from app.domain.enums import BrokerExecutionMode
from app.main.__main__ import main
from app.storage.sqlite import SqliteRecordStore


class NoCallTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls += 1
        raise AssertionError("transport must not be reached")


@pytest.mark.parametrize("value", [None, "", "not-a-mode"])
def test_broker_execution_mode_fails_closed_to_read_only(value: str | None) -> None:
    values = {} if value is None else {"AEGIS_BROKER_EXECUTION_MODE": value}

    config = load_config(values)

    assert config.broker_execution_mode is BrokerExecutionMode.READ_ONLY


def test_demo_client_is_immutably_bound_to_allowlisted_endpoint() -> None:
    transport = NoCallTransport()
    client = DemoBoundEtoroExecutionClient(
        credentials=EtoroCredentials(api_key="secret", user_key="secret"),
        http=DisciplinedHttpClient(transport),
    )

    assert client.endpoint == DEMO_ORDER_URL
    assert transport.calls == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://public-api.etoro.com/api/v2/trading/execution/real/orders",
        "https://evil.invalid/api/v2/trading/execution/demo/orders",
        "http://public-api.etoro.com/api/v2/trading/execution/demo/orders",
    ],
)
def test_demo_client_rejects_real_or_non_allowlisted_endpoint(endpoint: str) -> None:
    transport = NoCallTransport()

    with pytest.raises(DemoExecutionError):
        DemoBoundEtoroExecutionClient(
            credentials=EtoroCredentials(api_key="secret", user_key="secret"),
            http=DisciplinedHttpClient(transport),
            endpoint=endpoint,
        )

    assert transport.calls == 0


def test_real_execution_configuration_remains_unavailable() -> None:
    with pytest.raises(ValueError, match="REAL_EXECUTION is unavailable"):
        load_config({"AEGIS_BROKER_EXECUTION_MODE": "REAL_EXECUTION"})


def test_demo_validation_without_confirmation_is_strictly_no_write(tmp_path: Path) -> None:
    result = run_user_confirmed_demo_validation(
        load_config({}),
        values={},
        confirm_demo_write=False,
        store=SqliteRecordStore(tmp_path / "validation.sqlite3"),
        clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert result["status"] == "CONFIRM_DEMO_WRITE_REQUIRED"
    assert result["demo_submission_attempts"] == 0
    assert result["demo_write_performed"] is False
    assert result["real_write_performed"] is False


def test_operational_demo_once_requires_both_explicit_opt_ins(tmp_path: Path) -> None:
    result = run_operational_demo_once(
        load_config(
            {
                "ETORO_DEMO_SMOKE_TEST_OPT_IN": "true",
                "ETORO_DEMO_EXECUTION_ENABLED": "true",
                "AEGIS_OPERATING_MODE": "ETORO_DEMO",
                "ETORO_API_ENABLED": "true",
            }
        ),
        values={},
        confirm_demo_write=False,
        store=SqliteRecordStore(tmp_path / "validation.sqlite3"),
    )

    assert result["status"] == "EXPLICIT_DEMO_OPT_IN_REQUIRED"
    assert result["demo_submission_attempts"] == 0
    assert result["real_write_performed"] is False


def test_demo_validation_cli_requires_explicit_write_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["etoro-demo-execution-validate"], values={}) == 0

    output = capsys.readouterr().out
    assert '"status": "CONFIRM_DEMO_WRITE_REQUIRED"' in output
    assert '"demo_submission_attempts": 0' in output
    assert '"real_write_performed": false' in output


def test_demo_validation_fails_before_network_without_exact_capital(tmp_path: Path) -> None:
    config = load_config(
        {
            "AEGIS_OPERATING_MODE": "ETORO_DEMO",
            "ETORO_API_ENABLED": "true",
            "ETORO_DEMO_EXECUTION_ENABLED": "true",
            "AEGIS_BROKER_EXECUTION_MODE": "DEMO_EXECUTION",
            "AEGIS_AUTHORIZED_CAPITAL_EUR": str(Decimal("1999")),
        }
    )

    result = run_user_confirmed_demo_validation(
        config,
        values={},
        confirm_demo_write=True,
        store=SqliteRecordStore(tmp_path / "validation.sqlite3"),
    )

    assert result["status"] == "BLOCKED"
    assert "AUTHORIZED_CAPITAL_MUST_EQUAL_EUR_2000" in result["blockers"]
    assert result["demo_submission_attempts"] == 0
