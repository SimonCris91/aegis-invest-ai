from __future__ import annotations

from app.benchmarks import etoro_profiles


class FakeClient:
    def public_portfolio_gain(self, username: str, *, granularity: str, count: int) -> object:
        assert granularity == "monthly"
        assert count == 60
        return {"gains": [{"date": "2026-08", "gain": "0.0425"}]}

    def public_portfolio_copiers(self, username: str) -> object:
        return {"copiers": 1234, "riskScore": 4}

    def public_portfolio_assets(self, username: str, *, period: str) -> object:
        return {"history": [{"date": "2026-08-31", "assets": [{"symbol": "BTC", "valuePct": 40.5}]}]}


def test_public_profile_payload_is_sanitized_and_read_only() -> None:
    payload = etoro_profiles.read_etoro_benchmarks(FakeClient())  # type: ignore[arg-type]

    assert payload["status"] == "AVAILABLE"
    profile = payload["profiles"][0]  # type: ignore[index]
    assert profile["username"] == "TradingInvest890"
    assert profile["latest_monthly_gain"] == {"date": "2026-08", "gain": 4.25}
    assert profile["copiers"] == 1234.0
    assert profile["risk_score"] == 4.0
    assert profile["top_assets"] == [{"symbol": "BTC", "weight": 40.5}]
    assert "api_key" not in str(payload).lower()
    assert "user_key" not in str(payload).lower()


def test_missing_credentials_never_claims_verified_external_data() -> None:
    payload = etoro_profiles.read_etoro_benchmarks(None)

    assert payload["status"] == "NOT_CONFIGURED"
    assert all(row["status"] == "NOT_VERIFIED" for row in payload["profiles"])  # type: ignore[index]


def test_profile_http_failure_is_local_to_one_profile(monkeypatch) -> None:
    class FailingClient(FakeClient):
        def public_portfolio_gain(self, username: str, *, granularity: str, count: int) -> object:
            if username == "TradingInvest890":
                raise RuntimeError("synthetic")
            return super().public_portfolio_gain(username, granularity=granularity, count=count)

    monkeypatch.setattr(etoro_profiles, "_cache", None)
    payload = etoro_profiles.read_etoro_benchmarks(FailingClient())  # type: ignore[arg-type]
    first = payload["profiles"][0]  # type: ignore[index]
    second = payload["profiles"][1]  # type: ignore[index]
    assert first["status"] == "UNAVAILABLE"
    assert second["status"] == "AVAILABLE"
