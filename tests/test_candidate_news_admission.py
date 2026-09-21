from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.orchestration.active_runtime import _candidate_news_available


@pytest.mark.parametrize(
    'status,freshness,timestamp,expected',
    [
        ('AVAILABLE', 'NEWS_FRESH', '2026-09-13T06:00:00Z', True),
        ('PARTIAL', 'NEWS_FRESH', '2026-09-13T06:00:00Z', True),
        ('PROVIDER_UNAVAILABLE', 'NEWS_FRESH', '2026-09-13T06:00:00Z', False),
        ('AVAILABLE', 'NEWS_STALE', '2026-09-13T06:00:00Z', False),
        ('AVAILABLE', 'NEWS_FRESH', '2026-09-14T06:00:00Z', False),
        ('AVAILABLE', 'NEWS_FRESH', None, False),
        ('AVAILABLE', 'NEWS_FRESH', 'invalid', False),
        ('AVAILABLE', 'NEWS_FRESH', '2026-09-13T06:00:00', False),
    ],
)
def test_candidate_requires_fresh_causal_evidence(status, freshness, timestamp, expected):
    cycle = SimpleNamespace(
        news_provider_status=status,
        news_cutoff_timestamp=datetime(2026, 9, 13, 7, tzinfo=UTC),
        news_asset_contexts={'ABC': {
            'freshness': freshness,
            'latest_material_event_timestamp': timestamp,
        }},
    )
    assert _candidate_news_available(cycle, 'ABC') is expected
    assert not _candidate_news_available(cycle, 'OTHER')
