from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from app.brokers.etoro import live_candidates as module

NOW = datetime(2026, 9, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_wall_clock_sleep(monkeypatch):
    monkeypatch.setattr(
        module,
        "current_catalog_candidates",
        partial(module.current_catalog_candidates, sleeper=lambda _: None),
    )


def setup_catalog(monkeypatch, count=6, first_type=10):
    rows = [
        {
            "instrumentID": i,
            "symbolFull": f"S{i}",
            "instrumentTypeID": first_type if i == 1 else 6,
            "isInternalInstrument": i == 5,
        }
        for i in range(1, count + 1)
    ]
    monkeypatch.setattr(
        module,
        "read_etoro_instrument_catalog_snapshot",
        lambda: {"snapshot_id": "test", "raw_response": {"items": rows}},
    )


def row(i, **changes):
    return {
        "instrumentId": i,
        "internalSymbolFull": f"S{i}",
        "isOpen": True,
        "isExchangeOpen": True,
        "isCurrentlyTradable": True,
        "isBuyEnabled": True,
        "isActiveInPlatform": True,
        "isInternalInstrument": False,
        "isHiddenFromClient": False,
        "isDelisted": False,
        **changes,
    }


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.last_response = {"items": []}

    def session_catalog_page(self, **kwargs):
        raise AssertionError("full search pagination is forbidden")

    def session_catalog_ids(self, ids):
        self.calls.append(ids)
        self.last_response = next(self.responses, self.last_response)
        return self.last_response


def test_catalog_is_universe_and_lookup_is_id_scoped(monkeypatch):
    setup_catalog(monkeypatch)
    client = Client(
        [
            {
                "totalItems": 999999,
                "items": [
                    row(1),
                    row(2, isOpen=False, isExchangeOpen=False),
                    row(3, isCurrentlyTradable=None),
                    row(4, isBuyEnabled=False),
                    row(5),
                    row(6),
                    row(225033, isInternalInstrument=True),
                ],
            }
        ]
    )
    result = module.current_catalog_candidates(client, clock=lambda: NOW)
    assert client.calls == [(2,), (1,), (3,), (4,), (6,)]
    assert [x.broker_instrument_id for x in result] == ["1", "6"]
    assert result[0].asset_class.value == "CRYPTO"


def test_all_catalog_batches_continue_without_search_completeness(monkeypatch):
    setup_catalog(monkeypatch, 130)
    client = Client(
        [
            {"items": [row(225033)] * 100, "totalItems": 16152},
            {"items": [], "totalItems": 16152},
            {"items": [row(3)], "totalItems": 16152},
        ]
    )
    result = module.current_catalog_candidates(client, clock=lambda: NOW, live_get_cap=3)
    assert [len(ids) for ids in client.calls] == [1, 1, 1]
    assert len({i for ids in client.calls for i in ids}) == 3
    assert [x.broker_instrument_id for x in result] == ["3"]


def test_catalog_offset_continues_ordered_batches_without_repeating(monkeypatch):
    setup_catalog(monkeypatch, 130)
    client = Client([{"items": []}] * 100)
    first_stats = {}
    second_stats = {}

    module.current_catalog_candidates(
        client,
        clock=lambda: NOW,
        live_get_cap=50,
        catalog_offset=0,
        selection_diagnostics=first_stats,
    )
    module.current_catalog_candidates(
        client,
        clock=lambda: NOW,
        live_get_cap=50,
        catalog_offset=50,
        selection_diagnostics=second_stats,
    )

    etf_ids = [i for i in range(1, 131) if i not in {1, 5}]
    crypto_ids = [1]
    expected_ids = []
    for index in range(max(len(etf_ids), len(crypto_ids))):
        if index < len(etf_ids):
            expected_ids.append(etf_ids[index])
        if index < len(crypto_ids):
            expected_ids.append(crypto_ids[index])
    assert client.calls == [(i,) for i in expected_ids[:100]]
    assert first_stats["batch_catalog_count"] == 50
    assert first_stats["batch_end_offset"] == 50
    assert second_stats["catalog_offset"] == 50
    assert second_stats["batch_catalog_count"] == 50
    assert second_stats["batch_end_offset"] == 100


@pytest.mark.parametrize("duplicate", [row(1), row(1, displayname="Name", currentRate=123)])
def test_compatible_duplicates_are_canonical(monkeypatch, duplicate):
    setup_catalog(monkeypatch)
    diagnostics = []
    result = module.current_catalog_candidates(
        Client([{"items": [row(1), duplicate, row(6)]}]),
        clock=lambda: NOW,
        duplicate_diagnostics=diagnostics,
    )
    assert [x.broker_instrument_id for x in result] == ["1", "6"]
    assert diagnostics[0]["duplicate_count"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"internalSymbolFull": "CONFLICT"},
        {"isOpen": False},
        {"isBuyEnabled": False},
        {"isInternalInstrument": True},
        {"isCurrentlyTradable": 1},
        {"isOpen": None},
    ],
)
def test_conflicts_excluded_locally_and_sticky(monkeypatch, changes):
    setup_catalog(monkeypatch)
    diagnostics = []
    result = module.current_catalog_candidates(
        Client([{"items": [row(1), row(1, **changes), row(1), row(6)]}]),
        clock=lambda: NOW,
        duplicate_diagnostics=diagnostics,
    )
    assert [x.broker_instrument_id for x in result] == ["6"]
    assert diagnostics[0]["classification"] == "CONFLICTING"


@pytest.mark.parametrize(
    "changes",
    [
        {"isInternalInstrument": True},
        {"isActiveInPlatform": False},
        {"isCurrentlyTradable": False},
    ],
)
def test_unusable_duplicates_excluded(monkeypatch, changes):
    setup_catalog(monkeypatch)
    result = module.current_catalog_candidates(
        Client([{"items": [row(1, **changes)] * 120 + [row(6)]}]), clock=lambda: NOW
    )
    assert [x.broker_instrument_id for x in result] == ["6"]


def test_missing_response_has_no_historical_fallback(monkeypatch):
    setup_catalog(monkeypatch)
    assert module.current_catalog_candidates(Client([{"items": []}]), clock=lambda: NOW) == ()


def test_invalid_response_fails_closed(monkeypatch):
    setup_catalog(monkeypatch)
    with pytest.raises(ValueError, match="PAGE_INVALID"):
        module.current_catalog_candidates(Client([{"error": "unavailable"}]), clock=lambda: NOW)


def test_freshness_unchanged(monkeypatch):
    setup_catalog(monkeypatch, count=1)
    times = iter([NOW, NOW + timedelta(minutes=16)])
    with pytest.raises(ValueError, match="EXPIRED"):
        module.current_catalog_candidates(
            Client([{"items": [row(1)]}]), clock=lambda: next(times), live_get_cap=1
        )
