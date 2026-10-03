"""Small injectable HTTP boundary with bounded read retries."""

import json
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import sleep
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from app.domain.enums import EtoroTransportMode

ETORO_PUBLIC_API_HOST = "public-api.etoro.com"
ETORO_USER_AGENT = "AegisInvestAI/0.7"

DIAGNOSTIC_RESPONSE_HEADERS = {
    "cf-cache-status",
    "cf-ray",
    "content-length",
    "content-type",
    "date",
    "retry-after",
    "server",
    "www-authenticate",
    "x-correlation-id",
    "x-request-id",
}

@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


class EtoroHttpFailureKind(StrEnum):
    AUTH_API_PERMISSION_ERROR = "AUTH_API_PERMISSION_ERROR"
    EDGE_WAF_BLOCK = "EDGE_WAF_BLOCK"
    HTTP_ERROR = "HTTP_ERROR"
    NETWORK_TRANSPORT_ERROR = "NETWORK_TRANSPORT_ERROR"


class TransportFailureDetail(StrEnum):
    DNS = "DNS"
    TLS = "TLS"
    SOCKET_CONNECT = "SOCKET_CONNECT"
    TIMEOUT = "TIMEOUT"
    CONNECTION_RESET = "CONNECTION_RESET"
    PROXY_NETWORK_POLICY = "PROXY_NETWORK_POLICY"
    OTHER_TRANSPORT = "OTHER_TRANSPORT"


RETRYABLE_READ_TRANSPORT_DETAILS = frozenset(
    {
        TransportFailureDetail.DNS,
        TransportFailureDetail.SOCKET_CONNECT,
        TransportFailureDetail.TIMEOUT,
        TransportFailureDetail.CONNECTION_RESET,
    }
)


class TransportError(RuntimeError):
    kind = EtoroHttpFailureKind.NETWORK_TRANSPORT_ERROR

    def __init__(
        self,
        message: str,
        *,
        detail: TransportFailureDetail = TransportFailureDetail.OTHER_TRANSPORT,
    ) -> None:
        super().__init__(message)
        self.detail = detail


class UnknownWriteOutcome(TransportError):
    pass


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse: ...


class UrllibTransport:
    def __init__(self, mode: EtoroTransportMode = EtoroTransportMode.SYSTEM_PROXY) -> None:
        self.mode = mode

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> HttpResponse:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            if self.mode is EtoroTransportMode.DIRECT:
                opener = build_opener(ProxyHandler({}))
                response_context = opener.open(request, timeout=20)
            else:
                response_context = urlopen(request, timeout=20)
            with response_context as response:
                return HttpResponse(
                    response.status, dict(response.headers.items()), response.read()
                )
        except HTTPError as exc:
            return HttpResponse(exc.code, dict(exc.headers.items()), exc.read())
        except URLError as exc:
            raise TransportError(
                "network transport failed", detail=classify_transport_failure(exc)
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise TransportError(
                "network transport failed", detail=classify_transport_failure(exc)
            ) from exc


class DisciplinedHttpClient:
    def __init__(
        self,
        transport: HttpTransport,
        *,
        max_read_attempts: int = 3,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if not 1 <= max_read_attempts <= 5:
            raise ValueError("max_read_attempts must be between 1 and 5")
        self._transport = transport
        self._max_read_attempts = max_read_attempts
        self._sleeper = sleeper

    def get_once(self, url: str, headers: dict[str, str]) -> HttpResponse:
        """One physical GET; caller owns pacing and retry policy."""
        return self._transport.request("GET", url, self._request_headers(url, headers))

    def get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        response: HttpResponse | None = None
        for attempt in range(self._max_read_attempts):
            try:
                response = self._transport.request(
                    "GET", url, self._request_headers(url, headers)
                )
            except TransportError as exc:
                if (
                    attempt + 1 >= self._max_read_attempts
                    or exc.detail not in RETRYABLE_READ_TRANSPORT_DETAILS
                ):
                    raise
                self._sleeper(min(0.25 * (2**attempt), 1.0))
                continue
            if response.status not in {429, 500, 502, 503, 504}:
                return response
            if attempt + 1 < self._max_read_attempts:
                delay = min(float(response.headers.get("Retry-After", "0")), 2.0)
                self._sleeper(max(delay, 0))
        assert response is not None
        return response

    def post_once(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> HttpResponse:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        try:
            return self._transport.request(
                "POST",
                url,
                self._request_headers(url, {**headers, "Content-Type": "application/json"}),
                encoded,
            )
        except TransportError as exc:
            raise UnknownWriteOutcome("write outcome is unknown; reconciliation required") from exc

    def post_read(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> HttpResponse:
        """Retry a documented side-effect-free POST used only for data lookup."""

        encoded = json.dumps(payload, separators=(",", ":")).encode()
        response: HttpResponse | None = None
        request_headers = {**headers, "Content-Type": "application/json"}
        for attempt in range(self._max_read_attempts):
            try:
                response = self._transport.request(
                    "POST", url, self._request_headers(url, request_headers), encoded
                )
            except TransportError as exc:
                if (
                    attempt + 1 >= self._max_read_attempts
                    or exc.detail not in RETRYABLE_READ_TRANSPORT_DETAILS
                ):
                    raise
                self._sleeper(min(0.25 * (2**attempt), 1.0))
                continue
            if response.status not in {429, 500, 502, 503, 504}:
                return response
            if attempt + 1 < self._max_read_attempts:
                delay = min(float(response.headers.get("Retry-After", "0")), 2.0)
                self._sleeper(max(delay, 0))
        assert response is not None
        return response

    def _request_headers(self, url: str, headers: dict[str, str]) -> dict[str, str]:
        normalized = {
            key: value for key, value in headers.items() if key.casefold() != "user-agent"
        }
        if urlparse(url).netloc.casefold() == ETORO_PUBLIC_API_HOST:
            normalized["User-Agent"] = ETORO_USER_AGENT
        return normalized


def classify_http_failure(response: HttpResponse) -> EtoroHttpFailureKind:
    body = response.body.decode("utf-8", errors="replace").strip().casefold()
    if response.status == 403 and body == "error code: 1010":
        return EtoroHttpFailureKind.EDGE_WAF_BLOCK
    if response.status in {401, 403}:
        return EtoroHttpFailureKind.AUTH_API_PERMISSION_ERROR
    return EtoroHttpFailureKind.HTTP_ERROR


def diagnostic_headers(response: HttpResponse) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.casefold() in DIAGNOSTIC_RESPONSE_HEADERS
    }


def classify_transport_failure(exc: BaseException) -> TransportFailureDetail:
    reason = exc.reason if isinstance(exc, URLError) else exc
    text = f"{type(reason).__name__}: {reason}".casefold()
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in text:
        return TransportFailureDetail.DNS
    if isinstance(reason, ssl.SSLError) or "certificate" in text or "tls" in text:
        return TransportFailureDetail.TLS
    if isinstance(reason, TimeoutError) or "timed out" in text or "timeout" in text:
        return TransportFailureDetail.TIMEOUT
    if isinstance(reason, ConnectionResetError) or "connection reset" in text:
        return TransportFailureDetail.CONNECTION_RESET
    if isinstance(reason, PermissionError) or "winerror 10013" in text:
        return TransportFailureDetail.PROXY_NETWORK_POLICY
    if isinstance(reason, OSError):
        return TransportFailureDetail.SOCKET_CONNECT
    return TransportFailureDetail.OTHER_TRANSPORT
