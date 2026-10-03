"""Persistent cooldown for read-only eToro catalog bootstrap passes."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite

DEFAULT_COOLDOWN_SECONDS = 15 * 60


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo is not None else None


def persisted_cooldown(checkpoint: Mapping[str, object]) -> tuple[datetime | None, int]:
    deadline = _timestamp(checkpoint.get("retry_not_before"))
    attempts = checkpoint.get("rate_limit_attempts", 0)
    attempts = max(0, attempts) if isinstance(attempts, int) else 0
    if deadline is not None:
        return deadline, max(1, attempts)
    if "retry_not_before" in checkpoint and checkpoint["retry_not_before"] is None:
        return None, attempts
    # Older checkpoints only recorded the 429 on individual instruments.
    # Respect their last observation instead of immediately probing again.
    records = checkpoint.get("records", [])
    observed = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict) or record.get("status") != "ERROR_RETRYABLE":
            continue
        error = record.get("error")
        if record.get("reason") != "RATE_LIMITED" and not (
            isinstance(error, dict) and str(error.get("status")) == "429"
        ):
            continue
        timestamp = _timestamp(record.get("updated_at"))
        if timestamp is not None:
            observed.append(timestamp)
    if observed:
        return max(observed) + timedelta(seconds=DEFAULT_COOLDOWN_SECONDS), max(1, attempts)
    return None, 0


def next_cooldown(
    headers: Mapping[str, str], *, observed_at: datetime, attempts: int
) -> tuple[datetime, str]:
    """Honor provider seconds/HTTP-date; use bounded backoff when absent/invalid."""
    normalized = {key.casefold(): value for key, value in headers.items()}
    raw = normalized.get("retry-after")
    if raw is not None:
        try:
            seconds = float(raw)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(raw)
                reference = (
                    parsedate_to_datetime(normalized["date"])
                    if "date" in normalized
                    else observed_at
                )
                seconds = (deadline - reference).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = float("nan")
        if isfinite(seconds) and seconds >= 0:
            try:
                return observed_at + timedelta(seconds=max(1, seconds)), "PROVIDER_RETRY_AFTER"
            except OverflowError:
                pass
    seconds = DEFAULT_COOLDOWN_SECONDS * (2 ** min(max(attempts - 1, 0), 2))
    return observed_at + timedelta(seconds=seconds), "LOCAL_BACKOFF"
