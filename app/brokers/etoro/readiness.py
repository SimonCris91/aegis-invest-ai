"""Read-only eToro readiness checks for live validation gates."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, field_validator

from app.agent.service import DeterministicAegisAgent
from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.mapping import asset_class_from_etoro_instrument_type
from app.brokers.etoro.runtime import runtime_credentials, runtime_settings
from app.brokers.identity import AccountIdentityGuard
from app.brokers.market_validation import (
    MarketObservationError,
    validate_market_observation,
)
from app.brokers.models import (
    AccountKind,
    BrokerCapabilities,
    BrokerIdentity,
    InstrumentResolution,
)
from app.config.models import ApplicationConfig
from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import MarketStatus, OperatingMode, SettlementType
from app.domain.market import InstrumentMetadata, MarketQuote, NewsItem
from app.domain.portfolio import PortfolioSnapshot
from app.orchestration.shadow import ShadowRecorder, ShadowService
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SqliteRecordStore

CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_BLOCKED = "BLOCKED"
CHECK_NOT_CONFIGURED = "NOT_CONFIGURED"
CHECK_NOT_AVAILABLE = "NOT_AVAILABLE"
CHECK_MARKET_CLOSED = "MARKET_CLOSED"
CHECK_UNKNOWN = "UNKNOWN"
DEFAULT_READINESS_STORE_PATH = Path("work") / "etoro-readiness.sqlite3"


class EtoroReadinessCheck(FrozenDomainModel):
    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    timestamp: datetime
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")


class EtoroReadinessReport(FrozenDomainModel):
    generated_at: datetime
    status: str = Field(min_length=1)
    checks: tuple[EtoroReadinessCheck, ...]
    credentials_configured: bool
    authentication_attempted: bool
    authentication_successful: bool
    broker_identity_verified: bool
    live_rates_verified: bool
    demo_portfolio_read_verified: bool
    real_portfolio_read_only_verified: bool
    demo_eligibility_verified: bool
    shadow_mode_live_data_verified: bool
    demo_execution_ready: bool = False
    real_execution_available: bool = False
    demo_auto_execution_enabled: bool = False

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "generated_at")


class _EmptyNewsProvider:
    def get_news(self, symbols: tuple[str, ...], *, as_of: datetime) -> tuple[NewsItem, ...]:
        return ()


class _ReadinessBroker:
    def __init__(
        self,
        *,
        identity: BrokerIdentity,
        portfolio: PortfolioSnapshot,
        quote: MarketQuote,
        instrument: InstrumentMetadata,
    ) -> None:
        self._identity = identity
        self._portfolio = portfolio
        self._quote = quote
        self._instrument = instrument

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="etoro-official-api-readiness",
            mode=OperatingMode.SHADOW,
            authenticated_reads=True,
            demo_execution=False,
            real_execution=False,
        )

    def identity(self) -> BrokerIdentity:
        return self._identity

    def portfolio(self) -> PortfolioSnapshot:
        return self._portfolio

    def quote(self, instrument_id: int, symbol: str) -> MarketQuote:
        if instrument_id != self._quote.instrument_id or symbol != self._quote.symbol:
            raise ValueError("readiness quote mapping mismatch")
        return self._quote

    def instrument(self, instrument_id: int, symbol: str) -> InstrumentMetadata:
        if instrument_id != self._instrument.instrument_id or symbol != self._instrument.symbol:
            raise ValueError("readiness instrument mapping mismatch")
        return self._instrument


def build_etoro_readiness_report(
    config: ApplicationConfig,
    *,
    values: Mapping[str, str] | None = None,
    client: EtoroReadClient | None = None,
    clock: Callable[[], datetime] | None = None,
    store: SqliteRecordStore | None = None,
) -> EtoroReadinessReport:
    now = (clock or (lambda: datetime.now(UTC)))()
    checks: list[EtoroReadinessCheck] = []

    def add_check(
        name: str, status: str, reason: str, metadata: Mapping[str, str] | None = None
    ) -> None:
        checks.append(
            EtoroReadinessCheck(
                name=name,
                status=status,
                reason=reason,
                timestamp=now,
                metadata=dict(metadata or {}),
            )
        )

    try:
        settings = runtime_settings(values)
    except ValueError as exc:
        add_check("runtime_configuration", CHECK_FAIL, str(exc))
        return _finalize_report(now, checks, credentials_configured=False, store=store)

    credentials = runtime_credentials(values)
    credentials_configured = credentials is not None
    if credentials_configured:
        add_check("credentials", CHECK_PASS, "both eToro credential placeholders are configured")
    else:
        add_check("credentials", CHECK_NOT_CONFIGURED, "eToro API credentials are not configured")

    if not config.etoro_api_enabled:
        add_check("api_enablement", CHECK_NOT_CONFIGURED, "ETORO_API_ENABLED is false")
    else:
        add_check("api_enablement", CHECK_PASS, "official eToro API reads are enabled")

    if not credentials_configured or not config.etoro_api_enabled:
        _add_blocked_live_checks(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=credentials_configured,
            store=store,
        )

    assert credentials is not None
    read_client = client or _live_client(credentials, config)
    authentication_successful = False
    identity_verified = False

    try:
        identity = read_client.identity()
        authentication_successful = True
        add_check("authentication", CHECK_PASS, "authenticated user profile was read")
    except EtoroApiError as exc:
        add_check(
            "authentication",
            CHECK_FAIL,
            f"authenticated user profile could not be read: {exc.category.value}",
            _failure_metadata(exc),
        )
        _add_blocked_after_identity(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=False,
            store=store,
        )
    except (RuntimeError, ValueError):
        add_check("authentication", CHECK_FAIL, "authenticated user profile could not be read")
        _add_blocked_after_identity(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=False,
            store=store,
        )

    if not settings.expected_username_configured and not settings.expected_gcid_configured:
        add_check("identity_match", CHECK_BLOCKED, "no expected eToro identity is configured")
        _add_blocked_after_identity(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            store=store,
        )

    if _identity_matches(identity, settings.expected_username, settings.expected_gcid):
        identity_verified = True
        add_check(
            "identity_match",
            CHECK_PASS,
            "authenticated identity matches configured expected identity",
            {"identity_reference": identity.redacted_reference},
        )
    else:
        add_check("identity_match", CHECK_FAIL, "authenticated identity mismatch")
        _add_blocked_after_identity(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=False,
            store=store,
        )

    instrument = None
    quote = None
    real_portfolio = None
    market_status = MarketStatus.UNKNOWN
    if settings.readiness_symbol is None:
        add_check("instrument_resolution", CHECK_BLOCKED, "no readiness symbol is configured")
        _add_blocked_after_instrument(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=identity_verified,
            store=store,
        )
    else:
        try:
            resolution = read_client.resolve_instrument(settings.readiness_symbol, as_of=now)
            if (
                settings.readiness_instrument_id is not None
                and resolution.instrument_id != settings.readiness_instrument_id
            ):
                add_check("instrument_resolution", CHECK_FAIL, "resolved instrument ID mismatch")
                _add_blocked_after_instrument(add_check)
                return _finalize_report(
                    now,
                    checks,
                    credentials_configured=True,
                    authentication_attempted=True,
                    authentication_successful=authentication_successful,
                    broker_identity_verified=identity_verified,
                    store=store,
                )
            elif not resolution.resolved:
                add_check(
                    "instrument_resolution",
                    CHECK_FAIL,
                    "instrument symbol could not be resolved",
                )
                _add_blocked_after_instrument(add_check)
                return _finalize_report(
                    now,
                    checks,
                    credentials_configured=True,
                    authentication_attempted=True,
                    authentication_successful=authentication_successful,
                    broker_identity_verified=identity_verified,
                    store=store,
                )
            elif not resolution.structurally_supported:
                add_check(
                    "instrument_resolution",
                    CHECK_FAIL,
                    "resolved instrument is not structurally supported: "
                    f"{resolution.structural_status}",
                )
                _add_blocked_after_instrument(add_check)
                return _finalize_report(
                    now,
                    checks,
                    credentials_configured=True,
                    authentication_attempted=True,
                    authentication_successful=authentication_successful,
                    broker_identity_verified=identity_verified,
                    store=store,
                )
            else:
                instrument = _metadata_from_resolution(resolution, now)
                market_status = resolution.market_status
                add_check(
                    "instrument_resolution",
                    CHECK_PASS,
                    "symbol resolved to structurally supported metadata",
                    {
                        "instrument_id": str(resolution.instrument_id),
                        "internal_symbol_full": resolution.internal_symbol_full,
                        "structural_status": resolution.structural_status,
                    },
                )
                _add_market_status_check(add_check, market_status)
        except EtoroApiError as exc:
            add_check(
                "instrument_resolution",
                CHECK_FAIL,
                f"instrument could not be resolved: {exc.category.value}",
                _failure_metadata(exc),
            )
            _add_blocked_after_instrument(add_check)
            return _finalize_report(
                now,
                checks,
                credentials_configured=True,
                authentication_attempted=True,
                authentication_successful=authentication_successful,
                broker_identity_verified=identity_verified,
                store=store,
            )
        except (RuntimeError, ValueError):
            add_check("instrument_resolution", CHECK_FAIL, "instrument could not be resolved")
            _add_blocked_after_instrument(add_check)
            return _finalize_report(
                now,
                checks,
                credentials_configured=True,
                authentication_attempted=True,
                authentication_successful=authentication_successful,
                broker_identity_verified=identity_verified,
                store=store,
            )

    if instrument is None:
        add_check("live_rates", CHECK_BLOCKED, "verified instrument metadata is required")
    else:
        try:
            quote = read_client.quote(instrument.instrument_id, instrument.symbol).model_copy(
                update={"market_status": market_status}
            )
            readiness_rate_status = _validate_market_rates_for_readiness(
                quote,
                instrument,
                now=now,
                maximum_age_seconds=settings.maximum_quote_age_seconds,
            )
            add_check(
                "live_rates",
                CHECK_PASS,
                "fresh bid/ask market rate was normalized"
                if readiness_rate_status == CHECK_PASS
                else "market data endpoint returned normalized rates while market is closed",
                {"market_status": quote.market_status.value},
            )
        except EtoroApiError as exc:
            add_check(
                "live_rates",
                CHECK_FAIL,
                f"fresh live market rate could not be verified: {exc.category.value}",
                _failure_metadata(exc),
            )
            _add_blocked_after_rates(add_check)
            return _finalize_report(
                now,
                checks,
                credentials_configured=True,
                authentication_attempted=True,
                authentication_successful=authentication_successful,
                broker_identity_verified=identity_verified,
                store=store,
            )
        except (RuntimeError, ValueError, MarketObservationError):
            add_check("live_rates", CHECK_FAIL, "fresh live market rate could not be verified")
            _add_blocked_after_rates(add_check)
            return _finalize_report(
                now,
                checks,
                credentials_configured=True,
                authentication_attempted=True,
                authentication_successful=authentication_successful,
                broker_identity_verified=identity_verified,
                store=store,
            )

    try:
        demo_portfolio = read_client.demo_account(identity)
        switch = KillSwitch(active=False, reason="readiness", clock=lambda: now)
        AccountIdentityGuard(
            identity,
            AccountKind.DEMO,
            switch,
        ).verify(identity, demo_portfolio.context)
        add_check("demo_portfolio", CHECK_PASS, "Demo aggregate portfolio was read")
    except EtoroApiError as exc:
        add_check(
            "demo_portfolio",
            CHECK_FAIL,
            f"Demo aggregate portfolio could not be verified: {exc.category.value}",
            _failure_metadata(exc),
        )
        _add_blocked_after_demo_portfolio(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=identity_verified,
            store=store,
        )
    except (RuntimeError, ValueError):
        add_check("demo_portfolio", CHECK_FAIL, "Demo aggregate portfolio could not be verified")
        _add_blocked_after_demo_portfolio(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=identity_verified,
            store=store,
        )

    try:
        symbol_map = {instrument.instrument_id: instrument.symbol} if instrument is not None else {}
        real_portfolio = read_client.real_portfolio_read_only(symbol_map)
        add_check("real_portfolio_read_only", CHECK_PASS, "real portfolio was read without writes")
    except EtoroApiError as exc:
        add_check(
            "real_portfolio_read_only",
            CHECK_NOT_AVAILABLE,
            f"real portfolio read-only endpoint was not available: {exc.category.value}",
            _failure_metadata(exc),
        )
        _add_blocked_after_real_portfolio(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=identity_verified,
            store=store,
        )
    except (RuntimeError, ValueError):
        add_check(
            "real_portfolio_read_only",
            CHECK_NOT_AVAILABLE,
            "real portfolio read-only endpoint was not available",
        )
        _add_blocked_after_real_portfolio(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=identity_verified,
            store=store,
        )

    try:
        assert instrument is not None
        eligibility = read_client.demo_eligibility(instrument.instrument_id, instrument.symbol)
        eligibility_ready = eligibility.verified and eligibility.allow_open
        add_check(
            "demo_eligibility",
            CHECK_PASS if eligibility_ready else CHECK_FAIL,
            "Demo eligibility was verified"
            if eligibility_ready
            else "Demo eligibility does not allow opening this instrument",
        )
        if not eligibility_ready:
            _add_blocked_after_eligibility(add_check)
            return _finalize_report(
                now,
                checks,
                credentials_configured=True,
                authentication_attempted=True,
                authentication_successful=authentication_successful,
                broker_identity_verified=identity_verified,
                store=store,
            )
    except EtoroApiError as exc:
        add_check(
            "demo_eligibility",
            CHECK_FAIL,
            f"Demo eligibility could not be verified: {exc.category.value}",
            _failure_metadata(exc),
        )
        _add_blocked_after_eligibility(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=identity_verified,
            store=store,
        )
    except (RuntimeError, ValueError):
        add_check("demo_eligibility", CHECK_FAIL, "Demo eligibility could not be verified")
        _add_blocked_after_eligibility(add_check)
        return _finalize_report(
            now,
            checks,
            credentials_configured=True,
            authentication_attempted=True,
            authentication_successful=authentication_successful,
            broker_identity_verified=identity_verified,
            store=store,
        )

    if instrument is None or quote is None or real_portfolio is None:
        add_check("shadow_mode_live_data", CHECK_BLOCKED, "live reads are incomplete")
    elif store is None:
        add_check("shadow_mode_live_data", CHECK_BLOCKED, "record store is not configured")
    else:
        try:
            _run_read_only_shadow(
                identity=identity,
                portfolio=real_portfolio,
                quote=quote,
                instrument=instrument,
                config=config,
                clock=lambda: now,
                store=store,
            )
            add_check("shadow_mode_live_data", CHECK_PASS, "Shadow Mode consumed live reads only")
        except (RuntimeError, ValueError):
            add_check("shadow_mode_live_data", CHECK_FAIL, "Shadow Mode live-data run failed")

    return _finalize_report(
        now,
        checks,
        credentials_configured=True,
        authentication_attempted=True,
        authentication_successful=authentication_successful,
        broker_identity_verified=identity_verified,
        store=store,
    )


def _live_client(credentials: EtoroCredentials, config: ApplicationConfig) -> EtoroReadClient:
    return EtoroReadClient(
        credentials, DisciplinedHttpClient(UrllibTransport(config.etoro_transport_mode))
    )


def default_etoro_readiness_store(path: Path | None = None) -> SqliteRecordStore:
    return SqliteRecordStore(DEFAULT_READINESS_STORE_PATH if path is None else path)


def _identity_matches(
    identity: BrokerIdentity, expected_username: str | None, expected_gcid: str | None
) -> bool:
    username_matches = expected_username is None or (
        identity.username is not None
        and identity.username.casefold() == expected_username.casefold()
    )
    gcid_matches = expected_gcid is None or identity.stable_user_id == expected_gcid
    return username_matches and gcid_matches


def _failure_metadata(exc: EtoroApiError) -> dict[str, str]:
    metadata = exc.safe_metadata()
    body = (exc.response_body or "").strip()
    if body == "error code: 1010":
        metadata["response_body"] = body
    return metadata


def _metadata_from_resolution(
    resolution: InstrumentResolution, now: datetime
) -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id=resolution.instrument_id,
        symbol=resolution.symbol,
        asset_class=asset_class_from_etoro_instrument_type(resolution.instrument_type),
        settlement_type=SettlementType.REAL,
        is_valid=resolution.structurally_supported,
        is_tradable=resolution.structurally_supported,
        allows_long=resolution.is_buy_enabled is True,
        allows_short=False,
        allowed_leverages=(1,),
        metadata_as_of=now,
        source="etoro-official-api-search",
    )


def _add_market_status_check(
    add_check: Callable[[str, str, str, Mapping[str, str] | None], None],
    market_status: MarketStatus,
) -> None:
    if market_status is MarketStatus.OPEN:
        add_check(
            "market_currently_open",
            CHECK_PASS,
            "instrument market is currently reported open",
            {"market_status": market_status.value},
        )
    elif market_status is MarketStatus.CLOSED:
        add_check(
            "market_currently_open",
            CHECK_MARKET_CLOSED,
            "instrument market is currently reported closed",
            {"market_status": market_status.value},
        )
    else:
        add_check(
            "market_currently_open",
            CHECK_UNKNOWN,
            "instrument market open status is not present in search metadata",
            {"market_status": market_status.value},
        )


def _validate_market_rates_for_readiness(
    quote: MarketQuote,
    instrument: InstrumentMetadata,
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> str:
    try:
        validate_market_observation(
            quote,
            instrument,
            now=now,
            maximum_age_seconds=maximum_age_seconds,
        )
    except MarketObservationError as exc:
        if quote.market_status is MarketStatus.CLOSED and str(exc) == "quote is stale":
            return CHECK_MARKET_CLOSED
        raise
    return CHECK_PASS


def _run_read_only_shadow(
    *,
    identity: BrokerIdentity,
    portfolio: PortfolioSnapshot,
    quote: MarketQuote,
    instrument: InstrumentMetadata,
    config: ApplicationConfig,
    clock: Callable[[], datetime],
    store: SqliteRecordStore,
) -> None:
    ShadowService(
        broker=_ReadinessBroker(
            identity=identity,
            portfolio=portfolio,
            quote=quote,
            instrument=instrument,
        ),
        news=_EmptyNewsProvider(),
        agent=DeterministicAegisAgent(),
        risk_manager=RiskManager(
            config.risk,
            KillSwitch(active=False, reason="readiness shadow", clock=clock),
            authorization_key=b"readiness-shadow-risk-key-32bytes!",
            clock=clock,
        ),
        strategy=config.strategy,
        recorder=ShadowRecorder(store),
        clock=clock,
        strategy_version="etoro-readiness-v1",
    ).run(quote.symbol, quote.instrument_id)


def _add_blocked_live_checks(add_check: Callable[[str, str, str], None]) -> None:
    for name in (
        "authentication",
        "identity_match",
        "instrument_resolution",
        "market_currently_open",
        "live_rates",
        "demo_portfolio",
        "real_portfolio_read_only",
        "demo_eligibility",
        "shadow_mode_live_data",
    ):
        add_check(name, CHECK_BLOCKED, "credentials and API enablement are required")


def _add_blocked_after_identity(add_check: Callable[[str, str, str], None]) -> None:
    for name in (
        "instrument_resolution",
        "market_currently_open",
        "live_rates",
        "demo_portfolio",
        "real_portfolio_read_only",
        "demo_eligibility",
        "shadow_mode_live_data",
    ):
        add_check(name, CHECK_BLOCKED, "verified broker identity is required")


def _add_blocked_after_instrument(add_check: Callable[[str, str, str], None]) -> None:
    for name in (
        "market_currently_open",
        "live_rates",
        "demo_portfolio",
        "real_portfolio_read_only",
        "demo_eligibility",
        "shadow_mode_live_data",
    ):
        add_check(name, CHECK_BLOCKED, "verified instrument metadata is required")


def _add_blocked_after_rates(add_check: Callable[[str, str, str], None]) -> None:
    for name in (
        "demo_portfolio",
        "real_portfolio_read_only",
        "demo_eligibility",
        "shadow_mode_live_data",
    ):
        add_check(name, CHECK_BLOCKED, "fresh live market rates are required")


def _add_blocked_after_demo_portfolio(add_check: Callable[[str, str, str], None]) -> None:
    for name in ("real_portfolio_read_only", "demo_eligibility", "shadow_mode_live_data"):
        add_check(name, CHECK_BLOCKED, "verified Demo portfolio read is required")


def _add_blocked_after_real_portfolio(add_check: Callable[[str, str, str], None]) -> None:
    for name in ("demo_eligibility", "shadow_mode_live_data"):
        add_check(name, CHECK_BLOCKED, "verified real portfolio read-only access is required")


def _add_blocked_after_eligibility(add_check: Callable[[str, str, str], None]) -> None:
    add_check("shadow_mode_live_data", CHECK_BLOCKED, "verified Demo eligibility is required")


def _finalize_report(
    generated_at: datetime,
    checks: list[EtoroReadinessCheck],
    *,
    credentials_configured: bool,
    authentication_attempted: bool = False,
    authentication_successful: bool = False,
    broker_identity_verified: bool = False,
    store: SqliteRecordStore | None = None,
) -> EtoroReadinessReport:
    report = EtoroReadinessReport(
        generated_at=generated_at,
        status=_overall_status(checks),
        checks=tuple(checks),
        credentials_configured=credentials_configured,
        authentication_attempted=authentication_attempted,
        authentication_successful=authentication_successful,
        broker_identity_verified=broker_identity_verified,
        live_rates_verified=_passed(checks, "live_rates"),
        demo_portfolio_read_verified=_passed(checks, "demo_portfolio"),
        real_portfolio_read_only_verified=_passed(checks, "real_portfolio_read_only"),
        demo_eligibility_verified=_passed(checks, "demo_eligibility"),
        shadow_mode_live_data_verified=_passed(checks, "shadow_mode_live_data"),
        demo_execution_ready=False,
        real_execution_available=False,
        demo_auto_execution_enabled=False,
    )
    if store is not None:
        store.append("etoro-readiness-report", report.model_dump(mode="json"))
    return report


def _passed(checks: list[EtoroReadinessCheck], name: str) -> bool:
    return any(check.name == name and check.status == CHECK_PASS for check in checks)


def _overall_status(checks: list[EtoroReadinessCheck]) -> str:
    statuses = {check.status for check in checks}
    if CHECK_FAIL in statuses:
        return CHECK_FAIL
    if CHECK_NOT_CONFIGURED in statuses:
        return CHECK_NOT_CONFIGURED
    if CHECK_BLOCKED in statuses:
        return CHECK_BLOCKED
    if CHECK_NOT_AVAILABLE in statuses:
        return CHECK_BLOCKED
    return "READ_ONLY_READY"
