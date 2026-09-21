import pytest

from app.brokers.etoro import live_candidates as module
from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse
from tests.test_live_catalog_candidates import NOW, Client, setup_catalog


def test_bounded_calls_pacing_and_unchecked_count(monkeypatch):
    setup_catalog(monkeypatch, 200)
    client = Client([{"items": []}, {"items": []}])
    pauses = []
    stats = {}
    module.current_catalog_candidates(
        client,
        clock=lambda: NOW,
        live_get_cap=2,
        pacing_delay_seconds=1.5,
        sleeper=pauses.append,
        selection_diagnostics=stats,
    )
    assert len(client.calls) == 2
    assert pauses == [1.5]
    assert stats["unchecked_due_to_cap_count"] == 197
    assert stats["live_get_count"] == 2
    assert len({i for batch in client.calls for i in batch}) == 2


def test_default_limit_is_50_physical_lookups(monkeypatch):
    setup_catalog(monkeypatch, 3300)
    client = Client([{"items": []}] * 50)
    pauses = []
    stats = {}
    module.current_catalog_candidates(client, sleeper=pauses.append, selection_diagnostics=stats)
    assert len(client.calls) == 50
    assert pauses == [1.0] * 49
    assert stats["unchecked_due_to_cap_count"] == 3249


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Retry-After": "120"}, 120),
        (
            {
                "retry-after": "Sat, 05 Sep 2026 00:02:00 GMT",
                "Date": "Sat, 05 Sep 2026 00:00:00 GMT",
            },
            120,
        ),
        ({"Retry-After": "invalid"}, None),
        ({}, None),
    ],
)
def test_429_stops_without_retry_or_candidates(monkeypatch, headers, expected):
    setup_catalog(monkeypatch, 200)

    class Limited(Client):
        def session_catalog_ids(self, ids):
            self.calls.append(ids)
            raise EtoroApiError(
                "rate limited", endpoint="/search", status=429, response_headers=headers
            )

    client = Limited([])
    stats = {}
    pauses = []
    with pytest.raises(EtoroApiError):
        module.current_catalog_candidates(
            client, sleeper=pauses.append, selection_diagnostics=stats
        )
    assert len(client.calls) == 1
    assert pauses == []
    assert stats["http_429_count"] == 1
    assert stats["retry_after_seconds"] == expected
    assert stats["status"] == "RATE_LIMITED_STOPPED_NO_RETRY"


def test_session_bulk_is_one_physical_get_even_on_429():
    class Transport:
        calls = 0

        def request(self, method, url, headers, body=None):
            assert method == "GET"
            self.calls += 1
            return HttpResponse(429, {"Retry-After": "120"}, b"{}")

    transport = Transport()
    client = EtoroReadClient(
        EtoroCredentials(api_key="test", user_key="test"), DisciplinedHttpClient(transport)
    )
    with pytest.raises(EtoroApiError):
        client.session_catalog_ids((1,))
    assert transport.calls == 1


def test_shared_static_exclusions(monkeypatch):
    from app.data.runtime import catalog_session_exclusion_reason

    rows = [
        {"instrumentID": 1, "instrumentTypeID": 10, "symbolFull": "CRYPTO"},
        {"instrumentID": 2, "instrumentTypeID": 5, "isInternalInstrument": True},
        {"instrumentID": 3, "instrumentTypeID": 5, "isDelisted": True},
        {"instrumentID": 4, "instrumentTypeID": 5, "isHiddenFromClient": True},
        {"instrumentID": 5, "instrumentTypeID": 5, "isActiveInPlatform": False},
        {"instrumentID": 6, "instrumentTypeID": 1},
    ]
    assert catalog_session_exclusion_reason(rows[0]) is None
    assert all(catalog_session_exclusion_reason(r) for r in rows[1:])
    monkeypatch.setattr(
        module,
        "read_etoro_instrument_catalog_snapshot",
        lambda: {"snapshot_id": "test", "raw_response": {"items": rows}},
    )
    client = Client([{"items": []}])
    module.current_catalog_candidates(client)
    assert client.calls == [(1,)]


@pytest.mark.parametrize("cap,delay", [(0, 1), (51, 1), (1, 0), (1, float("nan"))])
def test_invalid_limits_never_query(cap, delay):
    client = Client([])
    with pytest.raises(ValueError):
        module.current_catalog_candidates(client, live_get_cap=cap, pacing_delay_seconds=delay)
    assert client.calls == []
