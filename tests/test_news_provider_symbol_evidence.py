from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import AssetClass
from app.domain.universe import UniversalInstrument
from app.news.alpaca import _parse_article, alpaca_news_symbol
from app.news.intelligence import GlobalNewsIntelligenceEngine, NewsFeedProvider, RawNewsItem

NOW = datetime(2026, 10, 1, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("CRYPTO:ADA", "ADAUSD"),
        ("CRYPTO:BTCUSD", "BTCUSD"),
        ("CRYPTO:LTCJPY", None),
        ("CRYPTO:LTCNZD", None),
        ("AMAT.RTH", "AMAT"),
        ("VWRD.L", None),
    ],
)
def test_source_tickers_do_not_invent_currency_pairs(symbol, expected):
    assert alpaca_news_symbol(symbol) == expected


def instrument(symbol, asset_class=AssetClass.EQUITY):
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id=symbol,
        symbol=symbol,
        asset_class=asset_class,
        metadata_timestamp=NOW,
    )


def analyze(item, instruments):
    return GlobalNewsIntelligenceEngine(NewsFeedProvider((item,))).analyze(
        instruments=instruments,
        as_of=NOW,
    )


def article(**changes):
    raw = dict(
        headline="Market outlook after quarterly results",
        source="Benzinga",
        created_at=(NOW - timedelta(minutes=1)).isoformat(),
        summary="Financial context " * 200,
        symbols=["AAPL", "QQQ"],
    )
    raw.update(changes)
    return _parse_article(raw)


def test_long_summary_keeps_exact_equity_and_etf_article_symbols():
    item = article()
    result = analyze(
        item, (instrument("AAPL"), instrument("QQQ", AssetClass.ETF), instrument("MSFT"))
    )
    assert {link.symbol for link in result.normalized_events[0].companies_assets_affected} == {
        "AAPL",
        "QQQ",
    }
    assert result.asset_contexts["AAPL"].unique_event_count == 1
    assert result.asset_contexts["QQQ"].unique_event_count == 1
    assert result.asset_contexts["MSFT"].unique_event_count == 0


def test_summary_adds_named_company_even_when_headline_already_links_another():
    item = RawNewsItem(
        headline="AAPL earnings grow",
        source="wire",
        published_at=NOW,
        summary="MSFT raises outlook",
    )
    result = analyze(item, (instrument("AAPL"), instrument("MSFT")))
    assert {link.symbol for link in result.normalized_events[0].companies_assets_affected} == {
        "AAPL",
        "MSFT",
    }


def test_article_metadata_does_not_guess_broker_suffix_aliases():
    result = analyze(article(symbols=["AAPL"]), (instrument("AAPL.RTH"), instrument("AAPL.L")))
    assert result.normalized_events[0].companies_assets_affected == ()


def test_untrusted_feed_cannot_claim_alpaca_article_metadata():
    item = RawNewsItem(
        headline="Market outlook",
        source="unknown",
        published_at=NOW,
        provider="GDELT_DOC",
        provider_symbols=("AAPL",),
    )
    assert analyze(item, (instrument("AAPL"),)).normalized_events[0].companies_assets_affected == ()


@pytest.mark.parametrize("symbols", [None, "AAPL", {"ticker": "AAPL"}, [None, 1]])
def test_malformed_article_symbols_do_not_create_false_links(symbols):
    assert article(symbols=symbols).provider_symbols == ()


def test_future_article_cannot_supply_execution_news():
    assert (
        analyze(article(created_at=(NOW + timedelta(hours=1)).isoformat()), (instrument("AAPL"),))
        .asset_contexts["AAPL"]
        .unique_event_count
        == 0
    )
