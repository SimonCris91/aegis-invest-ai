"""Focused SQLite claim, fencing, and atomic-cycle primitive tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.storage.sqlite import (
    ActiveClaimError,
    ActiveOrchestrationClaim,
    SqliteRecordStore,
)

BASE = datetime(2026, 8, 31, 12, tzinfo=UTC)


def _claim(
    store: SqliteRecordStore, owner: str, *, acquired_at: datetime = BASE
) -> ActiveOrchestrationClaim | None:
    return store.acquire_active_claim(
        claim_name="active-scanner",
        owner_token=owner,
        acquired_at=acquired_at,
        expires_at=acquired_at + timedelta(minutes=10),
    )


def _finalize(
    store: SqliteRecordStore,
    claim: ActiveOrchestrationClaim,
    cycle_id: str,
    watermarks: dict[tuple[str, str], datetime],
) -> bool:
    return store.finalize_active_cycle(
        claim=claim,
        cycle_id=cycle_id,
        scan_cycle_timestamp=BASE,
        payload={"cycle_id": cycle_id, "scan_cycle_timestamp": BASE.isoformat()},
        watermarks=watermarks,
        finalized_at=BASE + timedelta(minutes=1),
    )


def test_claim_is_acquired_and_second_connection_is_blocked(tmp_path: Path) -> None:
    path = tmp_path / "claims.sqlite3"
    first_store = SqliteRecordStore(path)
    second_store = SqliteRecordStore(path)

    claim = _claim(first_store, "owner-a")

    assert claim is not None
    assert _claim(second_store, "owner-b") is None


def test_expired_claim_is_reclaimed_with_new_generation(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    first = _claim(store, "owner-a")
    second = _claim(store, "owner-b", acquired_at=BASE + timedelta(minutes=11))

    assert first is not None
    assert second is not None
    assert second.generation == first.generation + 1


def test_valid_owner_and_generation_can_release_claim(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    claim = _claim(store, "owner-a")
    assert claim is not None

    assert store.release_active_claim(
        claim_name=claim.claim_name,
        owner_token=claim.owner_token,
        generation=claim.generation,
        released_at=BASE + timedelta(minutes=1),
    )
    assert store.active_claim(claim.claim_name) is None


def test_managed_demo_exposure_requires_durable_amount_identity(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "exposure.sqlite3")

    assert store.managed_demo_exposure_eur() == Decimal("0")
    assert store.reserve_demo_submission(
        "aegis-demo-pilot:cycle:TEST:OPEN",
        {"amount_eur": "12.50", "instrument_id": 123, "account_currency": "EUR"},
    )
    assert store.managed_demo_exposure_eur() == Decimal("12.50")

    assert store.reserve_demo_submission(
        "aegis-demo-pilot:cycle:OTHER:OPEN",
        {"instrument_id": 456, "account_currency": "EUR"},
    )
    assert store.managed_demo_exposure_eur() is None


def test_managed_demo_exposure_reads_reconciled_usd_positions_without_fx_guessing(
    tmp_path: Path,
) -> None:
    from app.domain.enums import Currency

    store = SqliteRecordStore(tmp_path / "usd-exposure.sqlite3")
    assert store.reserve_demo_submission(
        "aegis-usd-order-123456",
        {
            "amount_account_currency": "500",
            "executed_exposure_account_currency": "499.98",
            "instrument_id": 321,
            "account_currency": "USD",
            "action": "OPEN",
        },
    )
    store.update_demo_submission("aegis-usd-order-123456", "FILLED", {})

    assert store.managed_demo_open_exposure(Currency.USD) == Decimal("499.98")
    assert store.managed_demo_exposure(Currency.USD) == Decimal("499.98")
    assert store.managed_demo_exposure_eur() is None


def test_legacy_demo_order_without_currency_is_not_assumed_to_be_eur(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "legacy-currency.sqlite3")
    assert store.reserve_demo_submission(
        "legacy-demo-order",
        {"amount_eur": "87659.69", "instrument_id": 123, "action": "OPEN"},
    )
    store.update_demo_submission("legacy-demo-order", "FILLED", {})

    assert store.managed_demo_exposure_eur() is None


def _submission(
    store: SqliteRecordStore,
    key: str,
    state: str,
    *,
    instrument_id: int = 123,
    amount: str = "100",
    action: str = "OPEN",
) -> None:
    assert store.reserve_demo_submission(
        key,
        {
            "amount_eur": amount,
            "instrument_id": instrument_id,
            "action": action,
            "account_currency": "EUR",
        },
    )
    store.update_demo_submission(key, state, {})


def test_filled_open_consumes_and_filled_close_releases_current_budget(
    tmp_path: Path,
) -> None:
    store = SqliteRecordStore(tmp_path / "lifecycle.sqlite3")
    _submission(store, "aegis-open-123456", "FILLED")
    assert store.managed_demo_open_exposure_eur() == Decimal("100")
    _submission(
        store,
        "aegis-close-123456",
        "FILLED",
        amount="100",
        action="CLOSE",
    )
    assert store.managed_demo_exposure_eur() == Decimal("0")


def test_partial_close_releases_only_the_remaining_open_exposure(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "partial.sqlite3")
    _submission(store, "aegis-open-234567", "FILLED")
    _submission(
        store,
        "aegis-reduce-234567",
        "FILLED",
        amount="40",
        action="REDUCE",
    )
    assert store.managed_demo_open_exposure_eur() == Decimal("60")


@pytest.mark.parametrize("terminal_state", ["REJECTED", "CANCELLED"])
def test_rejected_or_cancelled_unfilled_order_does_not_consume_budget(
    tmp_path: Path, terminal_state: str
) -> None:
    store = SqliteRecordStore(tmp_path / f"{terminal_state.lower()}.sqlite3")
    _submission(store, f"aegis-{terminal_state.lower()}-1234", terminal_state)
    assert store.managed_demo_exposure_eur() == Decimal("0")


def test_unknown_submission_remains_reserved_until_reconciled(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "unknown.sqlite3")
    _submission(store, "aegis-unknown-123456", "UNKNOWN")
    assert store.managed_demo_open_exposure_eur() == Decimal("0")
    assert store.managed_demo_reserved_capital_eur() == Decimal("100")
    assert store.managed_demo_exposure_eur() == Decimal("100")

    store.update_demo_submission("aegis-unknown-123456", "CANCELLED", {})
    assert store.managed_demo_exposure_eur() == Decimal("0")


def test_historical_closed_trades_do_not_exhaust_current_authorized_capital(
    tmp_path: Path,
) -> None:
    store = SqliteRecordStore(tmp_path / "history.sqlite3")
    _submission(store, "aegis-old-open-1234", "FILLED", amount="500")
    _submission(
        store,
        "aegis-old-close-1234",
        "FILLED",
        amount="500",
        action="CLOSE",
    )
    assert store.managed_demo_exposure_eur() == Decimal("0")

    # Broker positions that have no Aegis durable identity are not counted.
    assert store.managed_demo_open_exposure_eur() == Decimal("0")


def test_released_claim_can_be_acquired_again(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    first = _claim(store, "owner-a")
    assert first is not None
    assert store.release_active_claim(
        claim_name=first.claim_name,
        owner_token=first.owner_token,
        generation=first.generation,
        released_at=BASE + timedelta(minutes=1),
    )

    second = _claim(store, "owner-b", acquired_at=BASE + timedelta(minutes=2))
    assert second is not None
    assert second.generation == first.generation + 1


@pytest.mark.parametrize(
    ("owner_token", "generation"),
    (("owner-stale", 1), ("owner-a", 99)),
)
def test_wrong_owner_or_generation_cannot_release_claim(
    tmp_path: Path, owner_token: str, generation: int
) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    claim = _claim(store, "owner-a")
    assert claim is not None

    assert not store.release_active_claim(
        claim_name=claim.claim_name,
        owner_token=owner_token,
        generation=generation,
        released_at=BASE + timedelta(minutes=1),
    )
    assert store.active_claim(claim.claim_name) is not None


def test_stale_owner_cannot_release_after_reclaim(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    first = _claim(store, "owner-a")
    second = _claim(store, "owner-b", acquired_at=BASE + timedelta(minutes=11))
    assert first is not None and second is not None

    assert not store.release_active_claim(
        claim_name=first.claim_name,
        owner_token=first.owner_token,
        generation=first.generation,
        released_at=BASE + timedelta(minutes=12),
    )
    assert store.active_claim(first.claim_name) == second


def test_release_does_not_change_cycle_or_watermark_state(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    claim = _claim(store, "owner-a")
    assert claim is not None
    watermarks = {("AAPL", "1H"): BASE}
    assert _finalize(store, claim, "cycle-a", watermarks)

    released = _claim(store, "owner-b", acquired_at=BASE + timedelta(minutes=11))
    assert released is not None
    before_cycles = store.accepted_active_cycles()
    before_watermarks = store.active_cycle_watermarks()
    assert store.release_active_claim(
        claim_name=released.claim_name,
        owner_token=released.owner_token,
        generation=released.generation,
        released_at=BASE + timedelta(minutes=12),
    )

    assert store.accepted_active_cycles() == before_cycles
    assert store.active_cycle_watermarks() == before_watermarks
    assert len(store.list("active-intelligence-cycle")) == 1


def test_two_connections_release_then_second_owner_acquires(tmp_path: Path) -> None:
    path = tmp_path / "claims.sqlite3"
    first_store = SqliteRecordStore(path)
    second_store = SqliteRecordStore(path)
    first = _claim(first_store, "owner-a")
    assert first is not None

    assert first_store.release_active_claim(
        claim_name=first.claim_name,
        owner_token=first.owner_token,
        generation=first.generation,
        released_at=BASE + timedelta(minutes=1),
    )
    second = _claim(second_store, "owner-b", acquired_at=BASE + timedelta(minutes=2))
    assert second is not None


def test_releasing_already_released_claim_returns_false(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    claim = _claim(store, "owner-a")
    assert claim is not None
    assert store.release_active_claim(
        claim_name=claim.claim_name,
        owner_token=claim.owner_token,
        generation=claim.generation,
        released_at=BASE + timedelta(minutes=1),
    )
    assert not store.release_active_claim(
        claim_name=claim.claim_name,
        owner_token=claim.owner_token,
        generation=claim.generation,
        released_at=BASE + timedelta(minutes=2),
    )


def test_stale_owner_cannot_finalize_after_reclaim(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "claims.sqlite3")
    first = _claim(store, "owner-a")
    second = _claim(store, "owner-b", acquired_at=BASE + timedelta(minutes=11))

    assert first is not None and second is not None
    with pytest.raises(ActiveClaimError):
        _finalize(store, first, "cycle-a", {("AAPL", "1H"): BASE})

    assert store.accepted_active_cycles() == {}
    assert store.active_cycle_watermarks() == {}


def test_finalize_atomically_persists_cycle_timestamp_and_asset_watermarks(
    tmp_path: Path,
) -> None:
    store = SqliteRecordStore(tmp_path / "cycles.sqlite3")
    claim = _claim(store, "owner-a")
    assert claim is not None
    watermarks = {
        ("AAPL", "1H"): BASE,
        ("BTC", "1H"): BASE + timedelta(minutes=1),
    }

    assert _finalize(store, claim, "cycle-a", watermarks)
    assert store.accepted_active_cycles() == {"cycle-a": BASE}
    assert store.active_cycle_watermarks() == watermarks
    assert store.active_claim("active-scanner") is None
    assert len(store.list("active-intelligence-cycle")) == 1


def test_watermarks_are_asset_specific_and_cannot_regress(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "cycles.sqlite3")
    first = _claim(store, "owner-a")
    assert first is not None
    assert _finalize(store, first, "cycle-a", {("AAPL", "1H"): BASE})

    second = _claim(store, "owner-b", acquired_at=BASE + timedelta(minutes=11))
    assert second is not None
    with pytest.raises(ValueError, match="cannot regress"):
        _finalize(
            store,
            second,
            "cycle-b",
            {("AAPL", "1H"): BASE - timedelta(minutes=1), ("BTC", "1H"): BASE},
        )

    assert store.accepted_active_cycles() == {"cycle-a": BASE}
    assert store.active_cycle_watermarks() == {("AAPL", "1H"): BASE}


@pytest.mark.parametrize("failure_point", ("record", "watermark", "commit"))
def test_any_finalize_failure_rolls_back_all_cycle_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    store = SqliteRecordStore(tmp_path / f"{failure_point}.sqlite3")
    claim = _claim(store, "owner-a")
    assert claim is not None
    if failure_point == "record":
        monkeypatch.setattr(store, "_insert_operational_record", _raise_failure)
    elif failure_point == "watermark":
        monkeypatch.setattr(store, "_upsert_active_watermark", _raise_failure)
    else:
        monkeypatch.setattr(store, "_commit_transaction", _raise_failure)

    with pytest.raises(RuntimeError, match="injected failure"):
        _finalize(store, claim, "cycle-a", {("AAPL", "1H"): BASE})

    assert store.accepted_active_cycles() == {}
    assert store.active_cycle_watermarks() == {}
    assert store.list("active-intelligence-cycle") == ()
    assert store.active_claim("active-scanner") is not None


def test_duplicate_accepted_cycle_is_rejected_without_partial_state(tmp_path: Path) -> None:
    store = SqliteRecordStore(tmp_path / "cycles.sqlite3")
    first = _claim(store, "owner-a")
    assert first is not None
    assert _finalize(store, first, "cycle-a", {("AAPL", "1H"): BASE})
    second = _claim(store, "owner-b", acquired_at=BASE + timedelta(minutes=11))
    assert second is not None

    assert not _finalize(store, second, "cycle-a", {("BTC", "1H"): BASE})
    assert store.accepted_active_cycles() == {"cycle-a": BASE}
    assert store.active_cycle_watermarks() == {("AAPL", "1H"): BASE}


def test_two_independent_connections_have_one_claim_winner(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite3"
    first_store = SqliteRecordStore(path)
    second_store = SqliteRecordStore(path)

    first = _claim(first_store, "owner-a")
    second = _claim(second_store, "owner-b")

    assert (first is None) != (second is None)


def _raise_failure(*args: object, **kwargs: object) -> None:
    raise RuntimeError("injected failure")
