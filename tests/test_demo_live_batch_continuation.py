from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.brokers.etoro import demo_execution
from app.brokers.etoro.auth import EtoroCredentials
from app.config.loader import load_config
from app.domain.enums import AssetClass, Currency, MarketStatus
from app.domain.market import MarketQuote
from app.domain.universe import UniversalInstrument
from app.storage.sqlite import SqliteRecordStore

NOW = datetime(2026, 9, 5, tzinfo=UTC)


def _candidate(index: int) -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id=str(index),
        symbol=f"S{index}",
        asset_class=AssetClass.CRYPTO,
        currency=Currency.USD,
        market_status=MarketStatus.OPEN,
        tradeable=True,
        buy_allowed=True,
        metadata_timestamp=NOW,
    )


class _QuoteClient:
    def quote_with_diagnostics(self, instrument_id: int, symbol: str):
        quote = MarketQuote(
            instrument_id=instrument_id,
            symbol=symbol,
            price=Decimal("10"),
            as_of=NOW,
            currency=Currency.USD,
            source="test",
        )
        return quote, [NOW.isoformat()], NOW


def _config():
    return load_config(
        {
            "AEGIS_OPERATING_MODE": "ETORO_DEMO",
            "ETORO_API_ENABLED": "true",
            "ETORO_DEMO_EXECUTION_ENABLED": "true",
            "AEGIS_BROKER_EXECUTION_MODE": "DEMO_EXECUTION",
            "ETORO_DEMO_SMOKE_TEST_OPT_IN": "true",
            "AEGIS_AUTHORIZED_CAPITAL_EUR": "2000",
        }
    )


def _hold_result():
    return {
        "status": "PREFLIGHT_BLOCKED",
        "failure_reason": "HOLD",
        "failed_check": "aegis_agent",
        "demo_submission_attempts": 0,
        "demo_write_performed": False,
        "real_write_performed": False,
    }


def _selector(batches, offsets):
    def select(_client, *, selection_diagnostics, catalog_offset, **_kwargs):
        offsets.append(catalog_offset)
        batch = batches[catalog_offset]
        selection_diagnostics.update(
            {
                "local_prefilter_count": 51,
                "batch_catalog_count": len(batch),
                "batch_end_offset": catalog_offset + len(batch),
                "catalog_exhausted": catalog_offset == 50,
            }
        )
        return batch

    return select


def test_hold_first_batch_continues_to_next_batch_without_repeating(monkeypatch, tmp_path: Path):
    batches = {
        0: tuple(_candidate(index) for index in range(1, 51)),
        50: (_candidate(51),),
    }
    selector_offsets = []
    validation_ids = []

    def validate(_config, *, values, **_kwargs):
        validation_ids.append(values["AEGIS_ETORO_READINESS_INSTRUMENT_ID"])
        return _hold_result()

    monkeypatch.setattr(
        "app.brokers.etoro.live_candidates.current_catalog_candidates",
        _selector(batches, selector_offsets),
    )
    monkeypatch.setattr(
        demo_execution,
        "runtime_credentials",
        lambda _values: EtoroCredentials(api_key="k", user_key="u"),
    )
    monkeypatch.setattr(demo_execution, "EtoroReadClient", lambda *_args, **_kwargs: _QuoteClient())
    monkeypatch.setattr(demo_execution, "run_user_confirmed_demo_validation", validate)

    result = demo_execution.run_operational_demo_once(
        _config(),
        values={"ETORO_API_KEY": "k", "ETORO_USER_KEY": "u"},
        confirm_demo_write=True,
        store=SqliteRecordStore(tmp_path / "runtime.sqlite3"),
    )

    assert result["status"] == "NO_CANDIDATE_PASSED_PREFLIGHT"
    assert selector_offsets == [0, 50]
    assert validation_ids == [str(index) for index in range(1, 52)]
    assert result["candidates_checked"] == 51
    assert result["demo_submission_attempts"] == 0
    assert result["demo_write_performed"] is False
    assert result["real_write_performed"] is False


def test_valid_proposal_stops_after_first_successful_batch_candidate(monkeypatch, tmp_path: Path):
    batches = {
        0: tuple(_candidate(index) for index in range(1, 51)),
        50: (_candidate(51),),
    }
    selector_offsets = []
    validation_ids = []

    def validate(_config, *, values, **_kwargs):
        validation_ids.append(values["AEGIS_ETORO_READINESS_INSTRUMENT_ID"])
        if len(validation_ids) < 51:
            return _hold_result()
        return {
            "status": "DEMO_SUBMITTED",
            "demo_submission_attempts": 1,
            "demo_write_performed": True,
            "real_write_performed": False,
        }

    monkeypatch.setattr(
        "app.brokers.etoro.live_candidates.current_catalog_candidates",
        _selector(batches, selector_offsets),
    )
    monkeypatch.setattr(
        demo_execution,
        "runtime_credentials",
        lambda _values: EtoroCredentials(api_key="k", user_key="u"),
    )
    monkeypatch.setattr(demo_execution, "EtoroReadClient", lambda *_args, **_kwargs: _QuoteClient())
    monkeypatch.setattr(demo_execution, "run_user_confirmed_demo_validation", validate)

    result = demo_execution.run_operational_demo_once(
        _config(),
        values={"ETORO_API_KEY": "k", "ETORO_USER_KEY": "u"},
        confirm_demo_write=True,
        store=SqliteRecordStore(tmp_path / "runtime.sqlite3"),
    )

    assert result["status"] == "DEMO_SUBMITTED"
    assert selector_offsets == [0, 50]
    assert validation_ids == [str(index) for index in range(1, 52)]
    assert result["demo_submission_attempts"] == 1
    assert result["demo_write_performed"] is True
    assert result["real_write_performed"] is False
