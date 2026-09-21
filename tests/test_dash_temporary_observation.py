from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import AssetClass, Currency
from app.domain.universe import UniversalInstrument
from app.orchestration.session_state import SESSION_SOURCE, partition_temporarily_observed_dash

NOW = datetime(2026, 9, 13, 18, tzinfo=UTC)


@pytest.mark.parametrize("change,excluded", [
    ({}, True),
    ({"is_buy_enabled": True}, False),
    ({"is_buy_enabled": None}, False),
    ({"error_class": "HTTP_ERROR"}, False),
    ({"source": "other"}, False),
    ({"expires_at": NOW.isoformat()}, False),
    ({"observed_at": (NOW + timedelta(seconds=1)).isoformat()}, False),
    ({"expires_at": "invalid"}, False),
])
def test_hold_requires_fresh_explicit_evidence_and_releases_when_enabled(change, excluded):
    dash = UniversalInstrument(broker="etoro", broker_instrument_id="100004",
        symbol="DASH", asset_class=AssetClass.CRYPTO, currency=Currency.USD,
        metadata_timestamp=NOW)
    other = dash.model_copy(update={"broker_instrument_id": "100000", "symbol": "BTC"})
    state = {"source": SESSION_SOURCE, "observed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "is_buy_enabled": False, "error_class": "CRYPTO_BUY_DISABLED", **change}
    included, observed = partition_temporarily_observed_dash(
        (dash, other), states={"100004": state, "100000": state}, as_of=NOW)
    assert observed == ((dash,) if excluded else ())
    assert included == ((other,) if excluded else (dash, other))
    assert partition_temporarily_observed_dash((dash,), states={}, as_of=NOW) == ((dash,), ())
