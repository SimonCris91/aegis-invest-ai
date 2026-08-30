"""Step 7 read-only eToro readiness tests with deterministic fake transports."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import pytest

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import (
    BASE,
    INSTRUMENT_TYPES_PATH,
    INSTRUMENTS_PATH,
    SEARCH_PATH,
    EtoroReadClient,
)
from app.brokers.etoro.http import DisciplinedHttpClient, EtoroHttpFailureKind, HttpResponse
from app.brokers.etoro.mapping import (
    EtoroMappingError,
    map_demo_portfolio,
    map_identity,
    map_instrument_resolution,
)
from app.brokers.etoro.readiness import (
    DEFAULT_READINESS_STORE_PATH,
    build_etoro_readiness_report,
    default_etoro_readiness_store,
)
from app.brokers.models import AccountKind, BrokerIdentity
from app.config import load_config, load_runtime_values
from app.config.models import ApplicationConfig
from app.domain.enums import Currency, MarketStatus, TradeSide
from app.main.__main__ import main
from app.storage.sqlite import SqliteRecordStore
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


class BombClient:
    def identity(self) -> object:
        raise AssertionError("eToro client must not be used without ETORO_USER_KEY")


def _bytes(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _identity_payload() -> dict[str, object]:
    return {
        "gcid": "stable-user-0001",
        "demoCid": 222,
        "realCid": 111,
        "username": "aegis-user",
        "scopes": ["etoro-public:real:read", "etoro-public:user-info:read"],
    }


def _search_payload(
    *,
    symbol: str = "TEST",
    instrument_id: int | None = TEST_INSTRUMENT_ID,
    market_open: bool = True,
    active: bool | None = True,
    buy_enabled: bool = True,
    currently_tradable: bool = True,
    delisted: bool = False,
    hidden: bool = False,
    include_diagnostic_flags: bool = True,
) -> dict[str, object]:
    item: dict[str, object] = {
        "displayname": "Test Instrument",
        "internalSymbolFull": symbol,
        "instrumentType": "Stocks",
        "isBuyEnabled": buy_enabled,
        "isHiddenFromClient": hidden,
        "isDelisted": delisted,
        "currentRate": "10",
    }
    if instrument_id is not None:
        item["instrumentId"] = instrument_id
    if include_diagnostic_flags:
        item["isOpen"] = market_open
        item["isExchangeOpen"] = market_open
        item["isCurrentlyTradable"] = currently_tradable
        item["isActiveInPlatform"] = active
    return {
        "page": 1,
        "pageSize": 10,
        "totalItems": 1,
        "items": [item],
    }


def _rate_payload(now: datetime) -> dict[str, object]:
    return {
        "rates": [
            {
                "instrumentID": TEST_INSTRUMENT_ID,
                "ask": "10.01",
                "bid": "9.99",
                "lastExecution": "10",
                "date": now.isoformat(),
            }
        ]
    }


def _demo_aggregate_payload(now: datetime) -> dict[str, object]:
    return {
        "cid": 222,
        "timestamp": now.isoformat(),
        "accountCurrency": "USD",
        "accountTotals": {
            "accountAvailableCash": "4320.84",
            "accountCurrentPnl": "-300.35",
            "accountTotalValue": "5154.48",
            "accountBalance": "4320.84",
        },
        "instrumentAggregates": [
            {
                "instrumentId": TEST_INSTRUMENT_ID,
                "assetCurrency": "USD",
                "pnlAssetCurrency": "-10",
                "netUnits": "2",
                "netCurrentExposureAccountCurrency": "200",
                "netInitialExposureAccountCurrency": "210",
                "accountCurrencyReturn": "-10",
                "avgLeverage": "1",
                "avgOpenRate": "105",
            }
        ],
    }


def _eligibility_payload(*, allow_open: bool = True) -> dict[str, object]:
    return {
        "currency": "USD",
        "notFoundInstrumentIds": [],
        "notFoundSymbols": [],
        "eligibilities": [
            {
                "instrumentId": TEST_INSTRUMENT_ID,
                "symbol": "TEST",
                "minPositionExposure": "50",
                "allowOpenPosition": allow_open,
                "leverageConfigs": [
                    {
                        "settlementType": "real",
                        "direction": "long",
                        "leverageValues": [1],
                        "minPositionAmount": "50",
                    }
                ],
            }
        ],
    }


def _real_portfolio_payload() -> dict[str, object]:
    return {"clientPortfolio": {"credit": "200", "positions": []}}


def _values() -> dict[str, str]:
    return {
        "ETORO_API_KEY": "api-secret",
        "ETORO_USER_KEY": "user-secret",
        "ETORO_EXPECTED_USERNAME": "aegis-user",
        "ETORO_EXPECTED_GCID": "stable-user-0001",
        "AEGIS_ETORO_READINESS_SYMBOL": "TEST",
        "AEGIS_ETORO_READINESS_INSTRUMENT_ID": str(TEST_INSTRUMENT_ID),
        "AEGIS_ETORO_READINESS_MAX_QUOTE_AGE_SECONDS": "300",
    }


def _client(transport: SequencedTransport) -> EtoroReadClient:
    return EtoroReadClient(
        EtoroCredentials(api_key="api-secret", user_key="user-secret"),
        DisciplinedHttpClient(transport, max_read_attempts=1),
    )


def test_default_readiness_store_uses_work_directory_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    store = default_etoro_readiness_store()
    assert DEFAULT_READINESS_STORE_PATH == Path("work") / "etoro-readiness.sqlite3"
    store.append("etoro-readiness-report", {"status": "READ_ONLY_READY"})

    restarted = default_etoro_readiness_store()
    assert restarted.list("etoro-readiness-report")[0]["status"] == "READ_ONLY_READY"
    with pytest.raises(ValueError, match="secret-shaped"):
        restarted.append("bad-readiness", {"headers": {"x-user-key": "user-secret"}})


def test_identity_mapping_includes_username_without_serializing_it() -> None:
    identity = map_identity(_identity_payload())

    assert identity.username == "aegis-user"
    assert identity.redacted_reference == "user:0001"
    assert "aegis-user" not in repr(identity)
    assert "username" not in identity.model_dump(mode="json")


def test_demo_aggregate_mapping_normalizes_positions_and_pnl(now: datetime) -> None:
    identity = BrokerIdentity(
        stable_user_id="stable-user-0001",
        demo_account_id=222,
        real_account_id=111,
    )

    snapshot = map_demo_portfolio(_demo_aggregate_payload(now), identity)

    assert snapshot.context.kind is AccountKind.DEMO
    assert snapshot.cash == Decimal("4320.84")
    assert snapshot.current_pnl == Decimal("-300.35")
    assert snapshot.account_balance == Decimal("4320.84")
    assert snapshot.positions[0].side is TradeSide.BUY
    assert snapshot.positions[0].asset_currency is Currency.USD


def test_demo_aggregate_mapping_treats_naive_provider_timestamp_as_utc(
    now: datetime,
) -> None:
    identity = BrokerIdentity(
        stable_user_id="stable-user-0001",
        demo_account_id=222,
        real_account_id=111,
    )
    payload = _demo_aggregate_payload(now)
    payload["timestamp"] = now.replace(tzinfo=None).isoformat()

    snapshot = map_demo_portfolio(payload, identity)

    assert snapshot.as_of.tzinfo is UTC


def test_instrument_resolution_valid_open_market(now: datetime) -> None:
    resolution = map_instrument_resolution(_search_payload(), symbol="TEST", as_of=now)

    assert resolution.instrument_id == TEST_INSTRUMENT_ID
    assert resolution.resolved
    assert resolution.structurally_supported
    assert resolution.structural_status == "SUPPORTED"
    assert resolution.market_status is MarketStatus.OPEN
    assert resolution.is_active_in_platform is True
    assert resolution.is_delisted is False
    assert resolution.is_hidden_from_client is False
    assert resolution.is_currently_tradable is True
    assert resolution.is_buy_enabled is True
    assert resolution.current_rate == Decimal("10")
    assert resolution.verified


def test_instrument_resolution_valid_closed_market_is_not_unsupported(
    now: datetime,
) -> None:
    resolution = map_instrument_resolution(
        _search_payload(market_open=False, currently_tradable=False),
        symbol="TEST",
        as_of=now,
    )

    assert resolution.resolved
    assert resolution.structurally_supported
    assert resolution.structural_status == "SUPPORTED"
    assert resolution.market_status is MarketStatus.CLOSED
    assert resolution.verified


def test_instrument_resolution_exact_symbol_with_inactive_platform_flag_is_resolved(
    now: datetime,
) -> None:
    resolution = map_instrument_resolution(
        _search_payload(active=False, currently_tradable=False),
        symbol="TEST",
        as_of=now,
    )

    assert resolution.resolved
    assert resolution.structurally_supported
    assert resolution.structural_status == "SUPPORTED"
    assert resolution.is_active_in_platform is False
    assert resolution.is_currently_tradable is False
    assert resolution.verified


def test_instrument_resolution_rejects_delisted_instrument(now: datetime) -> None:
    resolution = map_instrument_resolution(
        _search_payload(delisted=True),
        symbol="TEST",
        as_of=now,
    )

    assert resolution.resolved
    assert not resolution.structurally_supported
    assert resolution.structural_status == "DELISTED"
    assert not resolution.verified


def test_instrument_resolution_rejects_hidden_instrument(now: datetime) -> None:
    resolution = map_instrument_resolution(
        _search_payload(hidden=True),
        symbol="TEST",
        as_of=now,
    )

    assert resolution.resolved
    assert not resolution.structurally_supported
    assert resolution.structural_status == "HIDDEN_FROM_CLIENT"
    assert not resolution.verified


def test_instrument_resolution_missing_boolean_fields_are_unknown(
    now: datetime,
) -> None:
    resolution = map_instrument_resolution(
        _search_payload(include_diagnostic_flags=False),
        symbol="TEST",
        as_of=now,
    )

    assert resolution.resolved
    assert resolution.is_active_in_platform is None
    assert resolution.is_delisted is False
    assert resolution.is_hidden_from_client is False
    assert resolution.structurally_supported
    assert resolution.structural_status == "SUPPORTED"
    assert resolution.market_status is MarketStatus.UNKNOWN
    assert resolution.verified


def test_instrument_resolution_rejects_unresolved_symbol(now: datetime) -> None:
    with pytest.raises(EtoroMappingError):
        map_instrument_resolution(_search_payload(), symbol="OTHER", as_of=now)


def test_instrument_resolution_rejects_not_found_symbol(now: datetime) -> None:
    with pytest.raises(EtoroMappingError):
        map_instrument_resolution({"items": []}, symbol="TEST", as_of=now)


@pytest.mark.parametrize("instrument_id", [None, 0, -1])
def test_instrument_resolution_rejects_invalid_or_missing_instrument_id(
    now: datetime, instrument_id: int | None
) -> None:
    with pytest.raises(EtoroMappingError):
        map_instrument_resolution(
            _search_payload(instrument_id=instrument_id),
            symbol="TEST",
            as_of=now,
        )


def test_read_client_resolves_instrument_with_documented_search_query(now: datetime) -> None:
    transport = SequencedTransport([HttpResponse(200, {}, _bytes(_search_payload()))])

    resolution = _client(transport).resolve_instrument("TEST", as_of=now)

    assert resolution.symbol == "TEST"
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1].startswith(BASE + SEARCH_PATH)
    fields = parse_qs(urlparse(transport.calls[0][1]).query)["fields"][0].split(",")
    assert fields == [
        "instrumentId",
        "displayname",
        "internalSymbolFull",
        "instrumentType",
        "instrumentTypeID",
        "instrumentTypeId",
        "internalAssetClassName",
        "internalAssetClassId",
        "internalCryptoTypeId",
        "assetClass",
        "assetType",
        "type",
        "instrumentClass",
        "securityType",
        "marketType",
        "category",
        "subCategory",
        "underlying",
        "exchangeID",
        "exchangeId",
        "internalExchangeName",
        "symbol",
        "isOpen",
        "isExchangeOpen",
        "isCurrentlyTradable",
        "isBuyEnabled",
        "isHiddenFromClient",
        "isDelisted",
        "isActiveInPlatform",
        "currentRate",
    ]
    assert "internalSymbolFull=TEST" in transport.calls[0][1]


def test_read_client_raw_search_and_metadata_reads_are_read_only() -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes(_search_payload(symbol="AAPL"))),
            HttpResponse(
                200,
                {},
                _bytes(
                    {
                        "instruments": [
                            {
                                "instrumentId": 1001,
                                "internalAssetClassName": "Stocks",
                                "instrumentTypeID": 1,
                            }
                        ]
                    }
                ),
            ),
            HttpResponse(
                200,
                {},
                _bytes(
                    {
                        "instrumentTypes": [
                            {"instrumentTypeID": 1, "instrumentType": "Stocks"},
                            {"id": 2, "name": "ETF"},
                        ]
                    }
                ),
            ),
        ]
    )
    client = _client(transport)

    raw = client.raw_instrument_search("AAPL")
    metadata = client.instrument_metadata((1001,))
    type_names = client.instrument_type_names()

    assert isinstance(raw, dict)
    assert metadata[1001]["internalAssetClassName"] == "Stocks"
    assert type_names == {1: "Stocks", 2: "ETF"}
    assert transport.calls[0][0] == "GET"
    assert transport.calls[1][0] == "GET"
    assert transport.calls[2][0] == "GET"
    assert transport.calls[1][1].startswith(BASE + INSTRUMENTS_PATH)
    assert transport.calls[2][1] == BASE + INSTRUMENT_TYPES_PATH
    assert all(call[3] is None for call in transport.calls)


def test_read_client_metadata_extractors_tolerate_unexpected_payloads() -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes({"notAList": {"instrumentId": 1001}})),
            HttpResponse(200, {}, _bytes({"items": [{"id": "bad"}, {"ID": 7}]})),
        ]
    )
    client = _client(transport)

    assert client.instrument_metadata((1001,)) == {}
    assert client.instrument_type_names() == {}


def test_readiness_report_fails_closed_without_credentials_and_cli_is_secret_free(
    now: datetime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ETORO_API_KEY", "ETORO_USER_KEY", "ETORO_API_ENABLED"):
        monkeypatch.delenv(name, raising=False)

    report = build_etoro_readiness_report(ApplicationConfig(), values={}, clock=lambda: now)

    assert report.status == "NOT_CONFIGURED"
    assert not report.credentials_configured
    assert not report.authentication_attempted
    assert not report.real_execution_available

    assert main(("etoro-readiness",), values={}) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "NOT_CONFIGURED"
    assert "api-secret" not in output
    assert "user-secret" not in output


def test_readiness_does_not_touch_client_without_user_key(now: datetime, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "ETORO_API_ENABLED=true",
                "ETORO_API_KEY=synthetic-api-secret",
                "ETORO_USER_KEY=",
                "ETORO_EXPECTED_USERNAME=aegis-user",
            )
        ),
        encoding="utf-8",
    )
    runtime_values = load_runtime_values(values={}, env_file=env_file)
    report = build_etoro_readiness_report(
        load_config(runtime_values),
        values=runtime_values,
        client=cast(EtoroReadClient, BombClient()),
        clock=lambda: now,
    )

    assert report.status == "NOT_CONFIGURED"
    assert not report.authentication_attempted
    assert not report.authentication_successful


def test_readiness_blocks_when_expected_identity_is_missing_after_auth(now: datetime) -> None:
    transport = SequencedTransport([HttpResponse(200, {}, _bytes(_identity_payload()))])
    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values={"ETORO_API_KEY": "api-secret", "ETORO_USER_KEY": "user-secret"},
        client=_client(transport),
        clock=lambda: now,
    )

    assert report.status == "BLOCKED"
    assert report.authentication_attempted
    assert report.authentication_successful
    assert not report.broker_identity_verified
    assert len(transport.calls) == 1


def test_readiness_blocks_on_identity_mismatch_before_downstream(now: datetime) -> None:
    transport = SequencedTransport([HttpResponse(200, {}, _bytes(_identity_payload()))])
    values = {**_values(), "ETORO_EXPECTED_USERNAME": "someone-else"}

    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values=values,
        client=_client(transport),
        clock=lambda: now,
    )

    assert report.status == "FAIL"
    assert report.authentication_successful
    assert not report.broker_identity_verified
    assert len(transport.calls) == 1


def test_readiness_classifies_cloudflare_1010_as_edge_waf_block(now: datetime) -> None:
    transport = SequencedTransport(
        [HttpResponse(403, {"CF-RAY": "edge-test"}, b"error code: 1010")]
    )

    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values=_values(),
        client=_client(transport),
        clock=lambda: now,
    )

    authentication = next(check for check in report.checks if check.name == "authentication")
    assert report.status == "FAIL"
    assert authentication.metadata["category"] == EtoroHttpFailureKind.EDGE_WAF_BLOCK.value
    assert authentication.metadata["http_status"] == "403"
    assert authentication.metadata["cf_ray"] == "edge-test"
    assert authentication.metadata["response_body"] == "error code: 1010"
    assert len(transport.calls) == 1


def test_readiness_live_path_is_read_only_with_mock_transport(
    now: datetime, tmp_path: Path
) -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes(_identity_payload())),
            HttpResponse(200, {}, _bytes(_search_payload())),
            HttpResponse(200, {}, _bytes(_rate_payload(now))),
            HttpResponse(200, {}, _bytes(_demo_aggregate_payload(now))),
            HttpResponse(200, {}, _bytes(_real_portfolio_payload())),
            HttpResponse(200, {}, _bytes(_eligibility_payload())),
        ]
    )
    store_path = tmp_path / "readiness.sqlite3"
    store = SqliteRecordStore(store_path)

    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values=_values(),
        client=_client(transport),
        clock=lambda: now,
        store=store,
    )

    assert report.status == "READ_ONLY_READY"
    assert report.authentication_successful
    assert report.broker_identity_verified
    market_check = next(check for check in report.checks if check.name == "market_currently_open")
    assert market_check.status == "PASS"
    assert report.live_rates_verified
    assert report.demo_portfolio_read_verified
    assert report.real_portfolio_read_only_verified
    assert report.demo_eligibility_verified
    assert report.shadow_mode_live_data_verified
    assert not report.demo_execution_ready
    assert not report.real_execution_available
    assert not report.demo_auto_execution_enabled

    methods = [call[0] for call in transport.calls]
    assert methods == ["GET", "GET", "GET", "GET", "GET", "POST"]
    urls = [call[1] for call in transport.calls]
    assert all("orders" not in url and "execution" not in url for url in urls)
    restarted_store = SqliteRecordStore(store_path)
    shadow_record = restarted_store.list("track-record:SHADOW")[0]
    assert shadow_record["broker_write_calls"] == 0
    assert shadow_record["mode"] == "SHADOW"
    assert shadow_record["strategy_version"] == "etoro-readiness-v1"
    assert shadow_record["risk_status"] in {"NONE", "APPROVED", "REJECTED"}
    serialized_shadow = json.dumps(shadow_record, sort_keys=True)
    assert "api-secret" not in serialized_shadow
    assert "user-secret" not in serialized_shadow
    assert "stable-user-0001" not in serialized_shadow
    assert "aegis-user" not in serialized_shadow
    assert "x-api-key" not in serialized_shadow
    assert "x-user-key" not in serialized_shadow
    persisted_report = restarted_store.list("etoro-readiness-report")[0]
    assert persisted_report["real_execution_available"] is False
    serialized_report = json.dumps(persisted_report, sort_keys=True)
    assert "api-secret" not in serialized_report
    assert "user-secret" not in serialized_report
    assert "stable-user-0001" not in serialized_report
    assert "aegis-user" not in serialized_report


def test_readiness_continues_when_structural_instrument_market_is_closed(
    now: datetime, tmp_path: Path
) -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes(_identity_payload())),
            HttpResponse(
                200,
                {},
                _bytes(_search_payload(market_open=False, currently_tradable=False)),
            ),
            HttpResponse(200, {}, _bytes(_rate_payload(now))),
            HttpResponse(200, {}, _bytes(_demo_aggregate_payload(now))),
            HttpResponse(200, {}, _bytes(_real_portfolio_payload())),
            HttpResponse(200, {}, _bytes(_eligibility_payload())),
        ]
    )
    store = SqliteRecordStore(tmp_path / "readiness.sqlite3")

    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values=_values(),
        client=_client(transport),
        clock=lambda: now,
        store=store,
    )

    market_check = next(check for check in report.checks if check.name == "market_currently_open")
    assert market_check.status == "MARKET_CLOSED"
    assert report.status == "READ_ONLY_READY"
    assert report.demo_portfolio_read_verified
    assert report.real_portfolio_read_only_verified
    assert report.demo_eligibility_verified
    assert report.shadow_mode_live_data_verified
    assert len(transport.calls) == 6


def test_readiness_continues_when_exact_instrument_has_inactive_platform_flag(
    now: datetime, tmp_path: Path
) -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes(_identity_payload())),
            HttpResponse(
                200,
                {},
                _bytes(_search_payload(active=False, currently_tradable=False)),
            ),
            HttpResponse(200, {}, _bytes(_rate_payload(now))),
            HttpResponse(200, {}, _bytes(_demo_aggregate_payload(now))),
            HttpResponse(200, {}, _bytes(_real_portfolio_payload())),
            HttpResponse(200, {}, _bytes(_eligibility_payload())),
        ]
    )
    store = SqliteRecordStore(tmp_path / "readiness.sqlite3")

    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values=_values(),
        client=_client(transport),
        clock=lambda: now,
        store=store,
    )

    resolution_check = next(
        check for check in report.checks if check.name == "instrument_resolution"
    )
    assert resolution_check.status == "PASS"
    assert report.status == "READ_ONLY_READY"
    assert report.demo_eligibility_verified
    assert report.shadow_mode_live_data_verified
    assert len(transport.calls) == 6


def test_readiness_stops_when_demo_eligibility_disallows_opening(now: datetime) -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes(_identity_payload())),
            HttpResponse(200, {}, _bytes(_search_payload())),
            HttpResponse(200, {}, _bytes(_rate_payload(now))),
            HttpResponse(200, {}, _bytes(_demo_aggregate_payload(now))),
            HttpResponse(200, {}, _bytes(_real_portfolio_payload())),
            HttpResponse(200, {}, _bytes(_eligibility_payload(allow_open=False))),
        ]
    )

    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values=_values(),
        client=_client(transport),
        clock=lambda: now,
    )

    eligibility_check = next(check for check in report.checks if check.name == "demo_eligibility")
    assert report.status == "FAIL"
    assert eligibility_check.status == "FAIL"
    assert eligibility_check.reason == "Demo eligibility does not allow opening this instrument"
    assert not report.demo_eligibility_verified
    assert not report.shadow_mode_live_data_verified
    assert len(transport.calls) == 6


def test_readiness_stops_at_instrument_failure_before_portfolio_reads(now: datetime) -> None:
    transport = SequencedTransport(
        [
            HttpResponse(200, {}, _bytes(_identity_payload())),
            HttpResponse(200, {}, _bytes({"items": []})),
        ]
    )

    report = build_etoro_readiness_report(
        ApplicationConfig(etoro_api_enabled=True),
        values=_values(),
        client=_client(transport),
        clock=lambda: now,
    )

    assert report.status == "FAIL"
    assert len(transport.calls) == 2
    assert all("/portfolio" not in call[1] for call in transport.calls)
    assert not report.live_rates_verified
    assert not report.demo_portfolio_read_verified
    assert not report.real_portfolio_read_only_verified
