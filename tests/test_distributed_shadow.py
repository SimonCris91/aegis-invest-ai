"""Distributed compute contract, replay protection and real loopback HTTP tests."""

from datetime import UTC, datetime, timedelta
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from app.distributed.coordinator import ShadowCoordinator
from app.distributed.protocol import calculate, sign, validate_payload, verify
from app.distributed.server import make_server
from app.distributed.worker import execute, run_once, validate_url

KEY = b"a" * 32  # Synthetic test key, never a broker credential.


def payload(now: datetime, asset_class: str = "EQUITY") -> dict[str, Any]:
    return {
        "symbol": "TEST",
        "asset_class": asset_class,
        "as_of": now.isoformat(),
        "bars": [
            {"closed_at": (now - timedelta(hours=21 - i)).isoformat(), "close": 100.0 + i}
            for i in range(21)
        ],
    }


@pytest.mark.parametrize("asset_class", ["EQUITY", "ETF", "CRYPTO"])
def test_multiasset_roundtrip_is_advisory_and_idempotent(asset_class: str) -> None:
    now = datetime.now(UTC)
    queue = ShadowCoordinator(KEY)
    data = payload(now, asset_class)
    job_id = queue.submit(data, now=now)
    assert queue.submit(data, now=now) == job_id
    assignment = queue.claim("pc2", now=now)
    assert assignment is not None
    assert queue.claim("other", now=now) is None
    result = execute(assignment, KEY, "pc2")
    queue.accept(result, now=now)
    queue.accept(result, now=now)
    assert queue.result(job_id, now=now) == {
        "shadow_only": True,
        "version": "ohlcv-shadow-v1",
        "metrics": calculate(data),
    }
    assert queue.claim("pc2", now=now) is None


@pytest.mark.parametrize("field", ["broker_token", "portfolio", "order", "command"])
def test_inputs_cannot_carry_secrets_portfolio_or_commands(field: str) -> None:
    data = payload(datetime.now(UTC))
    data[field] = "forbidden"
    with pytest.raises(ValueError):
        validate_payload(data)


def test_snapshot_expiry_and_lease_reassignment() -> None:
    now = datetime.now(UTC)
    queue = ShadowCoordinator(KEY)
    job_id = queue.submit(payload(now), now=now)
    first = queue.claim("pc2", now=now)
    assert first is not None
    old_result = execute(first, KEY, "pc2")
    second = queue.claim("pc3", now=now + timedelta(seconds=31))
    assert second is not None
    with pytest.raises(ValueError, match="foreign"):
        queue.accept(old_result, now=now + timedelta(seconds=31))
    assert queue.result(job_id, now=now + timedelta(seconds=121)) is None


def test_tampered_result_and_wrong_key_fail_closed() -> None:
    now = datetime.now(UTC)
    queue = ShadowCoordinator(KEY)
    queue.submit(payload(now), now=now)
    job = queue.claim("pc2", now=now)
    assert job is not None
    with pytest.raises(ValueError, match="signature"):
        verify(job, b"wrong" * 8)
    result = execute(job, KEY, "pc2")
    result["body"]["metrics"]["sma_20"] = 999.0
    with pytest.raises(ValueError, match="signature"):
        queue.accept(result, now=now)
    with pytest.raises(ValueError, match="reference"):
        queue.accept(sign(result["body"], KEY), now=now)


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf"), True])
def test_invalid_prices_are_rejected(price: float) -> None:
    data = payload(datetime.now(UTC))
    data["bars"][0]["close"] = price
    with pytest.raises(ValueError):
        validate_payload(data)


def test_future_stale_and_out_of_order_inputs() -> None:
    now = datetime.now(UTC)
    queue = ShadowCoordinator(KEY)
    for offset in (-301, 1):
        with pytest.raises(ValueError):
            queue.submit(payload(now + timedelta(seconds=offset)), now=now)
    data = payload(now)
    data["bars"][-1]["closed_at"] = (now + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        validate_payload(data)
    data = payload(now)
    data["bars"].reverse()
    with pytest.raises(ValueError):
        validate_payload(data)


def test_queue_is_bounded_and_input_snapshot_is_copied() -> None:
    now = datetime.now(UTC)
    queue = ShadowCoordinator(KEY, capacity=1)
    original = payload(now)
    queue.submit(original, now=now)
    original["bars"][0]["close"] = 500.0
    job = queue.claim("pc2", now=now)
    assert job is not None
    assert job["body"]["payload"]["bars"][0]["close"] == 100.0
    with pytest.raises(ValueError, match="capacity"):
        queue.submit(payload(now, "ETF"), now=now)


@pytest.mark.parametrize("url", ["http://remote.example", "https://a:b@host", "https://host/order"])
def test_plain_remote_http_and_credential_redirect_origins_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_url(url)


def test_nonlocal_server_cannot_accidentally_publish_without_secure_relay() -> None:
    with pytest.raises(ValueError, match="loopback"):
        make_server(("0.0.0.0", 0), ShadowCoordinator(KEY))


def test_real_http_worker_roundtrip_and_unauthenticated_access() -> None:
    now = datetime.now(UTC)
    queue = ShadowCoordinator(KEY)
    job_id = queue.submit(payload(now), now=now)
    server = make_server(("127.0.0.1", 0), queue)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(origin + "/v1/jobs?worker=pc2", timeout=3)
        assert error.value.code == 401
        assert run_once(origin, KEY, "pc2") is True
        assert queue.result(job_id, now=datetime.now(UTC)) is not None
        assert run_once(origin, KEY, "pc2") is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
