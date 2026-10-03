from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.news.relay import build_envelope, read_latest, store_envelope, verify_envelope


KEY = bytes(range(32))
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def cycle_payload():
    return {
        "scheduled_at": NOW.isoformat(),
        "news_cutoff_timestamp": NOW.isoformat(),
        "news_provider": "GOOGLE_BING_RSS",
        "news_provider_status": "PARTIAL",
        "news_events_received": 3,
        "news_events_fresh": 2,
        "news_events_material": 1,
        "news_asset_contexts": {
            "ACME": {
                "asset_class": "EQUITY",
                "freshness": "NEWS_FRESH",
                "latest_material_event_timestamp": (NOW - timedelta(minutes=2)).isoformat(),
                "material_event_count": 1,
                "event_summaries": ("earnings update",),
                "explanation": "linked source",
            },
            "EMPTY": {"freshness": "NEWS_SOURCE_UNAVAILABLE"},
        },
    }


def test_round_trip_filters_unavailable_context(tmp_path: Path):
    envelope = build_envelope(cycle_payload(), key=KEY, source_id="pc2", now=NOW)
    assert set(envelope) == {"body", "signature"}
    assert set(envelope["body"]["contexts"]) == {"ACME"}
    store_envelope(envelope, key=KEY, path=tmp_path / "relay.json", now=NOW)
    assert read_latest(key=KEY, path=tmp_path / "relay.json", now=NOW)["source_id"] == "pc2"


def test_tampered_envelope_is_rejected():
    envelope = build_envelope(cycle_payload(), key=KEY, source_id="pc2", now=NOW)
    envelope["body"]["contexts"]["ACME"]["explanation"] = "tampered"
    with pytest.raises(ValueError, match="signature"):
        verify_envelope(envelope, key=KEY, now=NOW)


def test_expired_envelope_is_not_read(tmp_path: Path):
    envelope = build_envelope(cycle_payload(), key=KEY, source_id="pc2", now=NOW)
    store_envelope(envelope, key=KEY, path=tmp_path / "relay.json", now=NOW)
    assert read_latest(key=KEY, path=tmp_path / "relay.json", now=NOW + timedelta(minutes=11)) is None
