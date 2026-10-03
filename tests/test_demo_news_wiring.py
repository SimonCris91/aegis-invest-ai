from datetime import UTC, datetime

import pytest

from app.brokers.etoro import demo_execution
from app.brokers.etoro.demo_execution import _ConfiguredDemoNewsProvider, _RunNewsSession
from app.config.loader import load_config
from app.domain.enums import AssetClass, Currency
from app.domain.universe import UniversalInstrument
from app.news.intelligence import NewsProviderError, NewsProviderStatus, RawNewsItem

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


def _instrument() -> UniversalInstrument:
    return UniversalInstrument(
        broker="etoro",
        broker_instrument_id="100017",
        symbol="ADA",
        display_name="Cardano ADA",
        asset_class=AssetClass.CRYPTO,
        currency=Currency.USD,
        metadata_timestamp=NOW,
    )


class _Provider:
    provider_name = "TEST_PROVIDER"

    def __init__(
        self,
        items: tuple[RawNewsItem, ...] = (),
        error: Exception | None = None,
        status: NewsProviderStatus | None = None,
    ) -> None:
        self.items = items
        self.error = error
        self.last_status = status

    def fetch_global_news(self, *, as_of: datetime) -> tuple[RawNewsItem, ...]:
        if self.error is not None:
            raise self.error
        return tuple(item for item in self.items if item.published_at <= as_of)


def _news_item() -> RawNewsItem:
    return RawNewsItem(
        headline="ADA growth accelerates",
        source="wire",
        published_at=NOW,
        summary="ADA growth",
    )


def test_configured_provider_reaches_preflight_news_contract() -> None:
    provider = _ConfiguredDemoNewsProvider(
        _Provider((_news_item(),)),
        _instrument(),
    )

    news = provider.get_news(("ADA",), as_of=NOW)

    assert len(news) == 1
    assert news[0].asset_relevance == ("ADA",)
    assert news[0].sentiment > 0
    assert provider.diagnostics["news_provider_status"] == "AVAILABLE"
    assert provider.diagnostics["context_news_count"] == 1


def test_no_events_remains_empty_without_synthetic_evidence() -> None:
    provider = _ConfiguredDemoNewsProvider(
        _Provider(status=NewsProviderStatus.AVAILABLE), _instrument()
    )

    assert provider.get_news(("ADA",), as_of=NOW) == ()
    assert provider.diagnostics == {
        "news_provider": "TEST_PROVIDER",
        "news_provider_status": "NO_EVENTS",
        "news_raw_event_count": 0,
        "news_normalized_event_count": 0,
        "news_relevant_event_count": 0,
        "context_news_count": 0,
        "news_request_suppressed_after_rate_limit": False,
    }


def test_provider_failure_fails_safe_to_empty_news() -> None:
    provider = _ConfiguredDemoNewsProvider(
        _Provider(
            error=NewsProviderError(
                "rate limited",
                status=NewsProviderStatus.RATE_LIMITED,
            )
        ),
        _instrument(),
    )

    assert provider.get_news(("ADA",), as_of=NOW) == ()
    assert provider.diagnostics["news_provider_status"] == "RATE_LIMITED"
    assert provider.diagnostics["context_news_count"] == 0


@pytest.mark.parametrize(
    "status",
    (NewsProviderStatus.AUTH_FAILED, NewsProviderStatus.PROVIDER_UNAVAILABLE),
)
def test_provider_failure_status_is_preserved_without_news(status: NewsProviderStatus) -> None:
    provider = _ConfiguredDemoNewsProvider(
        _Provider(error=NewsProviderError("provider failed", status=status)),
        _instrument(),
    )

    assert provider.get_news(("ADA",), as_of=NOW) == ()
    assert provider.diagnostics["news_provider_status"] == status.value
    assert provider.diagnostics["context_news_count"] == 0


def test_run_scoped_cache_reuses_only_same_symbol_and_cutoff() -> None:
    session = _RunNewsSession(_Provider((_news_item(),)))
    first = _ConfiguredDemoNewsProvider(_Provider((_news_item(),)), _instrument(), session=session)
    second = _ConfiguredDemoNewsProvider(_Provider((_news_item(),)), _instrument(), session=session)

    assert first.get_news(("ADA",), as_of=NOW)
    assert second.get_news(("ADA",), as_of=NOW)
    assert session.provider_request_count == 1
    assert session.cache_hits == 1
    assert second.get_news(("ADA",), as_of=NOW.replace(second=1))
    assert session.provider_request_count == 2
    other = _ConfiguredDemoNewsProvider(
        session.provider,
        _instrument().model_copy(update={"symbol": "AAVE", "broker_instrument_id": "100044"}),
        session=session,
    )
    assert other.get_news(("AAVE",), as_of=NOW) == ()
    assert session.provider_request_count == 3


def test_first_rate_limit_suppresses_later_requests() -> None:
    provider = _Provider(
        error=NewsProviderError("rate limited", status=NewsProviderStatus.RATE_LIMITED)
    )
    session = _RunNewsSession(provider)
    first = _ConfiguredDemoNewsProvider(provider, _instrument(), session=session)
    second = _ConfiguredDemoNewsProvider(provider, _instrument(), session=session)

    assert first.get_news(("ADA",), as_of=NOW) == ()
    assert second.get_news(("ADA",), as_of=NOW.replace(second=1)) == ()
    assert session.rate_limit_triggered is True
    assert session.provider_request_count == 1
    assert session.requests_suppressed_after_rate_limit == 1


def test_demo_preflight_keeps_gdelt_when_alpha_vantage_is_rate_limited(monkeypatch) -> None:
    class RateLimitedAlpha(_Provider):
        provider_name = "ALPHA_VANTAGE"

    class AvailableAlpaca(_Provider):
        provider_name = "ALPACA_NEWS"

    class AvailableGdelt(_Provider):
        provider_name = "GDELT_DOC"

    alpha = RateLimitedAlpha(
        error=NewsProviderError("rate limited", status=NewsProviderStatus.RATE_LIMITED),
        status=NewsProviderStatus.RATE_LIMITED,
    )
    alpaca = AvailableAlpaca(status=NewsProviderStatus.AVAILABLE)
    gdelt = AvailableGdelt((_news_item(),), status=NewsProviderStatus.AVAILABLE)
    monkeypatch.setattr(demo_execution, "AlphaVantageNewsProvider", lambda **_: alpha)
    monkeypatch.setattr(demo_execution, "AlpacaNewsProvider", lambda **_: alpaca)
    monkeypatch.setattr(demo_execution, "_shared_demo_gdelt_provider", lambda: gdelt)

    provider = demo_execution._configured_news_provider(
        load_config({"AEGIS_NEWS_PROVIDER": "alpha_vantage"}),
        {
            "AEGIS_NEWS_SECONDARY_PROVIDER": "alpaca",
            "AEGIS_NEWS_GDELT_ENABLED": "true",
        },
    )
    items = provider.fetch_global_news(as_of=NOW)

    assert provider.provider_name == "ALPHA_VANTAGE_PLUS_ALPACA_PLUS_GDELT"
    assert provider.last_status is NewsProviderStatus.PARTIAL
    assert items == (_news_item(),)
    assert provider.last_diagnostics["provider_statuses"]["ALPHA_VANTAGE"] == (
        "ERROR:RATE_LIMITED"
    )
