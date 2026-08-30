"""Safe runtime composition for authenticated eToro read diagnostics."""

from collections.abc import Mapping

from pydantic import Field

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.network import (
    WindowsProxyDiagnostic,
    classify_network_policy,
    proxy_environment_report,
    transport_library_proxy_report,
)
from app.brokers.models import BrokerStatus
from app.config.loader import load_runtime_values
from app.config.models import ApplicationConfig
from app.domain.base import FrozenDomainModel


class EtoroRuntimeSettings(FrozenDomainModel):
    api_key_configured: bool
    user_key_configured: bool
    expected_username_configured: bool
    expected_gcid_configured: bool
    readiness_symbol: str | None = Field(default=None, min_length=1)
    readiness_instrument_id: int | None = Field(default=None, gt=0)
    maximum_quote_age_seconds: int = Field(default=300, gt=0)
    expected_username: str | None = Field(default=None, min_length=1, repr=False, exclude=True)
    expected_gcid: str | None = Field(default=None, min_length=1, repr=False, exclude=True)


def runtime_credentials(values: Mapping[str, str] | None = None) -> EtoroCredentials | None:
    source = load_runtime_values() if values is None else values
    api_key = source.get("ETORO_API_KEY", "")
    user_key = source.get("ETORO_USER_KEY", "")
    if not api_key or not user_key:
        return None
    return EtoroCredentials(api_key=api_key, user_key=user_key)


def _optional_int(name: str, value: str) -> int | None:
    if not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def runtime_settings(values: Mapping[str, str] | None = None) -> EtoroRuntimeSettings:
    source = load_runtime_values() if values is None else values
    maximum_quote_age_seconds = _optional_int(
        "AEGIS_ETORO_READINESS_MAX_QUOTE_AGE_SECONDS",
        source.get("AEGIS_ETORO_READINESS_MAX_QUOTE_AGE_SECONDS", "300"),
    )
    if maximum_quote_age_seconds is None:
        maximum_quote_age_seconds = 300
    expected_username = source.get("ETORO_EXPECTED_USERNAME", "").strip() or None
    expected_gcid = source.get("ETORO_EXPECTED_GCID", "").strip() or None
    return EtoroRuntimeSettings(
        api_key_configured=bool(source.get("ETORO_API_KEY", "")),
        user_key_configured=bool(source.get("ETORO_USER_KEY", "")),
        expected_username_configured=expected_username is not None,
        expected_gcid_configured=expected_gcid is not None,
        readiness_symbol=source.get("AEGIS_ETORO_READINESS_SYMBOL", "").strip() or None,
        readiness_instrument_id=_optional_int(
            "AEGIS_ETORO_READINESS_INSTRUMENT_ID",
            source.get("AEGIS_ETORO_READINESS_INSTRUMENT_ID", ""),
        ),
        maximum_quote_age_seconds=maximum_quote_age_seconds,
        expected_username=expected_username,
        expected_gcid=expected_gcid,
    )


def etoro_status(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
) -> BrokerStatus:
    credentials = runtime_credentials(values)
    configured = credentials is not None
    if not configured or not config.etoro_api_enabled:
        return BrokerStatus(
            credentials_configured=configured,
            authentication_attempted=False,
            authentication_successful=False,
            account_identity_verified=False,
            real_portfolio_read_available=False,
            demo_portfolio_read_available=False,
            market_data_available=False,
            demo_execution_available=False,
            status="NOT_CONFIGURED",
        )
    assert credentials is not None
    read_client = client or EtoroReadClient(
        credentials, DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
    )
    try:
        identity = read_client.identity()
    except EtoroApiError as exc:
        status = exc.category.value if exc.status in {401, 403} else "AUTHENTICATION_FAILED"
        return BrokerStatus(
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=False,
            account_identity_verified=False,
            real_portfolio_read_available=False,
            demo_portfolio_read_available=False,
            market_data_available=False,
            demo_execution_available=False,
            status=status,
        )
    except (RuntimeError, ValueError):
        return BrokerStatus(
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=False,
            account_identity_verified=False,
            real_portfolio_read_available=False,
            demo_portfolio_read_available=False,
            market_data_available=False,
            demo_execution_available=False,
            status="AUTHENTICATION_FAILED",
        )
    real_available = False
    demo_available = False
    try:
        read_client.real_portfolio_read_only({})
        real_available = True
    except (RuntimeError, ValueError):
        pass
    try:
        read_client.demo_account(identity)
        demo_available = True
    except (RuntimeError, ValueError):
        pass
    return BrokerStatus(
        credentials_configured=True,
        authentication_attempted=True,
        authentication_successful=True,
        account_identity_verified=True,
        real_portfolio_read_available=real_available,
        demo_portfolio_read_available=demo_available,
        market_data_available=False,
        demo_execution_available=(
            demo_available and config.etoro_demo_execution_enabled and config.demo_smoke_test_opt_in
        ),
        status="READ_VALIDATED" if real_available and demo_available else "PARTIAL_READ_VALIDATION",
    )


def etoro_transport_check(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    proxy_values: Mapping[str, str] | None = None,
    library_proxies: Mapping[str, str] | None = None,
    system_proxy: WindowsProxyDiagnostic | None = None,
) -> dict[str, object]:
    proxy_environment = proxy_environment_report(proxy_values)
    transport_library_proxies = transport_library_proxy_report(library_proxies)
    credentials = runtime_credentials(values)
    base_payload = {
        "endpoint": "/api/v1/me",
        "transport_mode": config.etoro_transport_mode.value,
        "proxy_environment": tuple(entry.model_dump(mode="json") for entry in proxy_environment),
        "transport_library_proxy": tuple(
            entry.model_dump(mode="json") for entry in transport_library_proxies
        ),
    }
    if credentials is None:
        return {
            **base_payload,
            "http_status": None,
            "status": "NOT_CONFIGURED",
            "category": "CREDENTIALS",
        }
    if not config.etoro_api_enabled:
        return {
            **base_payload,
            "http_status": None,
            "status": "NOT_CONFIGURED",
            "category": "API_DISABLED",
        }
    read_client = client or EtoroReadClient(
        credentials, DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
    )
    try:
        headers = read_client.identity_diagnostic_headers()
    except EtoroApiError as exc:
        metadata = exc.safe_metadata()
        network_policy_detail = classify_network_policy(
            metadata.get("transport_detail"),
            proxy_environment=proxy_environment,
            transport_library_proxies=transport_library_proxies,
            system_proxy=system_proxy,
        )
        return {
            **base_payload,
            "endpoint": exc.endpoint,
            "http_status": exc.status,
            "status": "BLOCKED",
            "category": exc.category.value,
            "transport_detail": metadata.get("transport_detail"),
            "network_policy_detail": network_policy_detail.value,
            "cf_ray": metadata.get("cf_ray"),
            "content_type": metadata.get("content_type"),
        }
    except (RuntimeError, ValueError):
        return {
            **base_payload,
            "http_status": None,
            "status": "BLOCKED",
            "category": "OTHER_TRANSPORT",
            "network_policy_detail": "UNKNOWN_NETWORK_POLICY",
        }
    content_type = next(
        (value for key, value in headers.items() if key.casefold() == "content-type"),
        None,
    )
    cf_ray = next((value for key, value in headers.items() if key.casefold() == "cf-ray"), None)
    return {
        **base_payload,
        "http_status": 200,
        "status": "LIVE_VERIFIED",
        "category": None,
        "cf_ray": cf_ray,
        "content_type": content_type,
    }
