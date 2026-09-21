from datetime import UTC, datetime
from decimal import Decimal

from app.config.models import ApplicationConfig
from app.data.runtime import build_etoro_broker_universe_discovery_report
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType
from app.domain.universe import UniversalInstrument

NOW = datetime(2026, 8, 31, 16, tzinfo=UTC)


class FakeDiscoveryClient:
    def __init__(self, pages: tuple[tuple[UniversalInstrument, ...], ...]) -> None:
        self.pages = pages
        self.calls: list[tuple[int, int, str | None]] = []

    def discover_instruments(
        self, *, as_of, page_size: int, page_number: int, search_text: str | None = None
    ) -> tuple[UniversalInstrument, ...]:
        self.calls.append((page_size, page_number, search_text))
        return self.pages[page_number - 1] if page_number <= len(self.pages) else ()


def test_discovery_deduplicates_ids_and_does_not_filter_nominal_price() -> None:
    high_price = _instrument("BTC", "100001", AssetClass.CRYPTO, Decimal("100000"))
    duplicate = _instrument("BTC", "100001", AssetClass.CRYPTO, Decimal("100001"))
    client = FakeDiscoveryClient(
        (
            (
                high_price,
                _instrument("ETH", "100002", AssetClass.CRYPTO, Decimal("3000")),
                _instrument("SOL", "100003", AssetClass.CRYPTO, Decimal("150")),
                _instrument("XRP", "100004", AssetClass.CRYPTO, Decimal("1")),
                duplicate,
            ),
        )
    )

    report = build_etoro_broker_universe_discovery_report(
        ApplicationConfig(), client=client, clock=lambda: NOW, discovery_limit=5
    )

    assert report["status"] == "READ_ONLY_DISCOVERY_COMPLETE"
    assert report["discovered_count"] == 4
    assert report["duplicate_instrument_ids"] == ("100001",)
    assert report["verified_mapping"][0]["symbol"] == "BTC"
    assert report["broker_write_calls"] == 0
    assert report["validated_baseline_unchanged"] is True
    assert len(report["validated_baseline_symbols"]) == 34
    assert client.calls == [(5, 1, None)]


def test_unsupported_asset_class_is_not_a_scanner_candidate() -> None:
    instrument = _instrument("OIL-CFD", "100002", AssetClass.CFD, Decimal("80"))
    report = build_etoro_broker_universe_discovery_report(
        ApplicationConfig(),
        client=FakeDiscoveryClient(((instrument,),)),
        clock=lambda: NOW,
        discovery_limit=1,
    )

    row = report["mapping_rows"][0]
    assert row["mapping_status"] == "VERIFIED"
    assert row["market_data_status"] == "UNSUPPORTED_ASSET_CLASS"
    assert report["expansion_candidates"] == ()


def test_repeated_page_fails_closed_instead_of_claiming_full_catalog() -> None:
    page = (_instrument("AAPL", "100001", AssetClass.EQUITY, Decimal("200")),)
    report = build_etoro_broker_universe_discovery_report(
        ApplicationConfig(
            scanner={
                "discovery_limit": 2,
                "ranked_shortlist_limit": 1,
                "deep_analysis_limit": 1,
                "etoro_max_pages": 2,
            }
        ),
        client=FakeDiscoveryClient((page, page)),
        clock=lambda: NOW,
        discovery_limit=2,
    )

    assert report["status"] == "BLOCKED"
    assert report["blocker"] == "ETORO_DISCOVERY_PAGINATION_STALLED"
    assert report["pagination_verified"] is False
    assert report["broker_write_calls"] == 0


def _instrument(
    symbol: str, instrument_id: str, asset_class: AssetClass, price: Decimal
) -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id=instrument_id,
        symbol=symbol,
        display_name=symbol,
        asset_class=asset_class,
        currency=Currency.USD,
        market_status=MarketStatus.OPEN,
        tradeable=True,
        buy_allowed=True,
        sell_allowed=True,
        short_allowed=False,
        settlement_type=SettlementType.REAL,
        last_price=price,
        metadata_timestamp=NOW,
    )
