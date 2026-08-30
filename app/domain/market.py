"""Normalized market and news inputs; no provider-specific schemas live here."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass, Currency, MarketStatus, SettlementType


class EvidenceItem(FrozenDomainModel):
    source: str = Field(min_length=1)
    timestamp: datetime
    summary: str = Field(min_length=1)
    confidence: Decimal = Field(ge=0, le=1)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")


class NewsItem(FrozenDomainModel):
    news_id: str | None = Field(default=None, min_length=1)
    source: str = Field(min_length=1)
    timestamp: datetime
    headline: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    asset_relevance: tuple[str, ...]
    url: str | None = Field(default=None, min_length=1)
    sentiment: Decimal = Field(ge=-1, le=1)
    importance: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")

    @property
    def published_at(self) -> datetime:
        return self.timestamp

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.asset_relevance

    @property
    def deduplication_key(self) -> str:
        if self.news_id is not None:
            return f"id:{self.news_id.casefold()}"
        return "content:" + "|".join(
            (self.source.casefold(), self.headline.casefold(), self.timestamp.isoformat())
        )


class MarketQuote(FrozenDomainModel):
    """Provider-neutral quote. Raw provider payloads never cross this boundary."""

    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    price: Decimal = Field(gt=0)
    as_of: datetime
    currency: Currency
    source: str = Field(min_length=1)
    previous_close: Decimal | None = Field(default=None, gt=0)
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    market_status: MarketStatus = MarketStatus.UNKNOWN

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")

    @model_validator(mode="after")
    def bid_does_not_exceed_ask(self) -> "MarketQuote":
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        return self

    @property
    def timestamp(self) -> datetime:
        return self.as_of

    def to_price_snapshot(self) -> "PriceSnapshot":
        return PriceSnapshot(
            instrument_id=self.instrument_id,
            symbol=self.symbol,
            price=self.price,
            as_of=self.as_of,
            source=self.source,
        )

    def is_fresh(self, *, as_of: datetime, max_age_seconds: int) -> bool:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("freshness comparison time must include a timezone")
        age = as_of - self.as_of
        return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)


class PriceSnapshot(FrozenDomainModel):
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    price: Decimal = Field(gt=0)
    as_of: datetime
    source: str = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "as_of")


class InstrumentMetadata(FrozenDomainModel):
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    asset_class: AssetClass = AssetClass.UNKNOWN
    settlement_type: SettlementType
    is_valid: bool
    is_tradable: bool
    allows_long: bool
    allows_short: bool
    allowed_leverages: tuple[int, ...]
    min_position_amount: Decimal | None = Field(default=None, gt=0)
    metadata_as_of: datetime
    source: str = Field(min_length=1)

    @field_validator("metadata_as_of")
    @classmethod
    def metadata_as_of_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "metadata_as_of")
