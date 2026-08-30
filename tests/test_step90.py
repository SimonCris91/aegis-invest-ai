"""Step 9.0 active market scanner foundation tests."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import load_config
from app.data.historical.alpaca import AlpacaHistoricalMarketDataProvider
from app.data.historical.cache import HistoricalDataCache
from app.data.models import ProviderInstrumentReference
from app.data.runtime import (
    EXIT_EVIDENCE_SYMBOLS_BY_CLASS,
    build_active_scanner_1h_full_universe_sweep_report,
    build_active_scanner_1h_iex_equity_pilot_report,
    build_active_scanner_1h_pilot_report,
    build_active_scanner_observation_temporal_alignment_report,
)
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.universe import UniversalInstrument
from app.intelligence.models import FeatureQuality, MarketBar, TimeFrame
from app.main.__main__ import main
from app.scanner.active import (
    ActiveMarketScanner,
    ActiveScannerBucket,
    IntradayFreshnessStatus,
    _observation_from_candidate,
    classify_intraday_freshness,
)


def test_active_scanner_ranks_cross_asset_candidates_at_same_timestamp() -> None:
    as_of = datetime(2026, 8, 28, tzinfo=UTC)
    instruments = (
        _instrument("AAA", AssetClass.EQUITY, "101", as_of),
        _instrument("BBB", AssetClass.ETF, "102", as_of),
        _instrument("BTC", AssetClass.CRYPTO, "103", as_of),
    )
    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=instruments,
        bars_by_symbol={
            "AAA": _bars(instruments[0], as_of=as_of, drift=Decimal("0.01")),
            "BBB": _bars(instruments[1], as_of=as_of, drift=Decimal("0.002")),
            "BTC": _bars(instruments[2], as_of=as_of, drift=Decimal("0.006")),
        },
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )

    assert len(result.candidates) == 3
    assert tuple(item.rank for item in result.candidates) == (1, 2, 3)
    assert result.candidates[0].opportunity_score >= result.candidates[-1].opportunity_score
    assert {item.asset_class for item in result.candidates} == {
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.CRYPTO,
    }
    assert result.broker_write_calls == 0


def test_active_scanner_excludes_stale_data_fail_closed() -> None:
    latest = datetime(2026, 8, 1, tzinfo=UTC)
    as_of = datetime(2026, 8, 28, tzinfo=UTC)
    instrument = _instrument("AAA", AssetClass.EQUITY, "101", as_of)

    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(instrument,),
        bars_by_symbol={"AAA": _bars(instrument, as_of=latest, drift=Decimal("0.01"))},
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )

    assert result.rejected[0].bucket is ActiveScannerBucket.REJECTED
    assert "STALE" in result.rejected[0].rejection_reasons
    assert result.broker_write_calls == 0


def test_active_scanner_rejects_incomplete_data_without_trade_output() -> None:
    as_of = datetime(2026, 8, 28, tzinfo=UTC)
    instrument = _instrument("AAA", AssetClass.EQUITY, "101", as_of)

    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(instrument,),
        bars_by_symbol={"AAA": _bars(instrument, as_of=as_of, count=10)},
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )

    assert result.rejected[0].rejection_reasons == ("INSUFFICIENT_CACHED_BARS",)
    assert result.top_opportunities == ()
    assert result.broker_write_calls == 0


def test_active_scanner_monitors_existing_position_outside_top_ranking() -> None:
    as_of = datetime(2026, 8, 28, tzinfo=UTC)
    held = _instrument("HELD", AssetClass.EQUITY, "101", as_of)
    other = _instrument("OTHER", AssetClass.ETF, "102", as_of)
    portfolio = PortfolioSnapshot(
        as_of=as_of,
        currency=Currency.EUR,
        cash=Decimal("150"),
        positions=(
            Position(
                position_id="p1",
                instrument_id=101,
                symbol="HELD",
                settlement_type=SettlementType.REAL,
                units=Decimal("1"),
                average_entry_price=Decimal("40"),
                market_price=Decimal("50"),
            ),
        ),
    )

    result = ActiveMarketScanner(minimum_bars=60, top_n=1).scan(
        instruments=(held, other),
        bars_by_symbol={
            "HELD": _bars(held, as_of=as_of, drift=Decimal("-0.001")),
            "OTHER": _bars(other, as_of=as_of, drift=Decimal("0.01")),
        },
        portfolio=portfolio,
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )

    held_candidate = next(item for item in result.candidates if item.symbol == "HELD")
    assert result.existing_positions_monitored == 1
    assert held_candidate.current_position_state.startswith("LONG:")
    assert result.broker_write_calls == 0


def test_active_scanner_prevents_duplicate_symbol_timestamp_decisions() -> None:
    as_of = datetime(2026, 8, 28, tzinfo=UTC)
    first = _instrument("AAA", AssetClass.EQUITY, "101", as_of)
    duplicate = _instrument("AAA", AssetClass.EQUITY, "101", as_of)
    bars = _bars(first, as_of=as_of, drift=Decimal("0.01"))

    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(first, duplicate),
        bars_by_symbol={"AAA": bars},
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )

    assert result.duplicate_decisions_prevented == 1
    assert len(result.candidates) == 1
    assert result.broker_write_calls == 0


def test_active_scanner_calculates_eur200_capital_without_short_or_broker_write() -> None:
    as_of = datetime(2026, 8, 28, tzinfo=UTC)
    instrument = _instrument("AAA", AssetClass.EQUITY, "101", as_of)

    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(instrument,),
        bars_by_symbol={"AAA": _bars(instrument, as_of=as_of, drift=Decimal("0.01"))},
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_DAY,
        simulated_capital=Decimal("200"),
    )

    candidate = result.candidates[0]
    assert candidate.affordable_fractionally is True
    assert candidate.proposed_capital_allocation == Decimal("10.00")
    assert candidate.remaining_simulated_cash == Decimal("190.00")
    assert instrument.short_allowed is False
    assert result.broker_write_calls == 0


def test_alpaca_one_hour_timestamp_normalization_and_provider_provenance() -> None:
    as_of = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id="key",
        api_secret_key="secret",
        transport=OneHourAlpacaTransport(as_of=as_of),
    )

    bars = provider.get_bars_range(
        instrument,
        TimeFrame.ONE_HOUR,
        start=as_of - timedelta(hours=3),
        end=as_of,
        limit=10,
        max_pages=1,
    )

    assert provider.provider_name == "alpaca"
    assert TimeFrame.ONE_HOUR in provider.supported_timeframes
    assert bars[0].timestamp.tzinfo is not None
    assert bars[0].timestamp.hour == 14
    assert {bar.source for bar in bars} == {"alpaca"}


def test_one_hour_cache_incremental_updates_are_idempotent(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    cache = HistoricalDataCache(tmp_path / "cache.sqlite3")
    mapping = _mapping(instrument)
    bars = _bars(
        instrument,
        as_of=as_of,
        drift=Decimal("0.001"),
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    first = cache.upsert_bars_with_stats(
        provider="alpaca", bars=bars, fetched_at=as_of, mapping=mapping
    )
    second = cache.upsert_bars_with_stats(
        provider="alpaca", bars=bars, fetched_at=as_of, mapping=mapping
    )

    assert first["inserted"] == 70
    assert second == {"inserted": 0, "updated": 0, "unchanged": 70}
    assert cache.duplicate_timestamp_report() == ()


def test_one_hour_stale_data_is_rejected() -> None:
    as_of = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    bars = _bars(
        instrument,
        as_of=as_of - timedelta(hours=8),
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
        bars=bars,
        as_of=as_of,
        minimum_bars=60,
    )

    assert freshness is IntradayFreshnessStatus.STALE


def test_one_hour_market_closed_is_not_classified_as_data_failure() -> None:
    as_of = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    instrument = _instrument(
        "SPY", AssetClass.ETF, "3000", as_of, market_status=MarketStatus.CLOSED
    )
    bars = _bars(
        instrument,
        as_of=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(instrument,),
        bars_by_symbol={"SPY": bars},
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )

    assert result.no_trade[0].freshness == "MARKET_CLOSED"
    assert result.no_trade[0].rejection_reasons == ("MARKET_CLOSED",)
    assert result.rejected == ()
    assert result.broker_write_calls == 0


def test_one_hour_weekend_closure_remains_market_closed_not_stale() -> None:
    as_of = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    bars = _bars(
        instrument,
        as_of=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
        bars=bars,
        as_of=as_of,
        minimum_bars=60,
    )

    assert freshness is IntradayFreshnessStatus.MARKET_CLOSED


def test_one_hour_overnight_closure_remains_market_closed_not_stale() -> None:
    as_of = datetime(2026, 8, 28, 8, 30, tzinfo=UTC)
    instrument = _instrument("SPY", AssetClass.ETF, "3000", as_of)
    bars = _bars(
        instrument,
        as_of=datetime(2026, 8, 27, 20, 0, tzinfo=UTC),
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
        bars=bars,
        as_of=as_of,
        minimum_bars=60,
    )

    assert freshness is IntradayFreshnessStatus.MARKET_CLOSED


def test_one_hour_open_session_missing_expected_latest_bar_is_stale() -> None:
    as_of = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    bars = _bars(
        instrument,
        as_of=datetime(2026, 8, 28, 16, 0, tzinfo=UTC),
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
        bars=bars,
        as_of=as_of,
        minimum_bars=60,
    )

    assert freshness is IntradayFreshnessStatus.STALE


def test_one_hour_open_session_expected_latest_bar_is_fresh() -> None:
    as_of = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    expected_latest = as_of.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    bars = _bars(
        instrument,
        as_of=expected_latest,
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
        bars=bars,
        as_of=as_of,
        minimum_bars=60,
    )

    assert bars[-1].timestamp == expected_latest
    assert freshness is IntradayFreshnessStatus.FRESH


def test_active_scanner_1h_pilot_uses_four_symbol_universe(tmp_path: Path) -> None:
    as_of = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    cache = HistoricalDataCache(tmp_path / "cache.sqlite3")
    _seed_alpaca_1h_pilot_cache(cache, as_of=as_of)

    report = build_active_scanner_1h_pilot_report(
        load_config({}),
        values={
            "ALPACA_API_KEY_ID": "key",
            "ALPACA_API_SECRET_KEY": "secret",
        },
        cache=cache,
        clock=lambda: as_of,
    )

    assert report["status"] == "ACTIVE_SCANNER_1H_PILOT_READY"
    assert report["symbols_requested"] == 4
    ready_symbols = cast(tuple[str, ...], report["ready_symbols"])
    assert ready_symbols == ("AAPL", "BTC", "ETH", "SPY")
    assert report["broker_write_calls"] == 0


def test_active_scanner_1h_pilot_cli_writes_report_file(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = HistoricalDataCache(tmp_path / "work" / "market-data-cache.sqlite3")
    _seed_alpaca_1h_pilot_cache(cache, as_of=datetime(2026, 8, 30, 16, 0, tzinfo=UTC))

    assert (
        main(
            ("active-scanner-1h-pilot",),
            values={
                "ALPACA_API_KEY_ID": "key",
                "ALPACA_API_SECRET_KEY": "secret",
            },
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    report_path = tmp_path / "reports" / "step9_0a2_1h_readonly_pilot.md"

    assert payload["status"] == "ACTIVE_SCANNER_1H_PILOT_READY"
    assert payload["broker_write_calls"] == 0
    assert payload["report_path"] == str(Path("reports") / "step9_0a2_1h_readonly_pilot.md")
    assert report_path.exists()
    assert "STEP 9.0A2 1H Read-Only Data Pilot" in report_path.read_text(encoding="utf-8")


def test_active_scanner_1h_iex_pilot_requests_explicit_iex_feed(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    cache = HistoricalDataCache(tmp_path / "cache.sqlite3")
    _seed_alpaca_1h_iex_equity_cache(cache, as_of=as_of)
    transport = OneHourAlpacaTransport(as_of=as_of, expected_feed="iex", bar_count=120)

    report = build_active_scanner_1h_iex_equity_pilot_report(
        load_config({}),
        values={
            "ALPACA_API_KEY_ID": "key",
            "ALPACA_API_SECRET_KEY": "secret",
        },
        cache=cache,
        transport=transport,
        clock=lambda: as_of,
    )

    assert report["status"] == "ACTIVE_SCANNER_1H_IEX_EQUITY_PILOT_READY"
    assert report["requested_feed"] == "iex"
    ready_symbols = cast(tuple[str, ...], report["ready_symbols"])
    acquisition = cast(tuple[dict[str, object], ...], report["acquisition"])
    assert ready_symbols == ("AAPL", "SPY")
    assert acquisition[0]["provider_feed_provenance"] == "ALPACA_IEX"
    assert report["broker_write_calls"] == 0
    assert all("feed=iex" in url for url in transport.urls if "/v2/stocks/bars" in url)


def test_active_scanner_1h_full_universe_sweep_uses_iex_and_crypto_provenance(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    cache = HistoricalDataCache(tmp_path / "cache.sqlite3")
    _seed_alpaca_1h_full_universe_cache(cache, as_of=as_of)
    transport = OneHourAlpacaTransport(as_of=as_of, expected_feed="iex", bar_count=120)

    report = build_active_scanner_1h_full_universe_sweep_report(
        load_config({}),
        values={
            "ALPACA_API_KEY_ID": "key",
            "ALPACA_API_SECRET_KEY": "secret",
        },
        cache=cache,
        transport=transport,
        clock=lambda: as_of,
    )

    expected_symbols = sum(len(symbols) for symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.values())
    ready_symbols = cast(tuple[str, ...], report["ready_symbols"])
    acquisition = cast(tuple[dict[str, object], ...], report["acquisition"])
    assert report["status"] == "ACTIVE_SCANNER_1H_READY"
    assert report["report_title"] == "STEP 9.0A2D 1H Full-Universe Readiness Sweep"
    assert report["symbols_requested"] == expected_symbols
    assert report["symbols_1h_ready"] == expected_symbols
    assert report["repeated_intraday_shadow_scan_ready"] is True
    assert report["broker_write_calls"] == 0
    assert all("feed=iex" in url for url in transport.urls if "/v2/stocks/bars" in url)
    assert ready_symbols == tuple(sorted(ready_symbols))
    crypto_rows = [row for row in acquisition if row["asset_class"] == "CRYPTO"]
    assert crypto_rows and all(
        row["provider_feed_provenance"] == "ALPACA_CRYPTO_US" for row in crypto_rows
    )


def test_alpaca_provider_adapter_consumes_three_pages_and_verifies_pagination() -> None:
    as_of = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    transport = ThreePageAlpacaTransport(as_of=as_of, expected_feed="iex")
    provider = AlpacaHistoricalMarketDataProvider(
        api_key_id="alpaca-key-id",
        api_secret_key="alpaca-secret-key",
        transport=transport,
        stock_feed="iex",
        max_pages=3,
    )

    bars = provider.get_bars_range(
        instrument,
        TimeFrame.ONE_HOUR,
        start=as_of - timedelta(hours=6),
        end=as_of,
        limit=2,
        max_pages=3,
    )

    assert len(transport.urls) == 3
    assert provider.last_pagination_state == {
        "pagination_requested": True,
        "pagination_token_observed": True,
        "second_page_fetched": True,
        "pagination_verified": True,
        "pagination_truncated": False,
        "pages_fetched": 3,
    }
    assert len(bars) == 6
    assert [bar.timestamp for bar in bars] == sorted(bar.timestamp for bar in bars)
    assert len({bar.timestamp for bar in bars}) == 6
    assert all("feed=iex" in url for url in transport.urls)


def _seed_alpaca_1h_iex_equity_cache(
    cache: HistoricalDataCache,
    *,
    as_of: datetime,
) -> None:
    for symbol, instrument_id in (("AAPL", "1001"), ("SPY", "3000")):
        instrument = UniversalInstrument(
            broker="etoro",
            broker_instrument_id=instrument_id,
            symbol=symbol,
            display_name=f"{symbol} Pilot Asset",
            asset_class=AssetClass.EQUITY,
            currency=Currency.USD,
            exchange="TEST",
            market_status=MarketStatus.OPEN,
            short_allowed=False,
            leverage_available=False,
            max_leverage=Decimal("1"),
            settlement_type=SettlementType.REAL,
            minimum_order_value=Decimal("1"),
            fractional_supported=True,
            metadata_timestamp=as_of,
        )
        cache.upsert_bars(
            provider="etoro",
            bars=_bars(instrument, as_of=as_of, count=1, timeframe=TimeFrame.ONE_DAY),
            fetched_at=as_of,
            mapping=ProviderInstrumentReference(
                provider="etoro",
                provider_symbol=symbol,
                broker="etoro",
                broker_symbol=symbol,
                broker_instrument_id=instrument_id,
                exchange=instrument.exchange,
                asset_class=AssetClass.EQUITY,
                currency=Currency.USD,
                mapping_confidence=Decimal("1"),
                mapping_source="seeded test eToro mapping",
                verified=True,
            ),
        )


def test_one_hour_crypto_uses_247_freshness_semantics() -> None:
    as_of = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    instrument = _instrument("BTC", AssetClass.CRYPTO, "100000", as_of)
    bars = _bars(
        instrument,
        as_of=as_of - timedelta(hours=5),
        count=70,
        timeframe=TimeFrame.ONE_HOUR,
    )

    freshness = classify_intraday_freshness(
        instrument=instrument,
        timeframe=TimeFrame.ONE_HOUR,
        bars=bars,
        as_of=as_of,
        minimum_bars=60,
    )

    assert freshness is IntradayFreshnessStatus.DELAYED


def test_active_scanner_alignment_allows_cross_asset_causal_timestamps() -> None:
    as_of = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    aapl = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    btc = _instrument("BTC", AssetClass.CRYPTO, "100000", as_of)
    aapl_bars = _bars(
        aapl,
        as_of=as_of - timedelta(hours=1),
        count=60,
        timeframe=TimeFrame.ONE_HOUR,
    )
    btc_bars = tuple(
        bar.model_copy(update={"timestamp": bar.timestamp + timedelta(minutes=30)})
        for bar in _bars(
            btc,
            as_of=as_of - timedelta(hours=1),
            count=60,
            timeframe=TimeFrame.ONE_HOUR,
        )
    )

    scanner = ActiveMarketScanner(minimum_bars=60)
    snapshot = scanner.build_observation_snapshot(
        instruments=(aapl, btc),
        bars_by_symbol={"AAPL": aapl_bars, "BTC": btc_bars},
        portfolio=_portfolio(as_of),
        scan_cycle_timestamp=as_of,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )

    aapl_obs = next(
        observation for observation in snapshot.entry_candidates if observation.symbol == "AAPL"
    )
    btc_obs = next(
        observation for observation in snapshot.entry_candidates if observation.symbol == "BTC"
    )

    assert aapl_obs.bar_timestamp <= as_of
    assert btc_obs.bar_timestamp <= as_of
    assert aapl_obs.bar_timestamp != btc_obs.bar_timestamp
    assert aapl_obs.eligible_for_entry_comparison is True
    assert btc_obs.eligible_for_entry_comparison is True
    assert snapshot.positions_to_manage == ()


def test_active_scanner_observation_helper_excludes_future_and_mixed_timestamp_cases() -> None:
    as_of = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)
    bars = _bars(instrument, as_of=as_of, count=60, timeframe=TimeFrame.ONE_HOUR)
    scanner = ActiveMarketScanner(minimum_bars=60)
    result = scanner.scan(
        instruments=(instrument,),
        bars_by_symbol={"AAPL": bars},
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )
    candidate = result.candidates[0]

    future_bar = bars[-1].model_copy(update={"timestamp": as_of + timedelta(hours=1)})
    future_observation = _observation_from_candidate(
        candidate=candidate,
        bars=(future_bar,),
        scan_cycle_timestamp=as_of,
    )
    assert future_observation.eligibility_reason_code.value == "FUTURE_BAR"

    mixed_candidate = candidate.model_copy(
        update={
            "provider_provenance": ("alpaca", "etoro"),
            "freshness": "STALE",
        }
    )
    mixed_observation = _observation_from_candidate(
        candidate=mixed_candidate,
        bars=bars,
        scan_cycle_timestamp=as_of,
    )
    assert mixed_observation.eligibility_reason_code.value == "MIXED_TIMESTAMP_UNSAFE"


def test_active_scanner_observation_alignment_report_materializes_temporal_alignment(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    cache = HistoricalDataCache(tmp_path / "work" / "market-data-cache.sqlite3")
    _seed_alpaca_1h_full_universe_cache(cache, as_of=as_of)

    report = build_active_scanner_observation_temporal_alignment_report(
        load_config({}),
        cache=cache,
        clock=lambda: as_of,
    )
    assert report["status"] == "ACTIVE_SCANNER_FOUNDATION_READY"
    assert report["position_independence"] is True

    monkeypatch.chdir(tmp_path)
    assert (
        main(
            ("active-scanner-observation-alignment",),
            values={},
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    report_path = tmp_path / "reports" / "step9_0a3_scanner_observation_temporal_alignment.md"

    assert payload["status"] == "ACTIVE_SCANNER_FOUNDATION_READY"
    assert payload["observation_model"] == "READY"
    assert payload["temporal_alignment"] == "READY"
    assert payload["cross_asset_snapshot"] == "READY"
    assert payload["future_bar_exclusion"] is True
    assert payload["stale_exclusion"] is True
    assert payload["mixed_timestamp_handling"] is True
    assert payload["duplicate_evaluation_prevention"] is True
    assert payload["position_independence"] is True
    assert payload["anti_lookahead_verified"] is True
    assert payload["broker_write_calls"] == 0
    assert payload["report_path"] == str(
        Path("reports") / "step9_0a3_scanner_observation_temporal_alignment.md"
    )
    assert report_path.exists()
    assert "STEP 9.0A3 Scanner Observation Temporal Alignment" in report_path.read_text(
        encoding="utf-8"
    )


def test_one_hour_insufficient_warmup_rejects_fail_closed() -> None:
    as_of = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    instrument = _instrument("AAPL", AssetClass.EQUITY, "1001", as_of)

    result = ActiveMarketScanner(minimum_bars=60).scan(
        instruments=(instrument,),
        bars_by_symbol={
            "AAPL": _bars(instrument, as_of=as_of, count=35, timeframe=TimeFrame.ONE_HOUR)
        },
        portfolio=_portfolio(as_of),
        as_of=as_of,
        timeframe=TimeFrame.ONE_HOUR,
        simulated_capital=Decimal("200"),
    )

    assert result.rejected[0].rejection_reasons == ("INSUFFICIENT_CACHED_BARS",)
    assert result.broker_write_calls == 0


def _seed_alpaca_1h_pilot_cache(cache: HistoricalDataCache, *, as_of: datetime) -> None:
    specs = (
        ("AAPL", AssetClass.EQUITY, "1001"),
        ("SPY", AssetClass.ETF, "3000"),
        ("BTC", AssetClass.CRYPTO, "100000"),
        ("ETH", AssetClass.CRYPTO, "100001"),
    )
    for symbol, asset_class, instrument_id in specs:
        instrument = UniversalInstrument(
            broker="etoro",
            broker_instrument_id=instrument_id,
            symbol=symbol,
            display_name=f"{symbol} Pilot Asset",
            asset_class=asset_class,
            currency=Currency.USD,
            exchange="TEST",
            market_status=(
                MarketStatus.CONTINUOUS_24_7
                if asset_class is AssetClass.CRYPTO
                else MarketStatus.OPEN
            ),
            short_allowed=False,
            leverage_available=False,
            max_leverage=Decimal("1"),
            settlement_type=SettlementType.REAL,
            minimum_order_value=Decimal("1"),
            fractional_supported=True,
            metadata_timestamp=as_of,
        )
        cache.upsert_bars(
            provider="etoro",
            bars=_bars(instrument, as_of=as_of, count=1, timeframe=TimeFrame.ONE_DAY),
            fetched_at=as_of,
            mapping=ProviderInstrumentReference(
                provider="etoro",
                provider_symbol=symbol,
                broker="etoro",
                broker_symbol=symbol,
                broker_instrument_id=instrument_id,
                exchange=instrument.exchange,
                asset_class=asset_class,
                currency=Currency.USD,
                mapping_confidence=Decimal("1"),
                mapping_source="seeded test eToro mapping",
                verified=True,
            ),
        )
        cache.upsert_bars(
            provider="alpaca",
            bars=_bars(instrument, as_of=as_of, count=120, timeframe=TimeFrame.ONE_HOUR),
            fetched_at=as_of,
            mapping=ProviderInstrumentReference(
                provider="alpaca",
                provider_symbol=(
                    symbol if asset_class is not AssetClass.CRYPTO else f"{symbol}/USD"
                ),
                broker="etoro",
                broker_symbol=symbol,
                broker_instrument_id=instrument_id,
                exchange=instrument.exchange,
                asset_class=asset_class,
                currency=Currency.USD,
                mapping_confidence=Decimal("1"),
                mapping_source="seeded test Alpaca 1H pilot mapping",
                verified=True,
            ),
        )


def _seed_alpaca_1h_full_universe_cache(
    cache: HistoricalDataCache,
    *,
    as_of: datetime,
) -> None:
    index = 1000
    for asset_class, symbols in EXIT_EVIDENCE_SYMBOLS_BY_CLASS.items():
        for symbol in symbols:
            instrument_id = str(index)
            index += 1
            instrument = UniversalInstrument(
                broker="etoro",
                broker_instrument_id=instrument_id,
                symbol=symbol,
                display_name=f"{symbol} Full Universe Asset",
                asset_class=asset_class,
                currency=Currency.USD,
                exchange="TEST",
                market_status=(
                    MarketStatus.CONTINUOUS_24_7
                    if asset_class is AssetClass.CRYPTO
                    else MarketStatus.OPEN
                ),
                short_allowed=False,
                leverage_available=False,
                max_leverage=Decimal("1"),
                settlement_type=SettlementType.REAL,
                minimum_order_value=Decimal("1"),
                fractional_supported=True,
                metadata_timestamp=as_of,
            )
            cache.upsert_bars(
                provider="etoro",
                bars=_bars(
                    instrument,
                    as_of=as_of,
                    count=1,
                    timeframe=TimeFrame.ONE_DAY,
                ),
                fetched_at=as_of,
                mapping=ProviderInstrumentReference(
                    provider="etoro",
                    provider_symbol=(
                        symbol if asset_class is not AssetClass.CRYPTO else f"{symbol}/USD"
                    ),
                    broker="etoro",
                    broker_symbol=symbol,
                    broker_instrument_id=instrument_id,
                    exchange=instrument.exchange,
                    asset_class=asset_class,
                    currency=Currency.USD,
                    mapping_confidence=Decimal("1"),
                    mapping_source="seeded full universe test eToro mapping",
                    verified=True,
                ),
            )
            cache.upsert_bars(
                provider="alpaca",
                bars=_bars(
                    instrument,
                    as_of=as_of,
                    count=120,
                    timeframe=TimeFrame.ONE_HOUR,
                ),
                fetched_at=as_of,
                mapping=ProviderInstrumentReference(
                    provider="alpaca",
                    provider_symbol=(
                        symbol if asset_class is not AssetClass.CRYPTO else f"{symbol}/USD"
                    ),
                    broker="etoro",
                    broker_symbol=symbol,
                    broker_instrument_id=instrument_id,
                    exchange=instrument.exchange,
                    asset_class=asset_class,
                    currency=Currency.USD,
                    mapping_confidence=Decimal("1"),
                    mapping_source="seeded full universe test Alpaca 1H mapping",
                    verified=True,
                ),
            )


def _instrument(
    symbol: str,
    asset_class: AssetClass,
    broker_instrument_id: str,
    as_of: datetime,
    *,
    market_status: MarketStatus | None = None,
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="test",
        broker_instrument_id=broker_instrument_id,
        symbol=symbol,
        display_name=f"{symbol} Test Asset",
        asset_class=asset_class,
        currency=Currency.USD,
        exchange="TEST",
        market_status=market_status
        or (
            MarketStatus.CONTINUOUS_24_7 if asset_class is AssetClass.CRYPTO else MarketStatus.OPEN
        ),
        short_allowed=False,
        leverage_available=False,
        max_leverage=Decimal("1"),
        settlement_type=SettlementType.REAL,
        minimum_order_value=Decimal("1"),
        fractional_supported=True,
        metadata_timestamp=as_of,
    )


def _bars(
    instrument: UniversalInstrument,
    *,
    as_of: datetime,
    drift: Decimal = Decimal("0.001"),
    count: int = 80,
    timeframe: TimeFrame = TimeFrame.ONE_DAY,
) -> tuple[MarketBar, ...]:
    interval = timedelta(hours=1) if timeframe is TimeFrame.ONE_HOUR else timedelta(days=1)
    start = as_of - interval * (count - 1)
    price = Decimal("100")
    bars = []
    for index in range(count):
        timestamp = start + interval * index
        open_price = price
        close = (price * (Decimal("1") + drift)).quantize(Decimal("0.0001"))
        high = max(open_price, close) * Decimal("1.01")
        low = min(open_price, close) * Decimal("0.99")
        bars.append(
            MarketBar(
                instrument=instrument,
                timestamp=timestamp,
                timeframe=timeframe,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("100000"),
                currency=Currency.USD,
                source="test-cache",
                data_quality=FeatureQuality.GOOD,
            )
        )
        price = close
    return tuple(bars)


def _portfolio(as_of: datetime) -> PortfolioSnapshot:
    return PortfolioSnapshot(as_of=as_of, currency=Currency.EUR, cash=Decimal("200"))


def _mapping(instrument: UniversalInstrument) -> ProviderInstrumentReference:
    return ProviderInstrumentReference(
        provider="alpaca",
        provider_symbol=(
            instrument.symbol
            if instrument.asset_class is not AssetClass.CRYPTO
            else f"{instrument.symbol}/USD"
        ),
        broker=instrument.broker,
        broker_symbol=instrument.symbol,
        broker_instrument_id=instrument.broker_instrument_id,
        exchange=instrument.exchange,
        asset_class=instrument.asset_class,
        currency=instrument.currency,
        mapping_confidence=Decimal("1"),
        mapping_source="test verified mapping",
        verified=True,
    )


class OneHourAlpacaTransport:
    def __init__(
        self,
        *,
        as_of: datetime,
        expected_feed: str = "sip",
        bar_count: int = 3,
    ) -> None:
        self.as_of = as_of
        self.expected_feed = expected_feed
        self.bar_count = bar_count
        self.urls: list[str] = []

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.urls.append(url)
        assert headers["APCA-API-KEY-ID"] == "key"
        assert headers["APCA-API-SECRET-KEY"] == "secret"
        query = parse_qs(urlparse(url).query)
        assert query["timeframe"] == ["1Hour"]
        symbol = query["symbols"][0]
        if symbol in {"AAPL", "SPY"}:
            assert query["feed"] == [self.expected_feed]
        bars = []
        for hour in range(self.bar_count):
            timestamp = self.as_of - timedelta(hours=self.bar_count - 1 - hour)
            bars.append(
                {
                    "t": timestamp.isoformat().replace("+00:00", "Z"),
                    "o": 100 + hour,
                    "h": 101 + hour,
                    "l": 99 + hour,
                    "c": 100.5 + hour,
                    "v": 1000 + hour,
                }
            )
        return json.dumps({"bars": {symbol: bars}})


class ThreePageAlpacaTransport:
    def __init__(self, *, as_of: datetime, expected_feed: str = "iex") -> None:
        self.as_of = as_of
        self.expected_feed = expected_feed
        self.urls: list[str] = []

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.urls.append(url)
        assert headers["APCA-API-KEY-ID"] == "alpaca-key-id"
        assert headers["APCA-API-SECRET-KEY"] == "alpaca-secret-key"
        query = parse_qs(urlparse(url).query)
        assert query["timeframe"] == ["1Hour"]
        assert query["feed"] == [self.expected_feed]
        symbol = query["symbols"][0]
        page_token = query.get("page_token", [None])[0]
        if page_token is None:
            page_index = 0
            next_page_token = "page-2"
        elif page_token == "page-2":
            page_index = 1
            next_page_token = "page-3"
        elif page_token == "page-3":
            page_index = 2
            next_page_token = None
        else:
            raise AssertionError(f"unexpected page token {page_token!r}")
        bars = []
        for hour in range(2):
            timestamp = self.as_of - timedelta(hours=5 - (page_index * 2 + hour))
            bars.append(
                {
                    "t": timestamp.isoformat().replace("+00:00", "Z"),
                    "o": 100 + page_index * 2 + hour,
                    "h": 101 + page_index * 2 + hour,
                    "l": 99 + page_index * 2 + hour,
                    "c": 100.5 + page_index * 2 + hour,
                    "v": 1000 + page_index * 2 + hour,
                }
            )
        payload: dict[str, object] = {"bars": {symbol: bars}, "next_page_token": next_page_token}
        return json.dumps(payload)
