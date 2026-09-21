"""Step 6 live-validation foundation tests with offline deterministic transports."""

import inspect
import json
import logging
import socket
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Self, cast
from urllib.error import HTTPError, URLError
from uuid import UUID

import pytest

from app.agent.models import AegisAgentResult, AegisAnalysis
from app.agent.service import DeterministicAegisAgent
from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import (
    BASE,
    DEMO_AGGREGATE_PATH,
    DEMO_ELIGIBILITY_PATH,
    DEMO_INSTRUMENT_BREAKDOWN_PATH,
    EtoroApiError,
    EtoroReadClient,
)
from app.brokers.etoro.demo import DemoExecutionError, EtoroDemoAdapter
from app.brokers.etoro.http import (
    ETORO_USER_AGENT,
    DisciplinedHttpClient,
    EtoroHttpFailureKind,
    HttpResponse,
    TransportError,
    TransportFailureDetail,
    UrllibTransport,
    classify_http_failure,
    classify_transport_failure,
)
from app.brokers.etoro.mapping import (
    EtoroMappingError,
    map_demo_eligibility,
    map_demo_order_state,
    map_demo_portfolio,
    map_portfolio,
    map_quote,
)
from app.brokers.etoro.network import (
    NetworkPolicyDiagnostic,
    WindowsProxyDiagnostic,
    classify_network_policy,
    proxy_environment_report,
    transport_library_proxy_report,
    windows_winhttp_proxy_report,
)
from app.brokers.etoro.runtime import etoro_status, etoro_transport_check, runtime_credentials
from app.brokers.fake import FakeBroker
from app.brokers.identity import AccountIdentityError, AccountIdentityGuard
from app.brokers.market_validation import (
    MarketObservationError,
    validate_market_observation,
)
from app.brokers.models import (
    AccountKind,
    BrokerAccountContext,
    BrokerCapabilities,
    BrokerIdentity,
    BrokerSubmission,
    DemoEligibility,
    DemoPortfolioSnapshot,
    ExecutionState,
    FxRate,
    PerformanceSnapshot,
    PreflightDecision,
    TrackRecordKind,
)
from app.brokers.preflight import evaluate_demo_preflight
from app.brokers.reconciliation import ReconciliationError, reconcile_demo_state
from app.config.models import AegisStrategyConfig, ApplicationConfig, RiskPolicyConfig
from app.domain.enums import (
    Currency,
    EtoroTransportMode,
    MarketStatus,
    OperatingMode,
    RecommendedAction,
    SettlementType,
)
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.execution.gate import AuthorizedTrade, RiskEnforcedExecutionGate
from app.main.__main__ import main
from app.orchestration.shadow import ShadowRecorder, ShadowService
from app.performance.metrics import (
    NOT_ENOUGH_DATA,
    annualized_volatility,
    operational_counts,
    periodic_returns,
    sharpe_ratio,
    sortino_ratio,
)
from app.reporting.logging import JsonFormatter, configure_structured_logging
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SecretPersistenceError, SqliteRecordStore
from tests.conftest import TEST_INSTRUMENT_ID


class SequencedTransport:
    def __init__(self, items: list[HttpResponse | BaseException]) -> None:
        self.items = items
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class UrlopenResponse:
    status = 200

    def __init__(self, body: bytes = b"{}", headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class StaticNewsProvider:
    def __init__(self, news: tuple[NewsItem, ...]) -> None:
        self._news = news

    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]:
        return self._news


class HoldAgent:
    def analyze(self, context: object) -> AegisAgentResult:
        now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        return AegisAgentResult(
            analysis=AegisAnalysis(
                timestamp=now,
                market_assessment="hold",
                portfolio_assessment="hold",
                opportunity_summary="none",
                risk_summary="none",
                confidence=Decimal("0"),
                supporting_factors=(),
                risk_factors=("test",),
                recommended_action=RecommendedAction.HOLD,
                rationale="test hold",
            )
        )


class ActivatingStore(SqliteRecordStore):
    def __init__(self, path: Path, switch: KillSwitch) -> None:
        super().__init__(path)
        self._switch = switch

    def reserve_demo_submission(self, idempotency_key: str, payload: Mapping[str, object]) -> bool:
        reserved = super().reserve_demo_submission(idempotency_key, payload)
        self._switch.activate("activated before final HTTP check")
        return reserved


def _bytes(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _identity_payload() -> dict[str, object]:
    return {
        "gcid": "stable-user-0001",
        "demoCid": 222,
        "realCid": 111,
        "username": "aegis-user",
        "scopes": ["read"],
    }


def _portfolio_payload() -> dict[str, object]:
    return {
        "clientPortfolio": {
            "credit": "180",
            "positions": [
                {
                    "positionID": "p1",
                    "instrumentID": TEST_INSTRUMENT_ID,
                    "units": "1",
                    "openRate": "10",
                    "leverage": 1,
                    "isBuy": True,
                },
                {
                    "positionID": "ignored-cfd",
                    "instrumentID": TEST_INSTRUMENT_ID,
                    "units": "1",
                    "openRate": "10",
                    "leverage": 2,
                    "isBuy": True,
                },
            ],
        }
    }


def _demo_payload(now: datetime) -> dict[str, object]:
    return {
        "cid": 222,
        "timestamp": now.isoformat(),
        "accountCurrency": "USD",
        "accountTotals": {
            "accountAvailableCash": "1000",
            "accountCurrentPnl": "25",
            "accountTotalValue": "1200",
            "accountBalance": "1175",
        },
        "instrumentAggregates": [
            {
                "instrumentId": TEST_INSTRUMENT_ID,
                "assetCurrency": "USD",
                "pnlAssetCurrency": "25",
                "netUnits": "2",
                "netCurrentExposureAccountCurrency": "200",
                "netInitialExposureAccountCurrency": "175",
                "accountCurrencyReturn": "25",
                "avgLeverage": "1",
                "avgOpenRate": "87.5",
            }
        ],
    }


def _quote_payload(now: datetime) -> dict[str, object]:
    return {
        "rates": [
            {
                "instrumentID": TEST_INSTRUMENT_ID,
                "lastExecution": "10",
                "bid": "9.99",
                "ask": "10.01",
                "date": now.isoformat(),
            }
        ]
    }


def _eligibility_payload() -> dict[str, object]:
    return {
        "currency": "USD",
        "notFoundInstrumentIds": [],
        "eligibilities": [
            {
                "instrumentId": TEST_INSTRUMENT_ID,
                "allowOpenPosition": True,
                "minPositionExposure": "1",
                "leverageConfigs": [
                    {
                        "settlementType": "real",
                        "direction": "long",
                        "leverageValues": [1],
                        "minPositionAmount": "5",
                    }
                ],
            }
        ],
    }


def _order_state_payload(status: str) -> dict[str, object]:
    return {
        "instruments": [
            {
                "instrumentId": TEST_INSTRUMENT_ID,
                "orders": [{"orderId": "order-1", "status": status}],
            }
        ]
    }


def _identity() -> BrokerIdentity:
    return BrokerIdentity(
        stable_user_id="stable-user-0001", demo_account_id=222, real_account_id=111
    )


def _demo_snapshot(now: datetime, *, cash: Decimal = Decimal("100")) -> DemoPortfolioSnapshot:
    return DemoPortfolioSnapshot(
        context=BrokerAccountContext(
            stable_user_id="stable-user-0001", account_id=222, kind=AccountKind.DEMO
        ),
        as_of=now,
        currency=Currency.USD,
        cash=cash,
        total_value=Decimal("200"),
    )


def _eligibility() -> DemoEligibility:
    return DemoEligibility(
        instrument_id=TEST_INSTRUMENT_ID,
        symbol="TEST",
        currency=Currency.USD,
        minimum_position=Decimal("1"),
        allow_open=True,
        settlement_type=SettlementType.REAL,
        leverage=1,
        verified=True,
    )


def _submission(
    now: datetime, state: ExecutionState = ExecutionState.SUBMITTED
) -> BrokerSubmission:
    return BrokerSubmission(
        idempotency_key="idempotent-1",
        request_id="request-1",
        state=state,
        broker_order_id="order-1",
        proposal_id="proposal-1",
        authorization_id="authorization-1",
        submitted_at=now,
    )


def _admitted_trade(
    proposal: TradeProposal, context: RiskContext, now: datetime
) -> tuple[KillSwitch, RiskEnforcedExecutionGate, AuthorizedTrade]:
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    risk_manager = RiskManager(
        RiskPolicyConfig(),
        switch,
        authorization_key=b"test-risk-authorization-key-32b!",
        clock=lambda: now,
    )
    evaluation = risk_manager.evaluate(proposal, context)
    assert evaluation.authorization is not None
    gate = RiskEnforcedExecutionGate(risk_manager)
    return switch, gate, gate.admit(proposal, evaluation.authorization, at=now)


def _demo_adapter(
    *,
    transport: SequencedTransport,
    gate: RiskEnforcedExecutionGate,
    switch: KillSwitch,
    store: SqliteRecordStore,
    now: datetime,
    enabled: bool = True,
    opt_in: bool = True,
) -> EtoroDemoAdapter:
    return EtoroDemoAdapter(
        credentials=EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        http=DisciplinedHttpClient(transport),
        gate=gate,
        kill_switch=switch,
        registry=store,
        enabled=enabled,
        explicit_opt_in=opt_in,
        clock=lambda: now,
    )


def test_urllib_transport_normalizes_success_http_error_and_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ok(request: object, timeout: int) -> UrlopenResponse:
        return UrlopenResponse(b'{"ok":true}', {"X-Test": "yes"})

    monkeypatch.setattr("app.brokers.etoro.http.urlopen", ok)
    response = UrllibTransport().request("GET", "https://example.invalid", {})
    assert response.status == 200
    assert response.headers["X-Test"] == "yes"
    assert response.body == b'{"ok":true}'

    def http_error(request: object, timeout: int) -> UrlopenResponse:
        raise HTTPError(
            "https://example.invalid",
            403,
            "Forbidden",
            cast(Any, {"X-Test": "no"}),
            BytesIO(b"no"),
        )

    monkeypatch.setattr("app.brokers.etoro.http.urlopen", http_error)
    rejected = UrllibTransport().request("GET", "https://example.invalid", {})
    assert rejected.status == 403
    assert rejected.body == b"no"

    def url_error(request: object, timeout: int) -> UrlopenResponse:
        raise URLError("dns unavailable")

    monkeypatch.setattr("app.brokers.etoro.http.urlopen", url_error)
    with pytest.raises(TransportError):
        UrllibTransport().request("GET", "https://example.invalid", {})


def test_urllib_direct_mode_uses_empty_proxy_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeOpener:
        def open(self, request: object, timeout: int) -> UrlopenResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return UrlopenResponse(b'{"ok":true}')

    def fake_proxy_handler(proxies: dict[str, str]) -> object:
        captured["proxies"] = proxies
        return ("proxy-handler", proxies)

    def fake_build_opener(handler: object) -> FakeOpener:
        captured["handler"] = handler
        return FakeOpener()

    monkeypatch.setattr("app.brokers.etoro.http.ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr("app.brokers.etoro.http.build_opener", fake_build_opener)

    response = UrllibTransport(EtoroTransportMode.DIRECT).request(
        "GET", "https://example.invalid", {}
    )

    assert response.status == 200
    assert captured["proxies"] == {}
    assert captured["timeout"] == 20


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422])
def test_http_read_does_not_retry_non_retryable_statuses(status: int) -> None:
    transport = SequencedTransport([HttpResponse(status, {}, b"{}")])
    response = DisciplinedHttpClient(transport).get("https://example.invalid", {})
    assert response.status == status
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_read_and_read_lookup_retry_bounded_transient_failures(status: int) -> None:
    sleeps: list[float] = []
    transport = SequencedTransport(
        [
            HttpResponse(status, {"Retry-After": "1"}, b"{}"),
            HttpResponse(status, {"Retry-After": "0"}, b"{}"),
            HttpResponse(200, {}, b"{}"),
        ]
    )
    client = DisciplinedHttpClient(transport, sleeper=sleeps.append)
    assert client.get("https://example.invalid/read", {}).status == 200
    assert sleeps == [1.0, 0.0]

    lookup_transport = SequencedTransport(
        [HttpResponse(status, {}, b"{}"), HttpResponse(200, {}, b"{}")]
    )
    lookup_client = DisciplinedHttpClient(lookup_transport, sleeper=sleeps.append)
    assert lookup_client.post_read("https://example.invalid/lookup", {}, {"a": 1}).status == 200
    method, _url, headers, body = lookup_transport.calls[-1]
    assert method == "POST"
    assert headers["Content-Type"] == "application/json"
    assert body == b'{"a":1}'


def test_http_client_configuration_and_json_decoding() -> None:
    with pytest.raises(ValueError):
        DisciplinedHttpClient(SequencedTransport([]), max_read_attempts=0)
    assert HttpResponse(200, {}, b'{"ok": true}').json() == {"ok": True}


def test_etoro_http_client_adds_stable_user_agent_to_all_etoro_methods() -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, b"{}"),
            HttpResponse(200, {}, b"{}"),
            HttpResponse(202, {}, b"{}"),
        ]
    )
    client = DisciplinedHttpClient(transport, max_read_attempts=1)
    headers = {
        "User-Agent": "DoNotUse/1.0",
        "x-api-key": "api-secret",
        "x-request-id": "request-1",
        "x-user-key": "user-secret",
    }

    client.get(BASE + "/api/v1/me", headers)
    client.post_read(BASE + DEMO_ELIGIBILITY_PATH, headers, {"instrumentIds": [1]})
    client.post_once(BASE + "/api/v3/trading/execution/demo/orders", headers, {"amount": 1})

    for _method, _url, request_headers, _body in transport.calls:
        user_agent_keys = [key for key in request_headers if key.casefold() == "user-agent"]
        assert user_agent_keys == ["User-Agent"]
        assert request_headers["User-Agent"] == ETORO_USER_AGENT
        assert request_headers["x-api-key"] == "api-secret"
        assert request_headers["x-user-key"] == "user-secret"


def test_http_failure_classification_distinguishes_edge_waf_auth_and_http() -> None:
    assert (
        classify_http_failure(HttpResponse(403, {}, b"error code: 1010"))
        is EtoroHttpFailureKind.EDGE_WAF_BLOCK
    )
    assert (
        classify_http_failure(HttpResponse(403, {}, b'{"error":"forbidden"}'))
        is EtoroHttpFailureKind.AUTH_API_PERMISSION_ERROR
    )
    assert (
        classify_http_failure(HttpResponse(401, {}, b'{"error":"unauthorized"}'))
        is EtoroHttpFailureKind.AUTH_API_PERMISSION_ERROR
    )
    assert (
        classify_http_failure(HttpResponse(500, {}, b'{"error":"server"}'))
        is EtoroHttpFailureKind.HTTP_ERROR
    )


@pytest.mark.parametrize(
    ("exc", "detail"),
    [
        (URLError(socket.gaierror("getaddrinfo failed")), TransportFailureDetail.DNS),
        (URLError(ssl.SSLError("certificate verify failed")), TransportFailureDetail.TLS),
        (URLError(TimeoutError("timed out")), TransportFailureDetail.TIMEOUT),
        (
            URLError(ConnectionResetError("connection reset by peer")),
            TransportFailureDetail.CONNECTION_RESET,
        ),
        (
            URLError(PermissionError("[WinError 10013] access forbidden")),
            TransportFailureDetail.PROXY_NETWORK_POLICY,
        ),
        (URLError(OSError("connect failed")), TransportFailureDetail.SOCKET_CONNECT),
        (URLError(RuntimeError("unexpected")), TransportFailureDetail.OTHER_TRANSPORT),
    ],
)
def test_transport_failure_classification_is_sanitized(
    exc: URLError, detail: TransportFailureDetail
) -> None:
    assert classify_transport_failure(exc) is detail


def test_proxy_diagnostics_redact_credentials_and_classify_sources() -> None:
    proxy_env = proxy_environment_report(
        {
            "HTTP_PROXY": "http://proxy-user:proxy-pass@env.proxy.local:8080/path",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "localhost,127.0.0.1,internal.example",
        }
    )
    http_proxy = next(entry for entry in proxy_env if entry.variable == "HTTP_PROXY")
    no_proxy = next(entry for entry in proxy_env if entry.variable == "NO_PROXY")

    assert http_proxy.configured
    assert http_proxy.sanitized_destination == "http://env.proxy.local:8080"
    assert "proxy-user" not in http_proxy.model_dump_json()
    assert "proxy-pass" not in http_proxy.model_dump_json()
    assert no_proxy.sanitized_destination == "localhost,127.0.0.1,internal.example"

    library = transport_library_proxy_report(
        {"https": "http://lib-user:lib-pass@library.proxy.local:3128"}
    )
    assert library[0].sanitized_destination == "http://library.proxy.local:3128"
    assert "lib-pass" not in library[0].model_dump_json()

    assert (
        classify_network_policy(
            TransportFailureDetail.PROXY_NETWORK_POLICY.value,
            proxy_environment=proxy_env,
            transport_library_proxies=(),
        )
        is NetworkPolicyDiagnostic.ENV_PROXY
    )
    assert (
        classify_network_policy(
            TransportFailureDetail.PROXY_NETWORK_POLICY.value,
            proxy_environment=proxy_environment_report({}),
            transport_library_proxies=library,
        )
        is NetworkPolicyDiagnostic.TRANSPORT_LIBRARY_PROXY
    )
    assert (
        classify_network_policy(
            TransportFailureDetail.PROXY_NETWORK_POLICY.value,
            proxy_environment=proxy_environment_report({}),
            transport_library_proxies=(),
            system_proxy=WindowsProxyDiagnostic(available=True, configured=True),
        )
        is NetworkPolicyDiagnostic.SYSTEM_PROXY
    )


def test_network_policy_classification_covers_dns_tls_socket_and_sandbox() -> None:
    empty_env = proxy_environment_report({})
    assert (
        classify_network_policy(
            TransportFailureDetail.DNS.value,
            proxy_environment=empty_env,
            transport_library_proxies=(),
        )
        is NetworkPolicyDiagnostic.DNS_POLICY
    )
    assert (
        classify_network_policy(
            TransportFailureDetail.TLS.value,
            proxy_environment=empty_env,
            transport_library_proxies=(),
        )
        is NetworkPolicyDiagnostic.TLS_POLICY
    )
    assert (
        classify_network_policy(
            TransportFailureDetail.PROXY_NETWORK_POLICY.value,
            proxy_environment=empty_env,
            transport_library_proxies=(),
            codex_sandbox_network_restricted=True,
        )
        is NetworkPolicyDiagnostic.CODEX_SANDBOX_PROXY
    )
    assert (
        classify_network_policy(
            TransportFailureDetail.SOCKET_CONNECT.value,
            proxy_environment=empty_env,
            transport_library_proxies=(),
        )
        is NetworkPolicyDiagnostic.SOCKET_POLICY
    )
    assert (
        classify_network_policy(None, proxy_environment=empty_env, transport_library_proxies=())
        is NetworkPolicyDiagnostic.UNKNOWN_NETWORK_POLICY
    )


def test_windows_proxy_report_parser_is_sanitized() -> None:
    direct = windows_winhttp_proxy_report(
        "Current WinHTTP proxy settings:\n\n    Direct access (no proxy server).\n"
    )
    assert direct.available
    assert direct.configured is False

    configured = windows_winhttp_proxy_report(
        "Current WinHTTP proxy settings:\n\n"
        "    Proxy Server(s) : http=user:secret@system.proxy.local:8080\n"
    )
    assert configured.available
    assert configured.configured is True
    assert configured.sanitized_destination == "http://system.proxy.local:8080"
    assert "secret" not in configured.model_dump_json()


def test_etoro_read_client_normalizes_identity_portfolios_quote_eligibility_and_status(
    now: datetime,
) -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes(_identity_payload())),
            HttpResponse(200, {}, _bytes(_portfolio_payload())),
            HttpResponse(200, {}, _bytes(_quote_payload(now))),
            HttpResponse(200, {}, _bytes(_demo_payload(now))),
            HttpResponse(200, {}, _bytes(_eligibility_payload())),
            HttpResponse(200, {}, _bytes(_order_state_payload("pending"))),
        ]
    )
    client = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"), DisciplinedHttpClient(transport)
    )

    identity = client.identity()
    real = client.real_portfolio_read_only({TEST_INSTRUMENT_ID: "TEST"})
    quote = client.quote(TEST_INSTRUMENT_ID, "TEST")
    demo = client.demo_account(identity)
    eligibility = client.demo_eligibility(TEST_INSTRUMENT_ID, "TEST")
    state = client.demo_order_state(identity, TEST_INSTRUMENT_ID, "order-1")

    assert identity.redacted_reference == "user:0001"
    assert real.cash == Decimal("180")
    assert len(real.positions) == 1
    assert quote.bid == Decimal("9.99")
    assert demo.context.kind is AccountKind.DEMO
    assert eligibility.minimum_position == Decimal("5")
    assert state is ExecutionState.PENDING
    assert transport.calls[4][0] == "POST"
    assert transport.calls[4][1] == BASE + DEMO_ELIGIBILITY_PATH
    assert transport.calls[5][1].startswith(BASE + DEMO_INSTRUMENT_BREAKDOWN_PATH)
    assert transport.calls[5][2]["CID"] == "222"
    request_ids = [call[2]["x-request-id"] for call in transport.calls]
    assert len(set(request_ids)) == len(request_ids)
    for request_id in request_ids:
        UUID(request_id)
    assert all(call[2]["User-Agent"] == ETORO_USER_AGENT for call in transport.calls)


def test_etoro_read_client_fails_closed_on_bad_status_json_and_schema(now: datetime) -> None:
    rejected = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"),
        DisciplinedHttpClient(SequencedTransport([HttpResponse(401, {}, b"{}")])),
    )
    with pytest.raises(EtoroApiError):
        rejected.identity()

    invalid_json = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"),
        DisciplinedHttpClient(SequencedTransport([HttpResponse(200, {}, b"{")])),
    )
    with pytest.raises(EtoroApiError):
        invalid_json.identity()

    bad_status = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"),
        DisciplinedHttpClient(
            SequencedTransport([HttpResponse(200, {}, _bytes({"no": "orders"}))])
        ),
    )
    with pytest.raises(EtoroApiError):
        bad_status.demo_order_state(_identity(), TEST_INSTRUMENT_ID, "order-1")

    bad_lookup = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"),
        DisciplinedHttpClient(
            SequencedTransport([HttpResponse(500, {}, b"{}")]),
            max_read_attempts=1,
        ),
    )
    with pytest.raises(EtoroApiError):
        bad_lookup.demo_eligibility(TEST_INSTRUMENT_ID, "TEST")


def test_etoro_read_client_uses_demo_position_when_filled_order_row_is_absent(
    now: datetime,
) -> None:
    transport = SequencedTransport(
        [
            HttpResponse(
                200,
                {},
                _bytes({"instruments": [{"instrumentId": TEST_INSTRUMENT_ID, "orders": []}]}),
            ),
            HttpResponse(200, {}, _bytes(_demo_payload(now))),
        ]
    )
    client = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"), DisciplinedHttpClient(transport)
    )

    state = client.demo_order_state(_identity(), TEST_INSTRUMENT_ID, "order-1")

    assert state is ExecutionState.FILLED
    assert transport.calls[1][1] == BASE + DEMO_AGGREGATE_PATH


def test_mapping_failures_and_demo_state_semantics(now: datetime) -> None:
    identity = _identity()
    with pytest.raises(EtoroMappingError):
        map_demo_portfolio({**_demo_payload(now), "cid": 999}, identity)
    with pytest.raises(EtoroMappingError):
        map_demo_eligibility(
            {"currency": "USD", "notFoundInstrumentIds": [TEST_INSTRUMENT_ID]},
            TEST_INSTRUMENT_ID,
            "TEST",
        )
    with pytest.raises(EtoroMappingError):
        map_demo_eligibility(
            {
                "currency": "USD",
                "notFoundInstrumentIds": [],
                "eligibilities": [{"instrumentId": TEST_INSTRUMENT_ID, "leverageConfigs": []}],
            },
            TEST_INSTRUMENT_ID,
            "TEST",
        )
    with pytest.raises(EtoroMappingError):
        map_quote(
            {"rates": []}, instrument_id=TEST_INSTRUMENT_ID, symbol="TEST", currency=Currency.USD
        )
    with pytest.raises(EtoroMappingError):
        map_portfolio(
            {"clientPortfolio": {"credit": "1", "positions": [{"instrumentID": 7}]}},
            as_of=now,
            currency=Currency.USD,
            symbols={},
        )

    assert (
        map_demo_order_state(
            _order_state_payload("partially_filled"), TEST_INSTRUMENT_ID, "order-1"
        )
        is ExecutionState.PARTIALLY_FILLED
    )
    assert (
        map_demo_order_state(_order_state_payload("mystery"), TEST_INSTRUMENT_ID, "order-1")
        is ExecutionState.UNKNOWN
    )


def test_market_observation_validation_rejects_stale_future_mismatch_and_missing_fields(
    market_quote: MarketQuote, instrument: InstrumentMetadata, now: datetime
) -> None:
    validate_market_observation(market_quote, instrument, now=now, maximum_age_seconds=300)
    invalid_cases = (
        market_quote.model_copy(update={"instrument_id": TEST_INSTRUMENT_ID + 1}),
        market_quote.model_copy(update={"as_of": now - timedelta(seconds=301)}),
        market_quote.model_copy(update={"as_of": now + timedelta(seconds=1)}),
        market_quote.model_copy(update={"bid": None}),
    )
    for quote in invalid_cases:
        with pytest.raises(MarketObservationError):
            validate_market_observation(quote, instrument, now=now, maximum_age_seconds=300)
    with pytest.raises(MarketObservationError):
        validate_market_observation(
            market_quote,
            instrument.model_copy(update={"is_valid": False}),
            now=now,
            maximum_age_seconds=300,
        )


def test_demo_preflight_allows_only_complete_verified_feasibility(
    proposal: TradeProposal,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    now: datetime,
) -> None:
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    allowed = evaluate_demo_preflight(
        proposal=proposal,
        portfolio=_demo_snapshot(now),
        eligibility=_eligibility(),
        quote=market_quote,
        instrument=instrument,
        kill_switch=switch,
        now=now,
        maximum_age_seconds=300,
    )
    assert allowed.allowed
    assert allowed.minimum_trade_amount == Decimal("1")

    blocked = evaluate_demo_preflight(
        proposal=proposal,
        portfolio=_demo_snapshot(now, cash=Decimal("5")),
        eligibility=_eligibility().model_copy(update={"allow_open": False}),
        quote=market_quote.model_copy(update={"market_status": MarketStatus.CLOSED}),
        instrument=instrument,
        kill_switch=switch,
        now=now,
        maximum_age_seconds=300,
    )
    assert not blocked.allowed
    assert "market is not verified open" in blocked.reasons
    assert "Demo buying power is insufficient" in blocked.reasons

    switch.activate("test")
    killed = evaluate_demo_preflight(
        proposal=proposal,
        portfolio=_demo_snapshot(now),
        eligibility=_eligibility(),
        quote=market_quote,
        instrument=instrument,
        kill_switch=switch,
        now=now,
        maximum_age_seconds=300,
    )
    assert "kill switch is active" in killed.reasons


def test_demo_preflight_fx_fails_closed_unless_rate_is_fresh_and_matching(
    proposal: TradeProposal,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    now: datetime,
) -> None:
    eur_proposal = proposal.model_copy(update={"currency": Currency.EUR})
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    missing = evaluate_demo_preflight(
        proposal=eur_proposal,
        portfolio=_demo_snapshot(now),
        eligibility=_eligibility(),
        quote=market_quote,
        instrument=instrument,
        kill_switch=switch,
        now=now,
        maximum_age_seconds=300,
    )
    assert not missing.allowed
    assert "fresh verified FX conversion is unavailable" in missing.reasons

    stale_fx = FxRate(
        base_currency=Currency.EUR,
        quote_currency=Currency.USD,
        rate=Decimal("1.10"),
        as_of=now - timedelta(seconds=301),
        source="fixture",
    )
    stale = evaluate_demo_preflight(
        proposal=eur_proposal,
        portfolio=_demo_snapshot(now),
        eligibility=_eligibility(),
        quote=market_quote,
        instrument=instrument,
        kill_switch=switch,
        now=now,
        maximum_age_seconds=300,
        fx_rate=stale_fx,
    )
    assert not stale.allowed

    fresh_fx = stale_fx.model_copy(update={"as_of": now})
    converted = evaluate_demo_preflight(
        proposal=eur_proposal,
        portfolio=_demo_snapshot(now),
        eligibility=_eligibility(),
        quote=market_quote,
        instrument=instrument,
        kill_switch=switch,
        now=now,
        maximum_age_seconds=300,
        fx_rate=fresh_fx,
    )
    assert converted.allowed


def test_identity_guard_blocks_demo_real_or_user_mismatch(now: datetime) -> None:
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)
    identity = _identity()
    guard = AccountIdentityGuard(identity, AccountKind.DEMO, switch)
    guard.verify(identity, guard.context)

    with pytest.raises(AccountIdentityError):
        guard.verify(identity.model_copy(update={"stable_user_id": "changed"}), guard.context)
    assert switch.state.active

    switch.deactivate("test reset")
    with pytest.raises(AccountIdentityError):
        guard.verify(
            identity,
            BrokerAccountContext(
                stable_user_id=identity.stable_user_id,
                account_id=identity.real_account_id,
                kind=AccountKind.REAL,
            ),
        )
    assert switch.state.active


def test_fake_broker_portability_and_preflight_rejection(
    portfolio: PortfolioSnapshot,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    now: datetime,
) -> None:
    broker = FakeBroker(
        capabilities=BrokerCapabilities(
            provider="fake",
            mode=OperatingMode.OFFLINE_PAPER,
            authenticated_reads=False,
            demo_execution=True,
        ),
        identity=_identity(),
        portfolio=portfolio,
        quote=market_quote,
        instrument=instrument,
        submission=_submission(now),
    )
    assert broker.identity().demo_account_id == 222
    assert broker.portfolio() == portfolio
    assert broker.quote(TEST_INSTRUMENT_ID, "TEST") == market_quote
    assert broker.instrument(TEST_INSTRUMENT_ID, "TEST") == instrument
    with pytest.raises(PermissionError):
        broker.submit_demo(
            object(),  # type: ignore[arg-type]
            PreflightDecision(allowed=False, reasons=("blocked",)),
        )


def test_demo_adapter_blocks_disabled_missing_opt_in_bad_preflight_and_kill_switch(
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
    tmp_path: Path,
) -> None:
    switch, gate, trade = _admitted_trade(proposal, context, now)
    transport = SequencedTransport([])
    store = SqliteRecordStore(tmp_path / "demo.sqlite3")

    with pytest.raises(DemoExecutionError):
        _demo_adapter(
            transport=transport, gate=gate, switch=switch, store=store, now=now, enabled=False
        ).submit_demo(trade, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")))
    with pytest.raises(DemoExecutionError):
        _demo_adapter(
            transport=transport, gate=gate, switch=switch, store=store, now=now, opt_in=False
        ).submit_demo(trade, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")))
    with pytest.raises(DemoExecutionError):
        _demo_adapter(
            transport=transport, gate=gate, switch=switch, store=store, now=now
        ).submit_demo(trade, PreflightDecision(allowed=False, reasons=("blocked",)))
    switch.activate("manual")
    with pytest.raises(DemoExecutionError):
        _demo_adapter(
            transport=transport, gate=gate, switch=switch, store=store, now=now
        ).submit_demo(trade, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")))
    assert transport.calls == []


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (HttpResponse(422, {}, b'{"error":"blocked"}'), ExecutionState.REJECTED),
        (HttpResponse(202, {}, b"{}"), ExecutionState.UNKNOWN),
        (HttpResponse(202, {}, b"{"), ExecutionState.UNKNOWN),
    ],
)
def test_demo_adapter_maps_rejected_and_ambiguous_write_results(
    response: HttpResponse,
    expected: ExecutionState,
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
    tmp_path: Path,
) -> None:
    switch, gate, trade = _admitted_trade(proposal, context, now)
    store = SqliteRecordStore(tmp_path / "demo.sqlite3")
    transport = SequencedTransport([response])
    result = _demo_adapter(
        transport=transport, gate=gate, switch=switch, store=store, now=now
    ).submit_demo(trade, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")))
    assert result.state is expected
    assert store.demo_submission(proposal.idempotency_key) is not None
    if expected is ExecutionState.UNKNOWN:
        assert switch.state.active


def test_demo_adapter_ambiguous_transport_activates_kill_switch_and_blocks_replay(
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
    tmp_path: Path,
) -> None:
    switch, gate, trade = _admitted_trade(proposal, context, now)
    store = SqliteRecordStore(tmp_path / "demo.sqlite3")
    transport = SequencedTransport([TransportError("timeout")])
    result = _demo_adapter(
        transport=transport, gate=gate, switch=switch, store=store, now=now
    ).submit_demo(trade, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")))
    assert result.state is ExecutionState.UNKNOWN
    assert switch.state.active
    assert store.unresolved_demo_submissions()[0]["state"] == ExecutionState.UNKNOWN.value

    switch.deactivate("recovery check")
    with pytest.raises(DemoExecutionError):
        _demo_adapter(
            transport=transport, gate=gate, switch=switch, store=store, now=now
        ).submit_demo(trade, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")))
    assert len(transport.calls) == 1


def test_demo_adapter_final_kill_switch_check_runs_after_reservation(
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
    tmp_path: Path,
) -> None:
    switch, gate, trade = _admitted_trade(proposal, context, now)
    store = ActivatingStore(tmp_path / "demo.sqlite3", switch)
    transport = SequencedTransport([HttpResponse(202, {}, b'{"orderId":"order-1"}')])

    with pytest.raises(DemoExecutionError):
        _demo_adapter(
            transport=transport, gate=gate, switch=switch, store=store, now=now
        ).submit_demo(trade, PreflightDecision(allowed=True, minimum_trade_amount=Decimal("1")))

    assert transport.calls == []
    assert store.demo_submission(proposal.idempotency_key)["state"] == "RESERVED"  # type: ignore[index]


def test_reconciliation_updates_valid_states_and_halts_unknown_or_divergent_states(
    now: datetime, tmp_path: Path
) -> None:
    store = SqliteRecordStore(tmp_path / "records.sqlite3")
    local = _submission(now)
    assert store.reserve_demo_submission(local.idempotency_key, {"broker_order_id": "order-1"})
    switch = KillSwitch(active=False, reason="test", clock=lambda: now)

    assert (
        reconcile_demo_state(local, ExecutionState.PENDING, switch, store) is ExecutionState.PENDING
    )
    assert store.demo_submission(local.idempotency_key)["state"] == "PENDING"  # type: ignore[index]

    with pytest.raises(ReconciliationError):
        reconcile_demo_state(local, ExecutionState.UNKNOWN, switch, store)
    assert switch.state.active
    assert store.demo_submission(local.idempotency_key)["state"] == "UNKNOWN"  # type: ignore[index]

    switch.deactivate("test reset")
    divergent = _submission(now, ExecutionState.FILLED)
    assert store.reserve_demo_submission("divergent-key", {"broker_order_id": "order-1"})
    divergent = divergent.model_copy(update={"idempotency_key": "divergent-key"})
    with pytest.raises(ReconciliationError):
        reconcile_demo_state(divergent, ExecutionState.PENDING, switch, store)
    assert switch.state.active


def test_sqlite_track_records_idempotency_recovery_and_secret_rejection(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "records.sqlite3")
    shadow_id = store.append_track_record(TrackRecordKind.SHADOW, {"would_do": "HOLD"})
    demo_id = store.append_track_record(TrackRecordKind.ETORO_DEMO, {"demo_execution": True})
    real_id = store.append_track_record(TrackRecordKind.REAL_ACCOUNT_OBSERVATION, {"equity": "200"})
    assert (shadow_id, demo_id, real_id) == (1, 2, 3)
    assert store.list("track-record:ETORO_DEMO")[0]["funds_label"] == "SIMULATED FUNDS"
    assert store.list("track-record:REAL_ACCOUNT_OBSERVATION")[0]["aegis_generated_return"] is False

    assert store.reserve_demo_submission("idem-1", {"request_id": "request-1"})
    assert not store.reserve_demo_submission("idem-1", {"request_id": "request-2"})
    assert store.unresolved_demo_submissions()[0]["idempotency_key"] == "idem-1"
    store.update_demo_submission(
        "idem-1", ExecutionState.FILLED.value, {"broker_order_id": "order-1"}
    )
    assert store.unresolved_demo_submissions() == ()
    with pytest.raises(KeyError):
        store.update_demo_submission("missing", ExecutionState.UNKNOWN.value, {})
    with pytest.raises(SecretPersistenceError):
        store.reserve_demo_submission("bad", {"authorization": "secret"})


def test_shadow_mode_records_would_do_and_has_no_execution_dependency(
    portfolio: PortfolioSnapshot,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    now: datetime,
    tmp_path: Path,
) -> None:
    broker = FakeBroker(
        capabilities=BrokerCapabilities(
            provider="fake",
            mode=OperatingMode.SHADOW,
            authenticated_reads=True,
            demo_execution=True,
        ),
        identity=_identity(),
        portfolio=portfolio,
        quote=market_quote,
        instrument=instrument,
        submission=_submission(now),
    )
    store = SqliteRecordStore(tmp_path / "shadow.sqlite3")
    service = ShadowService(
        broker=broker,
        news=StaticNewsProvider((news_item,)),
        agent=DeterministicAegisAgent(),
        risk_manager=risk_manager,
        strategy=AegisStrategyConfig(),
        recorder=ShadowRecorder(store),
        clock=lambda: now,
        strategy_version="step6-test",
    )

    result = service.run("TEST", TEST_INSTRUMENT_ID)
    assert result["broker_write_calls"] == 0
    assert broker.demo_calls == 0
    assert result["risk_status"] == "APPROVED"
    persisted = store.list("track-record:SHADOW")[0]
    assert persisted["mode"] == "SHADOW"
    assert persisted["track_record_kind"] == "SHADOW"
    signature = inspect.signature(ShadowService.__init__)
    assert "execution" not in signature.parameters
    assert "demo" not in signature.parameters


def test_shadow_mode_can_hold_without_risk_decision(
    portfolio: PortfolioSnapshot,
    market_quote: MarketQuote,
    instrument: InstrumentMetadata,
    news_item: NewsItem,
    risk_manager: RiskManager,
    now: datetime,
    tmp_path: Path,
) -> None:
    broker = FakeBroker(
        capabilities=BrokerCapabilities(
            provider="fake",
            mode=OperatingMode.SHADOW,
            authenticated_reads=True,
            demo_execution=False,
        ),
        identity=_identity(),
        portfolio=portfolio,
        quote=market_quote,
        instrument=instrument,
        submission=_submission(now),
    )
    result = ShadowService(
        broker=broker,
        news=StaticNewsProvider((news_item,)),
        agent=HoldAgent(),
        risk_manager=risk_manager,
        strategy=AegisStrategyConfig(),
        recorder=ShadowRecorder(SqliteRecordStore(tmp_path / "shadow.sqlite3")),
        clock=lambda: now,
        strategy_version="step6-test",
    ).run("TEST", TEST_INSTRUMENT_ID)
    assert result["proposal"] is None
    assert result["risk_status"] == "NONE"


def test_runtime_status_is_safe_without_credentials_and_validates_reads_when_injected(
    now: datetime,
) -> None:
    assert runtime_credentials({"ETORO_API_KEY": "", "ETORO_USER_KEY": "user"}) is None
    assert runtime_credentials({"ETORO_API_KEY": "api", "ETORO_USER_KEY": "user"}) is not None

    disabled = etoro_status(ApplicationConfig(), values={})
    assert disabled.status == "NOT_CONFIGURED"
    assert not disabled.authentication_attempted
    assert not disabled.real_execution_available

    failed_client = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"),
        DisciplinedHttpClient(SequencedTransport([HttpResponse(401, {}, b"{}")])),
    )
    failed = etoro_status(
        ApplicationConfig(etoro_api_enabled=True),
        values={"ETORO_API_KEY": "api", "ETORO_USER_KEY": "user"},
        client=failed_client,
    )
    assert failed.status == "AUTH_API_PERMISSION_ERROR"

    ok_client = EtoroReadClient(
        EtoroCredentials(api_key="api", user_key="user"),
        DisciplinedHttpClient(
            SequencedTransport(
                [
                    HttpResponse(200, {}, _bytes(_identity_payload())),
                    HttpResponse(
                        200, {}, _bytes({"clientPortfolio": {"credit": "1", "positions": []}})
                    ),
                    HttpResponse(200, {}, _bytes(_demo_payload(now))),
                ]
            )
        ),
    )
    ok = etoro_status(
        ApplicationConfig(
            operating_mode=OperatingMode.ETORO_DEMO,
            etoro_api_enabled=True,
            etoro_demo_execution_enabled=True,
            demo_smoke_test_opt_in=True,
        ),
        values={"ETORO_API_KEY": "api", "ETORO_USER_KEY": "user"},
        client=ok_client,
    )
    assert ok.status == "READ_VALIDATED"
    assert ok.authentication_successful
    assert ok.demo_execution_available
    assert not ok.real_execution_available


def test_transport_check_reports_only_sanitized_read_status(now: datetime) -> None:
    credentials = {"ETORO_API_KEY": "api-secret", "ETORO_USER_KEY": "user-secret"}

    disabled = etoro_transport_check(
        ApplicationConfig(),
        values=credentials,
        proxy_values={},
        library_proxies={},
    )
    assert disabled["endpoint"] == "/api/v1/me"
    assert disabled["http_status"] is None
    assert disabled["status"] == "NOT_CONFIGURED"
    assert disabled["category"] == "API_DISABLED"
    assert disabled["transport_mode"] == "SYSTEM_PROXY"
    assert len(cast(tuple[object, ...], disabled["proxy_environment"])) == 8

    ok_client = EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(
            SequencedTransport(
                [
                    HttpResponse(
                        200,
                        {"Content-Type": "application/json", "CF-RAY": "ray-1"},
                        _bytes(_identity_payload()),
                    )
                ]
            )
        ),
    )
    verified = etoro_transport_check(
        ApplicationConfig(etoro_api_enabled=True),
        values=credentials,
        client=ok_client,
        proxy_values={},
        library_proxies={},
    )
    assert verified["endpoint"] == "/api/v1/me"
    assert verified["http_status"] == 200
    assert verified["status"] == "LIVE_VERIFIED"
    assert verified["category"] is None
    assert verified["cf_ray"] == "ray-1"
    assert verified["content_type"] == "application/json"

    blocked_client = EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(
            SequencedTransport(
                [
                    TransportError(
                        "policy blocked",
                        detail=TransportFailureDetail.PROXY_NETWORK_POLICY,
                    )
                ]
            )
        ),
    )
    blocked = etoro_transport_check(
        ApplicationConfig(etoro_api_enabled=True),
        values=credentials,
        client=blocked_client,
        proxy_values={},
        library_proxies={},
        system_proxy=WindowsProxyDiagnostic(available=True, configured=False),
    )
    assert blocked["endpoint"] == "/api/v1/me"
    assert blocked["http_status"] is None
    assert blocked["status"] == "BLOCKED"
    assert blocked["category"] == "NETWORK_TRANSPORT_ERROR"
    assert blocked["transport_detail"] == "PROXY_NETWORK_POLICY"
    assert blocked["network_policy_detail"] == "SOCKET_POLICY"
    assert blocked["cf_ray"] is None
    assert blocked["content_type"] is None
    serialized = json.dumps(blocked)
    assert "api-secret" not in serialized
    assert "user-secret" not in serialized
    assert "user_0001" not in serialized


@pytest.mark.parametrize(
    "command",
    [
        "broker-status",
        "etoro-status",
        "shadow-run",
        "demo-status",
        "reconcile",
        "performance",
        "demo-smoke-test",
    ],
)
def test_offline_cli_diagnostics_are_safe(
    command: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ETORO_API_KEY", "ETORO_USER_KEY", "ETORO_API_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    assert main((command,), values={}) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["application"] == "Aegis Invest AI"
    assert payload["real_execution_available"] is False
    if command == "demo-smoke-test":
        assert payload["order_submitted"] is False
        assert payload["status"] == "BLOCKED"


def test_performance_statistics_refuse_weak_samples_and_count_operations(now: datetime) -> None:
    weak = (
        PerformanceSnapshot(
            timestamp=now,
            strategy_version="v1",
            equity=Decimal("100"),
            currency=Currency.USD,
        ),
        PerformanceSnapshot(
            timestamp=now + timedelta(days=1),
            strategy_version="v1",
            equity=Decimal("101"),
            currency=Currency.USD,
        ),
    )
    assert annualized_volatility(weak) == NOT_ENOUGH_DATA
    assert sharpe_ratio(weak) == NOT_ENOUGH_DATA
    assert sortino_ratio(weak) == NOT_ENOUGH_DATA

    rich = tuple(
        PerformanceSnapshot(
            timestamp=now + timedelta(days=i),
            strategy_version="v1",
            equity=Decimal("100") + Decimal(i) + (Decimal("-3") if i % 7 == 0 else Decimal("0")),
            currency=Currency.USD,
        )
        for i in range(31)
    )
    assert len(periodic_returns(rich)) == 30
    assert isinstance(annualized_volatility(rich), Decimal)
    assert isinstance(sharpe_ratio(rich), Decimal)
    assert isinstance(sortino_ratio(rich), Decimal)
    assert operational_counts(
        (
            {"would_do": "HOLD"},
            {"would_do": "OPEN", "risk_status": "APPROVED", "demo_execution": True},
            {"would_do": "INCREASE", "risk_status": "REJECTED"},
        )
    ) == {
        "number_of_runs": 3,
        "hold_count": 1,
        "proposal_count": 2,
        "approved_proposals": 1,
        "risk_rejections": 1,
        "demo_executions": 1,
    }


def test_structured_logging_is_json_and_secret_scans_cover_persistence() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("aegis", logging.INFO, __file__, 1, "status ok", (), None)
    record.structured_event = {"status": "ok"}
    payload = json.loads(formatter.format(record))
    assert payload["event"] == {"status": "ok"}
    assert "api-secret" not in formatter.format(record)

    configure_structured_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING


def test_security_source_scan_keeps_real_execution_structurally_absent() -> None:
    root = Path(__file__).resolve().parents[1]
    app_files = tuple((root / "app").rglob("*.py"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in app_files)
    for forbidden in (
        "EtoroRealExecutionAdapter",
        "RealExecutionProvider",
        "REAL_TRADING_ENABLED",
        "REAL_ORDER_ENDPOINT",
        "/api/v3/trading/execution/real",
    ):
        assert forbidden not in source

    for package in ("agent", "domain", "risk", "strategy"):
        package_source = "\n".join(
            path.read_text(encoding="utf-8") for path in (root / "app" / package).rglob("*.py")
        )
        assert "app.brokers.etoro" not in package_source
        assert "app.etoro" not in package_source


def test_etoro_transport_does_not_disable_tls_verification() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "brokers" / "etoro" / "http.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "_create_unverified_context",
        "CERT_NONE",
        "check_hostname = False",
        "verify=False",
    )
    for token in forbidden:
        assert token not in source
