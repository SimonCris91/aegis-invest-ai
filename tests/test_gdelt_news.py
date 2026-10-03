from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import app.news.gdelt as gdelt_module
import pytest
from app.data.models import DataProviderError, DataProviderStatus
from app.news.gdelt import GdeltNewsProvider
from app.news.intelligence import NewsProviderError, NewsProviderStatus


class FakeTextTransport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.calls += 1
        return json.dumps(self.payload)


class NoNetworkTransport:
    def get_text(self, url: str, headers: dict[str, str]) -> str:
        raise AssertionError("a fresh persisted cache must avoid a provider request")


class RateLimitedTransport:
    def __init__(self, *, retry_after: str = "120") -> None:
        self.calls = 0
        self.retry_after = retry_after

    def get_text(self, url: str, headers: dict[str, str]) -> str:
        self.calls += 1
        raise DataProviderError(
            "rate limited",
            status=DataProviderStatus.RATE_LIMITED,
            http_status=429,
            retry_after=self.retry_after,
        )


def _patch_request_clock(monkeypatch) -> None:
    clock = [1000.0]

    def fake_monotonic() -> float:
        return clock[0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(gdelt_module, "monotonic", fake_monotonic)
    monkeypatch.setattr(gdelt_module, "sleep", fake_sleep)
    monkeypatch.setattr(gdelt_module, "_LAST_REQUEST_STARTED_AT", 0.0)
    monkeypatch.setattr(gdelt_module, "_SHARED_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(gdelt_module, "_SHARED_RATE_LIMIT_STREAK", 0)


def test_gdelt_cache_survives_provider_restart(tmp_path) -> None:
    as_of = datetime.now(UTC)
    published_at = as_of - timedelta(minutes=2)
    transport = FakeTextTransport(
        {
            "articles": [
                {
                    "title": "Central bank announces a rate decision",
                    "seendate": published_at.strftime("%Y%m%dT%H%M%SZ"),
                    "sourceCommonName": "Example Financial News",
                    "url": "https://example.test/news/rate-decision",
                    "language": "English",
                    "sourcecountry": "US",
                }
            ]
        }
    )
    cache_path = tmp_path / "gdelt-news-cache.json"

    first = GdeltNewsProvider(transport=transport, cache_path=cache_path)
    original_items = first.fetch_global_news(as_of=as_of)

    restarted = GdeltNewsProvider(
        transport=NoNetworkTransport(),
        cache_ttl=timedelta(minutes=30),
        cache_path=cache_path,
    )
    restored_items = restarted.fetch_global_news(as_of=as_of + timedelta(minutes=1))

    assert len(original_items) == 1
    assert restored_items == original_items
    assert restarted.last_status is NewsProviderStatus.AVAILABLE
    assert restarted.last_diagnostics["cache_hit"] is True
    assert restarted.read_calls == 0
    assert transport.calls == 1


def test_gdelt_cache_is_ignored_after_ttl(tmp_path) -> None:
    as_of = datetime.now(UTC)
    cache_path = tmp_path / "gdelt-news-cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "saved_at": (as_of - timedelta(hours=1)).isoformat(),
                "as_of": (as_of - timedelta(hours=1)).isoformat(),
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    transport = FakeTextTransport({"articles": []})

    provider = GdeltNewsProvider(
        transport=transport,
        cache_ttl=timedelta(minutes=30),
        cache_path=cache_path,
    )
    assert provider.fetch_global_news(as_of=as_of) == ()
    assert provider.read_calls == 1
    assert transport.calls == 1


def test_gdelt_accepts_a_bounded_candidate_query() -> None:
    as_of = datetime.now(UTC)
    provider = GdeltNewsProvider(query='"Example Asset" OR EXM')

    url = provider._url(as_of=as_of)

    assert "Example+Asset" in url
    assert "EXM" in url


def test_gdelt_uses_only_fresh_cached_articles_during_rate_limit_cooldown(
    tmp_path, monkeypatch
) -> None:
    _patch_request_clock(monkeypatch)
    as_of = datetime.now(UTC)
    published_at = as_of - timedelta(minutes=5)
    cache_path = tmp_path / "gdelt-news-cache.json"
    first_transport = FakeTextTransport(
        {
            "articles": [
                {
                    "title": "Central bank announces a rate decision",
                    "seendate": published_at.strftime("%Y%m%dT%H%M%SZ"),
                    "sourceCommonName": "Example Financial News",
                    "url": "https://example.test/news/rate-decision",
                    "language": "English",
                    "sourcecountry": "US",
                }
            ]
        }
    )
    initial = GdeltNewsProvider(transport=first_transport, cache_path=cache_path)
    assert len(initial.fetch_global_news(as_of=as_of)) == 1

    limited_transport = RateLimitedTransport(retry_after="120")
    provider = GdeltNewsProvider(
        transport=limited_transport,
        cache_ttl=timedelta(minutes=10),
        cache_path=cache_path,
    )
    first_cutoff = as_of + timedelta(minutes=35)
    fallback = provider.fetch_global_news(as_of=first_cutoff)

    assert len(fallback) == 1
    assert provider.last_status is NewsProviderStatus.DELAYED
    assert provider.last_diagnostics["stale_cache_fallback"] is True
    assert provider.last_diagnostics["fresh_articles_returned"] == 1
    assert provider.last_diagnostics["source_error_status"] == "RATE_LIMITED"
    assert provider.last_diagnostics["retry_after_seconds"] == 120
    assert limited_transport.calls == 1

    # A follow-up polling pass during Retry-After serves the same still-fresh
    # evidence and does not hit GDELT again.
    again = provider.fetch_global_news(as_of=first_cutoff + timedelta(minutes=1))
    assert again == fallback
    assert limited_transport.calls == 1


def test_gdelt_rate_limit_cooldown_is_shared_across_candidate_queries(monkeypatch) -> None:
    _patch_request_clock(monkeypatch)
    as_of = datetime.now(UTC)
    first_transport = RateLimitedTransport(retry_after="120")
    first = GdeltNewsProvider(transport=first_transport, query='"First Asset"')

    with pytest.raises(NewsProviderError) as first_error:
        first.fetch_global_news(as_of=as_of)

    assert first_error.value.status is NewsProviderStatus.RATE_LIMITED
    assert first.last_diagnostics["retry_after_seconds"] == 120

    second_transport = FakeTextTransport({"articles": []})
    second = GdeltNewsProvider(transport=second_transport, query='"Different Asset"')
    with pytest.raises(NewsProviderError) as second_error:
        second.fetch_global_news(as_of=as_of)

    assert second_error.value.status is NewsProviderStatus.RATE_LIMITED
    assert second.last_diagnostics["provider_error_message"] == "provider cooldown"
    assert second.last_diagnostics["retry_after_seconds"] == 120
    assert second.read_calls == 0
    assert second_transport.calls == 0


def test_gdelt_never_uses_cached_articles_past_news_freshness_window(
    tmp_path, monkeypatch
) -> None:
    _patch_request_clock(monkeypatch)
    as_of = datetime.now(UTC)
    published_at = as_of - timedelta(hours=7)
    cache_path = tmp_path / "gdelt-news-cache.json"
    initial = GdeltNewsProvider(
        transport=FakeTextTransport(
            {
                "articles": [
                    {
                        "title": "An old market story",
                        "seendate": published_at.strftime("%Y%m%dT%H%M%SZ"),
                        "sourceCommonName": "Example Financial News",
                        "url": "https://example.test/news/old-market-story",
                        "language": "English",
                        "sourcecountry": "US",
                    }
                ]
            }
        ),
        cache_path=cache_path,
    )
    assert len(initial.fetch_global_news(as_of=as_of)) == 1

    transport = RateLimitedTransport()
    provider = GdeltNewsProvider(
        transport=transport,
        cache_ttl=timedelta(minutes=10),
        cache_path=cache_path,
    )
    with pytest.raises(NewsProviderError) as error:
        provider.fetch_global_news(as_of=as_of + timedelta(minutes=35))

    assert error.value.status is NewsProviderStatus.RATE_LIMITED
    assert provider.last_diagnostics["status"] == NewsProviderStatus.RATE_LIMITED.value
    assert "stale_cache_fallback" not in provider.last_diagnostics
    assert transport.calls == 1
