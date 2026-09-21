import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.brokers.etoro import live_candidates
from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse
from app.brokers.etoro.mapping import map_instrument_resolution
from app.data.runtime import _etoro_bootstrap_instrument
from app.domain.enums import MarketStatus
from app.orchestration.session_state import _apply_state, session_state_for
from tests.test_live_catalog_candidates import row, setup_catalog


def test_real_request_contract_restores_existing_open_interpretation(monkeypatch):
    setup_catalog(monkeypatch)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    payload_row = row(1, isOpen=False, isExchangeOpen=True)
    old = map_instrument_resolution({"items": [payload_row]}, symbol="S1", as_of=now)
    assert old.market_status is MarketStatus.OPEN

    class Search:
        calls = []

        def request(self, method, url, headers, body=None):
            assert method == "GET"
            query = parse_qs(urlparse(url).query)
            self.calls.append(query)
            # eToro ignores filters that do not name indexed instrument fields.
            items = [payload_row] if query.get("instrumentId") == ["1"] else [row(225033)]
            return HttpResponse(200, {}, json.dumps({"items": items}).encode())

    transport = Search()
    client = EtoroReadClient(
        EtoroCredentials(api_key="test", user_key="test"), DisciplinedHttpClient(transport)
    )
    result = live_candidates.current_catalog_candidates(client, clock=lambda: now, live_get_cap=1)
    assert transport.calls[0]["instrumentId"] == ["1"]
    assert "instrumentIds" not in transport.calls[0]
    assert len(result) == 1
    assert result[0].market_status == old.market_status
    assert result[0].tradeable == old.is_currently_tradable


def test_undocumented_bulk_search_cannot_be_reintroduced():
    class Forbidden:
        def request(self, *args, **kwargs):
            raise AssertionError("no request for invalid bulk filter")

    client = EtoroReadClient(
        EtoroCredentials(api_key="test", user_key="test"), DisciplinedHttpClient(Forbidden())
    )
    with pytest.raises(ValueError, match="exactly one"):
        client.session_catalog_ids((1, 2))


@pytest.mark.parametrize("is_open", [True, False, None])
@pytest.mark.parametrize("exchange_open", [True, False, None])
@pytest.mark.parametrize("tradable", [True, False, None])
@pytest.mark.parametrize("buy_enabled", [True, False, None])
def test_same_payload_session_enrichment_and_current_selection_agree(
    monkeypatch,
    is_open,
    exchange_open,
    tradable,
    buy_enabled,
):
    setup_catalog(monkeypatch, first_type=6)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    payload = row(
        1,
        isOpen=is_open,
        isExchangeOpen=exchange_open,
        isCurrentlyTradable=tradable,
        isBuyEnabled=buy_enabled,
    )
    resolved = map_instrument_resolution({"items": [payload]}, symbol="S1", as_of=now)
    instrument = _etoro_bootstrap_instrument(
        {"instrumentID": 1, "symbolFull": "S1", "instrumentTypeID": 10},
        snapshot_id="test",
        now=now,
    )
    # Existing enrichment interpretation, before the catalog-selection refactor.
    enriched = _apply_state(
        instrument,
        {
            "session_state": session_state_for(
                market_status=resolved.market_status, tradable=resolved.is_currently_tradable
            ),
            "is_currently_tradable": resolved.is_currently_tradable,
            "is_buy_enabled": resolved.is_buy_enabled,
            "observed_at": now.isoformat(),
        },
    )
    previous_verdict = (
        enriched.market_status is MarketStatus.OPEN
        and enriched.tradeable is True
        and enriched.buy_allowed is True
    )

    class Client:
        def session_catalog_ids(self, ids):
            assert ids == (1,)
            return {"items": [payload]}

    current = live_candidates.current_catalog_candidates(
        Client(),
        clock=lambda: now,
        live_get_cap=1,
    )
    assert bool(current) == previous_verdict
