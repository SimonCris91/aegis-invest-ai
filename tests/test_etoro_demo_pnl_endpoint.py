import json

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import BASE, DEMO_PNL_PATH, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, HttpResponse


class RecordingTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        return HttpResponse(200, {}, json.dumps(self.payload).encode())


def test_demo_pnl_payload_uses_read_only_documented_demo_endpoint() -> None:
    payload = {"clientPortfolio": {"positions": [{"positionID": 77, "unrealizedPnL": {"pnL": 4}}]}}
    transport = RecordingTransport(payload)
    client = EtoroReadClient(
        EtoroCredentials(api_key="fixture", user_key="fixture"),
        DisciplinedHttpClient(transport),
    )

    assert client.demo_pnl_payload() == payload
    assert len(transport.calls) == 1
    method, url, headers, body = transport.calls[0]
    assert method == "GET"
    assert url == BASE + DEMO_PNL_PATH
    assert "x-request-id" in headers
    assert body is None
