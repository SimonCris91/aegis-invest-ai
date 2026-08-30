"""Sanitized network/proxy diagnostics for eToro transport checks."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from enum import StrEnum
from urllib.parse import urlsplit
from urllib.request import getproxies

from pydantic import Field

from app.brokers.etoro.http import TransportFailureDetail
from app.domain.base import FrozenDomainModel

PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
ACTIVE_PROXY_ENV_VARS = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
)


class NetworkPolicyDiagnostic(StrEnum):
    SYSTEM_PROXY = "SYSTEM_PROXY"
    ENV_PROXY = "ENV_PROXY"
    CODEX_SANDBOX_PROXY = "CODEX_SANDBOX_PROXY"
    TRANSPORT_LIBRARY_PROXY = "TRANSPORT_LIBRARY_PROXY"
    DNS_POLICY = "DNS_POLICY"
    TLS_POLICY = "TLS_POLICY"
    SOCKET_POLICY = "SOCKET_POLICY"
    UNKNOWN_NETWORK_POLICY = "UNKNOWN_NETWORK_POLICY"


class ProxyEnvironmentEntry(FrozenDomainModel):
    variable: str = Field(min_length=1)
    configured: bool
    sanitized_destination: str | None = None


class TransportLibraryProxyEntry(FrozenDomainModel):
    scheme: str = Field(min_length=1)
    configured: bool
    sanitized_destination: str | None = None


class WindowsProxyDiagnostic(FrozenDomainModel):
    available: bool
    configured: bool | None = None
    source: str = "winhttp"
    sanitized_destination: str | None = None


def proxy_environment_report(
    values: Mapping[str, str] | None = None,
) -> tuple[ProxyEnvironmentEntry, ...]:
    source = os.environ if values is None else values
    return tuple(
        ProxyEnvironmentEntry(
            variable=name,
            configured=bool(source.get(name, "").strip()),
            sanitized_destination=_sanitize_proxy_value(
                source.get(name, ""),
                list_value=name.casefold() == "no_proxy",
            ),
        )
        for name in PROXY_ENV_VARS
    )


def transport_library_proxy_report(
    proxies: Mapping[str, str] | None = None,
) -> tuple[TransportLibraryProxyEntry, ...]:
    source = getproxies() if proxies is None else proxies
    return tuple(
        TransportLibraryProxyEntry(
            scheme=scheme,
            configured=bool(value.strip()),
            sanitized_destination=_sanitize_proxy_value(value),
        )
        for scheme, value in sorted(source.items())
    )


def windows_winhttp_proxy_report(raw_output: str | None = None) -> WindowsProxyDiagnostic:
    if raw_output is None:
        try:
            completed = subprocess.run(
                ["netsh", "winhttp", "show", "proxy"],
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return WindowsProxyDiagnostic(available=False)
        raw_output = f"{completed.stdout}\n{completed.stderr}"

    text = raw_output.strip()
    if not text:
        return WindowsProxyDiagnostic(available=True, configured=None)
    lowered = text.casefold()
    direct_markers = (
        "direct access",
        "accesso diretto",
        "nessun server proxy",
        "no proxy server",
    )
    if any(marker in lowered for marker in direct_markers):
        return WindowsProxyDiagnostic(available=True, configured=False)
    configured_markers = ("proxy server", "server proxy", "proxy:")
    if any(marker in lowered for marker in configured_markers):
        return WindowsProxyDiagnostic(
            available=True,
            configured=True,
            sanitized_destination=_sanitize_proxy_value(_first_proxy_like_token(text)),
        )
    return WindowsProxyDiagnostic(available=True, configured=None)


def classify_network_policy(
    transport_detail: str | None,
    *,
    proxy_environment: tuple[ProxyEnvironmentEntry, ...],
    transport_library_proxies: tuple[TransportLibraryProxyEntry, ...],
    system_proxy: WindowsProxyDiagnostic | None = None,
    codex_sandbox_network_restricted: bool = False,
) -> NetworkPolicyDiagnostic:
    if transport_detail == TransportFailureDetail.DNS.value:
        return NetworkPolicyDiagnostic.DNS_POLICY
    if transport_detail == TransportFailureDetail.TLS.value:
        return NetworkPolicyDiagnostic.TLS_POLICY
    if _has_active_proxy_environment(proxy_environment):
        return NetworkPolicyDiagnostic.ENV_PROXY
    if system_proxy is not None and system_proxy.configured:
        return NetworkPolicyDiagnostic.SYSTEM_PROXY
    if _has_transport_library_proxy(transport_library_proxies):
        return NetworkPolicyDiagnostic.TRANSPORT_LIBRARY_PROXY
    if codex_sandbox_network_restricted:
        return NetworkPolicyDiagnostic.CODEX_SANDBOX_PROXY
    if transport_detail in {
        TransportFailureDetail.SOCKET_CONNECT.value,
        TransportFailureDetail.CONNECTION_RESET.value,
        TransportFailureDetail.PROXY_NETWORK_POLICY.value,
    }:
        return NetworkPolicyDiagnostic.SOCKET_POLICY
    return NetworkPolicyDiagnostic.UNKNOWN_NETWORK_POLICY


def _has_active_proxy_environment(entries: tuple[ProxyEnvironmentEntry, ...]) -> bool:
    return any(entry.configured and entry.variable in ACTIVE_PROXY_ENV_VARS for entry in entries)


def _has_transport_library_proxy(entries: tuple[TransportLibraryProxyEntry, ...]) -> bool:
    return any(
        entry.configured and entry.scheme.casefold() not in {"no", "no_proxy"} for entry in entries
    )


def _sanitize_proxy_value(value: str, *, list_value: bool = False) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if list_value:
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        return ",".join(parts[:8]) if parts else None

    candidate = raw.split(";", 1)[0].strip()
    if "=" in candidate and "://" not in candidate.split("=", 1)[0]:
        candidate = candidate.split("=", 1)[1].strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "<configured-unparseable-redacted>"
    if not parsed.hostname:
        return "<configured-unparseable-redacted>"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _first_proxy_like_token(text: str) -> str:
    for line in text.splitlines():
        if ":" not in line:
            continue
        _, value = line.split(":", 1)
        if value.strip():
            return value.strip()
    return text
