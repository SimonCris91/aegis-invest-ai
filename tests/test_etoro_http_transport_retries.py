import pytest

from app.brokers.etoro.http import (
    DisciplinedHttpClient,
    HttpResponse,
    TransportError,
    TransportFailureDetail,
)


class SequencedTransport:
    def __init__(self, responses: list[HttpResponse | TransportError]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append((method, url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, TransportError):
            raise response
        return response


@pytest.mark.parametrize("method", ["get", "post_read"])
def test_read_retries_transient_transport_failure_only(method: str) -> None:
    waits: list[float] = []
    transport = SequencedTransport(
        [
            TransportError("timeout", detail=TransportFailureDetail.TIMEOUT),
            HttpResponse(200, {}, b"{}"),
        ]
    )
    client = DisciplinedHttpClient(transport, sleeper=waits.append)

    if method == "get":
        response = client.get("https://example.invalid/read", {})
    else:
        response = client.post_read("https://example.invalid/lookup", {}, {"id": 1})

    assert response.status == 200
    assert len(transport.calls) == 2
    assert waits == [0.25]


def test_read_transport_retry_is_bounded_and_policy_failures_are_not_retried() -> None:
    waits: list[float] = []
    transient = TransportError("timeout", detail=TransportFailureDetail.TIMEOUT)
    transport = SequencedTransport([transient, transient, transient])
    client = DisciplinedHttpClient(transport, sleeper=waits.append)

    with pytest.raises(TransportError):
        client.get("https://example.invalid/read", {})

    assert len(transport.calls) == 3
    assert waits == [0.25, 0.5]

    blocked = TransportError(
        "policy block", detail=TransportFailureDetail.PROXY_NETWORK_POLICY
    )
    policy_transport = SequencedTransport([blocked])
    policy_client = DisciplinedHttpClient(policy_transport, sleeper=waits.append)
    with pytest.raises(TransportError):
        policy_client.get("https://example.invalid/read", {})
    assert len(policy_transport.calls) == 1
