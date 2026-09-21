import json
from functools import partial
from urllib.parse import parse_qs, urlparse

import pytest

from app.brokers.etoro import live_candidates
from app.brokers.etoro import live_session_diagnostic as diagnostic
from app.brokers.etoro.http import HttpResponse
from tests.test_live_catalog_candidates import NOW, Client, row, setup_catalog


@pytest.mark.parametrize(
    "payload,reason",
    [
        (row(1, isCurrentlyTradable=False), "isCurrentlyTradable=false"),
        (row(1, isActiveInPlatform=False), "isActiveInPlatform=false"),
        (row(1, isBuyEnabled=None), "isBuyEnabled!=true"),
        (row(1, isOpen=None, isExchangeOpen=None), "SESSION_STATE=UNKNOWN"),
        (row(225033), "REQUESTED_INSTRUMENT_ID_NOT_RETURNED"),
        (row(1), None),
    ],
)
def test_trace_reports_existing_branch_without_changing_verdict(monkeypatch, payload, reason):
    setup_catalog(monkeypatch, first_type=6)
    traces = []
    result = live_candidates.current_catalog_candidates(
        Client([{"items": [payload]}]),
        clock=lambda: NOW,
        live_get_cap=1,
        session_diagnostics=traces,
    )
    assert traces[0]["exact_rejection_condition"] == reason
    assert traces[0]["open_tradable_result"] == bool(result)
    assert traces[0]["http_status"] == 200
    assert traces[0]["raw_session_fields"][0]["instrumentId"] == payload["instrumentId"]


def test_diagnostic_module_only_ten_gets_and_no_secrets(monkeypatch, capsys):
    setup_catalog(monkeypatch, 20)
    monkeypatch.setattr(
        diagnostic,
        "load_runtime_values",
        lambda: {
            "ETORO_API_ENABLED": "true",
            "ETORO_API_KEY": "secret-api-test",
            "ETORO_USER_KEY": "secret-user-test",
        },
    )
    calls = []

    class Transport:
        def request(self, method, url, headers, body=None):
            assert method == "GET"
            assert body is None
            query = parse_qs(urlparse(url).query)
            iid = int(query["instrumentId"][0])
            calls.append(iid)
            payload = row(iid, isCurrentlyTradable=False, apiKey="must-not-appear")
            return HttpResponse(200, {}, json.dumps({"items": [payload]}).encode())

    monkeypatch.setattr(diagnostic, "UrllibTransport", lambda mode: Transport())
    monkeypatch.setattr(
        diagnostic,
        "current_catalog_candidates",
        partial(live_candidates.current_catalog_candidates, sleeper=lambda _: None),
    )
    diagnostic.main()
    output = capsys.readouterr().out
    result = json.loads(output)
    assert len(calls) == len(set(calls)) == 10
    assert len(result["first_10_results"]) == 10
    assert result["demo_submission_attempts"] == 0
    assert result["demo_write_performed"] is False
    assert result["real_write_performed"] is False
    assert "secret-api-test" not in output
    assert "secret-user-test" not in output
    assert "must-not-appear" not in output
