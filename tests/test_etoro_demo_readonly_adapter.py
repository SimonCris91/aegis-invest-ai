import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import DEMO_PORTFOLIO_PATH, EtoroReadClient
from app.brokers.etoro.demo_readonly import (
    EtoroDemoReadOnlyAdapter,
    EtoroDemoReadOnlyError,
    EtoroDemoReadStatus,
    normalize_demo_portfolio_payload,
)
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse
from app.domain.enums import Currency, OperatingMode, SettlementType, TradeSide


class RecordingTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str]] = []

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse:
        self.requests.append((method, url))
        assert body is None
        return self.response


def test_demo_readonly_adapter_normalizes_successful_portfolio() -> None:
    payload = {
        "timestamp": "2026-08-31T10:00:00Z",
        "clientPortfolio": {
            "currency": "USD",
            "credit": "1234.50",
            "buyingPower": "1200.00",
            "equity": "1300.75",
            "positions": [
                {
                    "positionID": "pos-1",
                    "instrumentID": 1001,
                    "symbol": "AAPL",
                    "units": "2.5",
                    "openRate": "180.10",
                    "currentRate": "182.00",
                    "currentValue": "455.00",
                    "unrealizedPnl": "4.75",
                    "leverage": "1",
                    "isBuy": True,
                    "settlementType": "real",
                }
            ],
        },
    }
    transport = RecordingTransport(HttpResponse(200, {}, json.dumps(payload).encode()))
    client = EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(transport, max_read_attempts=1),
    )

    result = EtoroDemoReadOnlyAdapter(client, environment=OperatingMode.ETORO_DEMO).read_portfolio()

    assert result.status is EtoroDemoReadStatus.OK
    assert result.http_status == 200
    assert result.write_request_sent is False
    assert result.broker_write_calls == 0
    assert transport.requests == [("GET", f"https://public-api.etoro.com{DEMO_PORTFOLIO_PATH}")]
    assert result.portfolio is not None
    assert result.portfolio.environment is OperatingMode.ETORO_DEMO
    assert result.portfolio.currency is Currency.USD
    assert result.portfolio.available_cash == Decimal("1234.50")
    assert result.portfolio.buying_power == Decimal("1200.00")
    assert result.portfolio.equity == Decimal("1300.75")
    position = result.portfolio.open_positions[0]
    assert position.instrument_id == 1001
    assert position.symbol == "AAPL"
    assert position.units == Decimal("2.5")
    assert position.average_open_price == Decimal("180.10")
    assert position.current_price == Decimal("182.00")
    assert position.current_value == Decimal("455.00")
    assert position.unrealized_pnl == Decimal("4.75")
    assert position.side is TradeSide.BUY
    assert position.settlement_type is SettlementType.REAL


def test_demo_readonly_adapter_preserves_missing_optional_fields() -> None:
    portfolio = normalize_demo_portfolio_payload(
        {
            "timestamp": "2026-08-31T10:00:00+00:00",
            "clientPortfolio": {"positions": [{"instrumentID": 1001}]},
        },
        environment=OperatingMode.ETORO_DEMO,
    )

    assert portfolio.currency is None
    assert portfolio.available_cash is None
    assert portfolio.equity is None
    assert portfolio.open_positions[0].symbol is None
    assert portfolio.open_positions[0].units is None


def test_demo_readonly_adapter_allows_empty_demo_portfolio() -> None:
    portfolio = normalize_demo_portfolio_payload(
        {
            "timestamp": "2026-08-31T10:00:00+00:00",
            "clientPortfolio": {"currency": "USD", "credit": "50", "positions": []},
        },
        environment=OperatingMode.ETORO_DEMO,
    )

    assert portfolio.available_cash == Decimal("50")
    assert portfolio.open_positions == ()
    assert portfolio.broker_write_calls == 0


def test_demo_readonly_adapter_reports_auth_failure_safely() -> None:
    transport = RecordingTransport(HttpResponse(403, {"content-type": "application/json"}, b"{}"))
    client = EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(transport, max_read_attempts=1),
    )

    result = EtoroDemoReadOnlyAdapter(client, environment=OperatingMode.ETORO_DEMO).read_portfolio()

    assert result.status is EtoroDemoReadStatus.AUTH_FAILED
    assert result.http_status == 403
    assert result.write_request_sent is False
    assert result.broker_write_calls == 0
    assert transport.requests[0][0] == "GET"
    assert "api-secret" not in json.dumps(result.model_dump(mode="json"))
    assert "user-secret" not in json.dumps(result.model_dump(mode="json"))


def test_demo_readonly_adapter_reports_malformed_response() -> None:
    transport = RecordingTransport(HttpResponse(200, {}, b"[]"))
    client = EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(transport, max_read_attempts=1),
    )

    result = EtoroDemoReadOnlyAdapter(client, environment=OperatingMode.ETORO_DEMO).read_portfolio()

    assert result.status is EtoroDemoReadStatus.MALFORMED_RESPONSE
    assert result.write_request_sent is False
    assert result.broker_write_calls == 0


def test_demo_readonly_adapter_rejects_real_environment() -> None:
    client = EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(RecordingTransport(HttpResponse(200, {}, b"{}"))),
    )

    with pytest.raises(EtoroDemoReadOnlyError):
        EtoroDemoReadOnlyAdapter(client, environment=OperatingMode.ETORO_REAL_READ_ONLY)


def test_demo_readonly_adapter_treats_naive_timestamp_as_utc() -> None:
    portfolio = normalize_demo_portfolio_payload(
        {"timestamp": "2026-08-31T10:00:00", "clientPortfolio": {"positions": []}},
        environment=OperatingMode.ETORO_DEMO,
    )

    assert portfolio.as_of == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
