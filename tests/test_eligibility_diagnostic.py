import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.brokers.etoro import eligibility_diagnostic as diagnostic
from app.brokers.etoro.http import HttpResponse
from app.config import load_config
from app.main import __main__ as cli


def test_help_registers_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["etoro-demo-execution-once", "--help"], values={})
    assert exc.value.code == 0
    assert "--diagnose-demo-eligibility" in capsys.readouterr().out


def test_cli_diagnostic_precedes_execution_even_with_confirmation(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("execution/store must not be reached")

    monkeypatch.setattr(cli, "run_operational_demo_once", forbidden)
    monkeypatch.setattr(cli, "SqliteRecordStore", forbidden)
    monkeypatch.setattr(cli, "load_runtime_values", lambda values: {})
    cli.main(
        ["etoro-demo-execution-once", "--diagnose-demo-eligibility", "--confirm-demo-write"],
        values={},
    )
    report = json.loads(capsys.readouterr().out)
    assert report["demo_submission_attempts"] == 0
    assert report["demo_write_performed"] is False
    assert report["real_write_performed"] is False


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_transport_blocks_writes(method):
    transport = MagicMock()
    with pytest.raises(ValueError):
        diagnostic.GetOnlyTransport(transport).request(method, "https://example.invalid", {})
    transport.request.assert_not_called()


@pytest.mark.parametrize("status", [200, 405])
def test_bounded_get_only_raw_mapping_diagnostic(monkeypatch, status):
    monkeypatch.setattr(diagnostic, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        diagnostic,
        "current_catalog_candidates",
        lambda client, **kwargs: tuple(
            SimpleNamespace(symbol=f"CRYPTO{i}", broker_instrument_id=str(i)) for i in range(1, 13)
        ),
    )
    requests = []

    class Transport:
        def request(self, method, url, headers, body=None):
            assert method == "POST" and body is not None
            assert url == diagnostic.BASE + diagnostic.DEMO_ELIGIBILITY_PATH
            requests.append(url)
            iid = len(requests)
            assert json.loads(body) == {
                "instrumentIds": [iid],
                "symbols": [f"CRYPTO{iid}"],
                "currency": "USD",
            }
            raw = {
                "currency": "USD",
                "eligibilities": [{"instrumentId": iid, "leverageConfigs": []}],
                "token": "secret-api",
                "message": "secret-user",
            }
            return HttpResponse(status, {}, json.dumps(raw).encode())

    values = {
        "AEGIS_OPERATING_MODE": "ETORO_DEMO",
        "ETORO_API_ENABLED": "true",
        "ETORO_API_KEY": "secret-api",
        "ETORO_USER_KEY": "secret-user",
    }
    result = diagnostic.diagnose_demo_eligibility(
        load_config(values), values, transport=Transport()
    )
    assert len(requests) == 10
    assert len(result["samples"]) == 10
    assert result["current_candidates_diagnosed"] == 10
    assert [x["symbol"] for x in result["samples"]] == [f"CRYPTO{i}" for i in range(1, 11)]
    assert result["demo_submission_attempts"] == 0
    assert result["demo_write_performed"] is False
    assert result["real_write_performed"] is False
    assert "secret-api" not in json.dumps(result)
    assert "secret-user" not in json.dumps(result)
    for sample in result["samples"]:
        assert sample["http_status"] == status
        if status == 200:
            assert sample["mapping_exception_type"] == "EtoroMappingError"
            assert sample["mapping_cause_type"] == "StopIteration"
            assert sample["leverage_configs_count"] == 0
            assert "eligibilities" in sample["raw_top_level_keys"]
        else:
            assert "mapping_exception_type" not in sample
        assert sample["mapping_result"] == ("FAIL" if status == 200 else "HTTP_ERROR")


@pytest.mark.parametrize("status", [200, 429])
def test_real_selector_crypto_payload_reaches_eligibility(monkeypatch, status):
    from tests.test_live_catalog_candidates import row, setup_catalog

    setup_catalog(monkeypatch, count=1, first_type=10)
    monkeypatch.setattr(diagnostic, "sleep", lambda seconds: None)
    requests = []

    class Transport:
        def request(self, method, url, headers, body=None):
            requests.append(url)
            if "/market-data/search?" in url:
                assert method == "GET" and body is None
                raw = {"items": [row(1, isExchangeOpen=False, isOpen=None)]}
                return HttpResponse(200, {}, json.dumps(raw).encode())
            assert diagnostic.DEMO_ELIGIBILITY_PATH in url
            assert method == "POST"
            assert json.loads(body) == {"instrumentIds": [1], "symbols": ["S1"], "currency": "USD"}
            raw = {
                "currency": "USD",
                "eligibilities": [
                    {
                        "instrumentId": 1,
                        "minPositionExposure": 10,
                        "allowOpenPosition": False,
                        "allowedOrderQuantityType": ["Units"],
                        "leverageConfigs": [
                            {
                                "settlementType": "Real",
                                "direction": "Long",
                                "leverageValues": [1],
                                "minPositionAmount": 10,
                            }
                        ],
                    }
                ],
            }
            return HttpResponse(status, {"Retry-After": "60"}, json.dumps(raw).encode())

    values = {
        "AEGIS_OPERATING_MODE": "ETORO_DEMO",
        "ETORO_API_ENABLED": "true",
        "ETORO_API_KEY": "secret-api",
        "ETORO_USER_KEY": "secret-user",
    }
    result = diagnostic.diagnose_demo_eligibility(
        load_config(values), values, transport=Transport()
    )
    assert len(requests) == 2
    assert result["live_selection"]["live_open_tradable_count"] == 1
    assert result["current_candidates_diagnosed"] == 1
    sample = result["samples"][0]
    assert sample["instrument_id"] == 1
    assert sample["symbol"] == "S1"
    assert sample["eligibility_endpoint"] == diagnostic.DEMO_ELIGIBILITY_PATH
    if status == 200:
        assert sample["mapping_result"] == "PASS"
        assert sample["exact_eligibility_rejection"] == [
            "allowOpenPosition is false",
            "amount order quantity type is not supported",
        ]
    else:
        assert result["status"] == "RATE_LIMITED_STOPPED_NO_RETRY"
        assert sample["retry_after"] == "60"
    assert result["demo_writes"] == result["real_writes"] == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://public-api.etoro.com/api/v2/trading/execution/demo/orders",
        "https://public-api.etoro.com/api/v2/trading/info/eligibility",
        "https://example.invalid/api/v2/trading/info/demo/eligibility",
        "https://public-api.etoro.com/api/v2/trading/info/demo/eligibility?extra=1",
    ],
)
def test_diagnostic_post_allowlist_blocks_every_other_endpoint(url):
    transport = MagicMock()
    with pytest.raises(ValueError):
        diagnostic.EligibilityDiagnosticTransport(transport).request("POST", url, {}, b"{}")
    transport.request.assert_not_called()
