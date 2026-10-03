from datetime import UTC, datetime, timedelta

import pytest

from app.data.bootstrap_retry import next_cooldown, persisted_cooldown

NOW = datetime(2026, 9, 30, 14, tzinfo=UTC)


@pytest.mark.parametrize(
    "headers, seconds, source",
    [
        ({"ReTrY-AfTeR": "120"}, 120, "PROVIDER_RETRY_AFTER"),
        ({"Retry-After": "0"}, 1, "PROVIDER_RETRY_AFTER"),
        ({"retry-after": "Wed, 30 Sep 2026 14:02:00 GMT"}, 120, "PROVIDER_RETRY_AFTER"),
        (
            {
                "retry-after": "Wed, 30 Sep 2026 12:02:00 GMT",
                "date": "Wed, 30 Sep 2026 12:00:00 GMT",
            },
            120,
            "PROVIDER_RETRY_AFTER",
        ),
        ({}, 900, "LOCAL_BACKOFF"),
        ({"Retry-After": "-10"}, 900, "LOCAL_BACKOFF"),
        ({"Retry-After": "NaN"}, 900, "LOCAL_BACKOFF"),
        ({"Retry-After": "inf"}, 900, "LOCAL_BACKOFF"),
        ({"Retry-After": "1e100"}, 900, "LOCAL_BACKOFF"),
        ({"Retry-After": "invalid"}, 900, "LOCAL_BACKOFF"),
    ],
)
def test_retry_deadline(headers: dict[str, str], seconds: int, source: str) -> None:
    deadline, reason = next_cooldown(headers, observed_at=NOW, attempts=1)
    assert deadline == NOW + timedelta(seconds=seconds)
    assert reason == source


def test_backoff_grows_without_ignoring_provider_deadline() -> None:
    assert next_cooldown({}, observed_at=NOW, attempts=2)[0] == NOW + timedelta(minutes=30)
    assert next_cooldown({}, observed_at=NOW, attempts=30)[0] == NOW + timedelta(hours=1)
    assert next_cooldown({"Retry-After": "7200"}, observed_at=NOW, attempts=30)[0] == (
        NOW + timedelta(hours=2)
    )


def test_legacy_checkpoint_cooldown_and_explicit_reset() -> None:
    checkpoint = {
        "records": [
            {
                "status": "ERROR_RETRYABLE",
                "reason": "HTTP_ERROR",
                "error": {"status": "429"},
                "updated_at": NOW.isoformat(),
            },
        ]
    }
    assert persisted_cooldown(checkpoint) == (NOW + timedelta(minutes=15), 1)
    assert persisted_cooldown({**checkpoint, "retry_not_before": None}) == (None, 0)
    assert persisted_cooldown({"records": None}) == (None, 0)
