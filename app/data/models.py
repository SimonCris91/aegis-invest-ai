"""Provider-neutral live data, quality, mapping, and research models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass, Currency
from app.domain.universe import UniversalInstrument
from app.intelligence.models import AegisOpportunityAnalysis, MarketBar, TimeFrame


class DataProviderStatus(StrEnum):
    SUCCESS = "SUCCESS"
    CACHE_HIT = "CACHE_HIT"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    DATA_CONFLICT = "DATA_CONFLICT"
    MAPPING_AMBIGUOUS = "MAPPING_AMBIGUOUS"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    ACCEPTABLE = "ACCEPTABLE"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class HistoricalDataQualityStatus(StrEnum):
    GOOD = "GOOD"
    PARTIAL = "PARTIAL"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"


class EventCategory(StrEnum):
    EARNINGS = "EARNINGS"
    DIVIDEND = "DIVIDEND"
    ECONOMIC_DATA = "ECONOMIC_DATA"
    CENTRAL_BANK = "CENTRAL_BANK"
    REGULATORY = "REGULATORY"
    LEGAL = "LEGAL"
    PRODUCT_EVENT = "PRODUCT_EVENT"
    TOKEN_EVENT = "TOKEN_EVENT"
    LISTING_DELISTING = "LISTING_DELISTING"
    MACRO_EVENT = "MACRO_EVENT"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class EarningsProximity(StrEnum):
    EARNINGS_IMMINENT = "EARNINGS_IMMINENT"
    EARNINGS_SOON = "EARNINGS_SOON"
    NO_IMMEDIATE_EARNINGS = "NO_IMMEDIATE_EARNINGS"
    UNKNOWN = "UNKNOWN"


class ResearchLabel(StrEnum):
    GOOD_SIGNAL = "GOOD_SIGNAL"
    BAD_SIGNAL = "BAD_SIGNAL"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_HORIZON = "INSUFFICIENT_HORIZON"


class DataProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: DataProviderStatus,
        http_status: int | None = None,
        sanitized_endpoint: str | None = None,
        transport_category: str | None = None,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
        retry_after: str | None = None,
        exception_type: str | None = None,
        exception_message: str | None = None,
        errno: int | None = None,
        winerror: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status
        self.sanitized_endpoint = sanitized_endpoint
        self.transport_category = transport_category
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        self.retry_after = retry_after
        self.exception_type = exception_type
        self.exception_message = exception_message
        self.errno = errno
        self.winerror = winerror

    def safe_diagnostics(self) -> dict[str, object]:
        return {
            "provider_status": self.status.value,
            "http_status": self.http_status,
            "sanitized_endpoint": self.sanitized_endpoint,
            "transport_category": self.transport_category,
            "provider_error_code": self.provider_error_code,
            "provider_error_message": self.provider_error_message,
            "retry_after": self.retry_after,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "errno": self.errno,
            "winerror": self.winerror,
        }


class ProviderInstrumentReference(FrozenDomainModel):
    provider: str = Field(min_length=1)
    provider_symbol: str = Field(min_length=1)
    broker: str = Field(min_length=1)
    broker_symbol: str = Field(min_length=1)
    broker_instrument_id: str = Field(min_length=1)
    exchange: str | None = Field(default=None, min_length=1)
    asset_class: AssetClass
    currency: Currency | None = None
    mapping_confidence: Decimal = Field(ge=0, le=1)
    mapping_source: str = Field(min_length=1)
    verified: bool = False


class InstrumentMappingResult(FrozenDomainModel):
    instrument: UniversalInstrument
    provider: str = Field(min_length=1)
    selected: ProviderInstrumentReference | None = None
    candidates: tuple[ProviderInstrumentReference, ...] = ()
    status: DataProviderStatus
    reasons: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.selected is not None and self.status is DataProviderStatus.SUCCESS


class ProviderProvenance(FrozenDomainModel):
    provider: str = Field(min_length=1)
    fetched_at: datetime
    source: str = Field(min_length=1)
    provider_symbol: str | None = Field(default=None, min_length=1)
    cached: bool = False

    @field_validator("fetched_at")
    @classmethod
    def fetched_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "provider fetch timestamp")


class HistoricalDataQualityReport(FrozenDomainModel):
    provider: str = Field(min_length=1)
    instrument: UniversalInstrument
    timeframe: TimeFrame
    status: HistoricalDataQualityStatus
    freshness: FreshnessStatus
    bar_count: int = Field(ge=0)
    expected_minimum_bars: int = Field(ge=1)
    duplicate_timestamps: int = Field(ge=0)
    out_of_order: bool = False
    missing_bars_estimate: int = Field(default=0, ge=0)
    ohlc_inconsistencies: int = Field(default=0, ge=0)
    currency_mismatch: bool = False
    extreme_anomalies: int = Field(default=0, ge=0)
    provider_gaps: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class HistoricalDataset(FrozenDomainModel):
    instrument: UniversalInstrument
    timeframe: TimeFrame
    bars: tuple[MarketBar, ...]
    provider: str = Field(min_length=1)
    fetched_at: datetime
    quality: HistoricalDataQualityReport
    provenance: ProviderProvenance

    @field_validator("fetched_at")
    @classmethod
    def fetched_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "historical dataset fetch timestamp")


class MultiProviderHistoricalResult(FrozenDomainModel):
    instrument: UniversalInstrument
    as_of: datetime
    datasets: tuple[HistoricalDataset, ...]
    bars_by_timeframe: dict[TimeFrame, tuple[MarketBar, ...]]
    provider_statuses: dict[str, DataProviderStatus]
    status: DataProviderStatus
    consistency_status: HistoricalDataQualityStatus
    reasons: tuple[str, ...] = ()
    broker_write_calls: int = Field(default=0, ge=0, le=0)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "historical result timestamp")


class NormalizedNewsItem(FrozenDomainModel):
    instrument: UniversalInstrument
    headline: str = Field(min_length=1)
    source: str = Field(min_length=1)
    published_at: datetime
    event_type: EventCategory = EventCategory.UNKNOWN
    sentiment_score: Decimal | None = Field(default=None, ge=Decimal("-1"), le=Decimal("1"))
    sentiment_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    relevance: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    url_or_reference: str | None = Field(default=None, min_length=1)
    data_quality: HistoricalDataQualityStatus = HistoricalDataQualityStatus.PARTIAL

    @field_validator("published_at")
    @classmethod
    def published_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "news timestamp")

    @property
    def dedupe_key(self) -> str:
        return "|".join(
            (
                self.source.casefold(),
                self.headline.casefold(),
                self.published_at.date().isoformat(),
                self.instrument.key,
            )
        )


class NewsProviderResult(FrozenDomainModel):
    provider: str = Field(min_length=1)
    instrument: UniversalInstrument
    as_of: datetime
    items: tuple[NormalizedNewsItem, ...]
    status: DataProviderStatus
    quality: HistoricalDataQualityStatus
    reasons: tuple[str, ...] = ()

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "news result timestamp")


class EventRiskItem(FrozenDomainModel):
    instrument: UniversalInstrument
    category: EventCategory
    scheduled_at: datetime | None = None
    severity: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    source: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_at_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "event timestamp")


class EventRiskAssessment(FrozenDomainModel):
    provider: str = Field(min_length=1)
    instrument: UniversalInstrument
    as_of: datetime
    events: tuple[EventRiskItem, ...] = ()
    earnings_proximity: EarningsProximity = EarningsProximity.UNKNOWN
    macro_status: str = "MACRO_NOT_CONFIGURED"
    status: DataProviderStatus = DataProviderStatus.DATA_INSUFFICIENT
    quality: HistoricalDataQualityStatus = HistoricalDataQualityStatus.INSUFFICIENT
    reasons: tuple[str, ...] = ()

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "event risk timestamp")


class CandidateObservation(FrozenDomainModel):
    observation_id: str = Field(min_length=1)
    analysis: AegisOpportunityAnalysis
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "research observation timestamp")


class ResearchOutcomeSnapshot(FrozenDomainModel):
    observation_id: str = Field(min_length=1)
    observed_at: datetime
    horizon: TimeFrame
    reference_price: Decimal = Field(gt=0)
    future_price: Decimal = Field(gt=0)
    maximum_favorable_excursion: Decimal
    maximum_adverse_excursion: Decimal
    drawdown: Decimal = Field(ge=0)
    ranking_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    label: ResearchLabel

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "research outcome timestamp")

    @model_validator(mode="after")
    def excursions_have_consistent_signs(self) -> ResearchOutcomeSnapshot:
        if self.maximum_favorable_excursion < self.maximum_adverse_excursion:
            raise ValueError("favorable excursion must be >= adverse excursion")
        return self
