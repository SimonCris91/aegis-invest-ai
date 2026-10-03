from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import AssetClass
from app.domain.universe import UniversalInstrument
from app.news.intelligence import (
    GlobalNewsEventCategory,
    GlobalNewsIntelligenceEngine,
    NewsFeedProvider,
    RawNewsItem,
)


def _instrument(symbol: str, name: str, asset_class: AssetClass) -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id=symbol,
        symbol=symbol,
        display_name=name,
        asset_class=asset_class,
        metadata_timestamp=datetime(2026, 9, 24, tzinfo=UTC),
    )


def _analyze(headline: str, instruments: tuple[UniversalInstrument, ...]):
    as_of = datetime(2026, 9, 24, 12, tzinfo=UTC)
    provider = NewsFeedProvider(
        (
            RawNewsItem(
                headline=headline,
                source="GDELT indexed outlet",
                published_at=as_of,
                provider="GDELT_DOC",
            ),
        )
    )
    return GlobalNewsIntelligenceEngine(provider).analyze(
        instruments=instruments,
        as_of=as_of,
    )


def test_global_macro_news_stays_global_instead_of_linking_every_etf_and_crypto():
    instruments = (
        _instrument("SPY", "SPDR S&P 500 ETF", AssetClass.ETF),
        _instrument("QQQ", "Invesco Nasdaq 100 ETF", AssetClass.ETF),
        _instrument("BTC", "Bitcoin", AssetClass.CRYPTO),
        _instrument("ETH", "Ethereum", AssetClass.CRYPTO),
    )

    result = _analyze(
        "Brazil central bank raises interest rates after inflation report",
        instruments,
    )

    event = result.normalized_events[0]
    assert event.event_category is GlobalNewsEventCategory.CENTRAL_BANK
    assert event.companies_assets_affected == ()
    assert result.global_risk_snapshot.monetary_policy_risk > 0
    assert all(context.unique_event_count == 0 for context in result.asset_contexts.values())


def test_short_ticker_does_not_match_lowercase_prose_but_uppercase_ticker_does():
    instrument = _instrument("ON", "ON Semiconductor", AssetClass.EQUITY)

    prose_result = _analyze("Markets rally on hopes of a rate cut", (instrument,))
    ticker_result = _analyze("ON shares rise after company raises its outlook", (instrument,))

    assert prose_result.normalized_events[0].companies_assets_affected == ()
    assert tuple(
        link.symbol for link in ticker_result.normalized_events[0].companies_assets_affected
    ) == ("ON",)


def test_company_alias_links_only_the_named_company():
    instruments = (
        _instrument("AAPL", "Apple Inc.", AssetClass.EQUITY),
        _instrument("MSFT", "Microsoft Corporation", AssetClass.EQUITY),
        _instrument("SPY", "SPDR S&P 500 ETF", AssetClass.ETF),
    )

    result = _analyze("Apple announces a stronger iPhone outlook", instruments)

    assert tuple(
        link.symbol for link in result.normalized_events[0].companies_assets_affected
    ) == ("AAPL",)


def test_low_trust_gdelt_source_is_not_mistaken_for_high_confidence_evidence():
    instrument = _instrument("AAPL", "Apple Inc.", AssetClass.EQUITY)

    result = _analyze("Apple announces a stronger iPhone outlook", (instrument,))

    context = result.asset_contexts["AAPL"]
    assert context.aggregate_source_reliability == Decimal("0.30")
    assert context.aggregate_confidence < context.aggregate_relevance


@pytest.mark.parametrize("headline", [
    "Republicans pour money into US House races previously considered safe",
    "Republicans pour money into US House races previously considered SAFE",
    "A California sheriff seized ballots",
    "A Kiev, Moscou cible aussi le panier de courses",
    "Investors move near the safe exit",
])
def test_common_words_cannot_link_through_ticker_or_display_name(headline):
    instruments = (
        _instrument("SAFE", "Safe", AssetClass.CRYPTO),
        _instrument("A", "VAULTA", AssetClass.CRYPTO),
        _instrument("NEAR", "NEAR", AssetClass.CRYPTO),
    )
    result = _analyze(headline, instruments)
    assert result.normalized_events[0].companies_assets_affected == ()
    assert all(context.unique_event_count == 0 for context in result.asset_contexts.values())


@pytest.mark.parametrize("headline,symbol,name", [
    ("SAFE token launches a governance vote", "SAFE", "Safe"),
    ("Safe token launches a governance vote", "SAFE", "Safe"),
    ("$SAFE rallies after governance vote", "SAFE", "Safe"),
    ("A token launches a governance vote", "A", "VAULTA"),
    ("VAULTA announces a network upgrade", "A", "VAULTA"),
    ("NYSE:ON announces results", "ON", "ON Semiconductor"),
    ("NEAR protocol announces an upgrade", "NEAR", "NEAR"),
])
def test_qualified_mentions_of_ambiguous_assets_still_link(headline, symbol, name):
    result = _analyze(headline, (_instrument(symbol, name, AssetClass.CRYPTO),))
    assert [link.symbol for link in result.normalized_events[0].companies_assets_affected] == [symbol]
