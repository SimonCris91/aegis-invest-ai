"""Deterministic event-risk and macro context normalization."""

from datetime import datetime, timedelta
from decimal import Decimal

from app.data.models import (
    DataProviderStatus,
    EarningsProximity,
    EventCategory,
    EventRiskAssessment,
    EventRiskItem,
    HistoricalDataQualityStatus,
)
from app.domain.enums import AssetClass
from app.domain.universe import UniversalInstrument


class StaticEventRiskProvider:
    provider_name = "static-events"

    def __init__(self, events: tuple[EventRiskItem, ...]) -> None:
        self._events = events

    def assess_events(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> EventRiskAssessment:
        relevant = tuple(
            event
            for event in self._events
            if event.instrument.key == instrument.key
            and (event.scheduled_at is None or event.scheduled_at >= as_of)
        )
        return EventRiskEngine().assess(
            instrument=instrument,
            events=relevant,
            as_of=as_of,
            provider=self.provider_name,
        )


class NullEventRiskProvider:
    provider_name = "none"

    def assess_events(
        self, instrument: UniversalInstrument, *, as_of: datetime
    ) -> EventRiskAssessment:
        return EventRiskAssessment(
            provider=self.provider_name,
            instrument=instrument,
            as_of=as_of,
            status=DataProviderStatus.DATA_INSUFFICIENT,
            quality=HistoricalDataQualityStatus.INSUFFICIENT,
            reasons=("EVENT_RISK_NOT_CONFIGURED", "MACRO_NOT_CONFIGURED"),
        )


class EventRiskEngine:
    def assess(
        self,
        *,
        instrument: UniversalInstrument,
        events: tuple[EventRiskItem, ...],
        as_of: datetime,
        provider: str,
    ) -> EventRiskAssessment:
        if not events:
            return EventRiskAssessment(
                provider=provider,
                instrument=instrument,
                as_of=as_of,
                events=(),
                earnings_proximity=EarningsProximity.UNKNOWN,
                macro_status="MACRO_NOT_CONFIGURED",
                status=DataProviderStatus.DATA_INSUFFICIENT,
                quality=HistoricalDataQualityStatus.INSUFFICIENT,
                reasons=("no provider-backed event risk",),
            )
        earnings = tuple(event for event in events if event.category is EventCategory.EARNINGS)
        proximity = _earnings_proximity(earnings, as_of=as_of)
        reasons: list[str] = []
        if proximity is EarningsProximity.EARNINGS_IMMINENT:
            reasons.append("earnings event is imminent")
        if instrument.asset_class is AssetClass.CRYPTO and any(
            event.category is EventCategory.TOKEN_EVENT for event in events
        ):
            reasons.append("provider-backed token event risk exists")
        if any(event.category is EventCategory.MACRO_EVENT for event in events):
            reasons.append("macro event risk exists")
        quality = (
            HistoricalDataQualityStatus.PARTIAL
            if any(event.confidence < Decimal("0.70") for event in events)
            else HistoricalDataQualityStatus.GOOD
        )
        return EventRiskAssessment(
            provider=provider,
            instrument=instrument,
            as_of=as_of,
            events=events,
            earnings_proximity=proximity,
            macro_status=(
                "MACRO_AVAILABLE"
                if any(event.category is EventCategory.MACRO_EVENT for event in events)
                else "MACRO_NOT_CONFIGURED"
            ),
            status=DataProviderStatus.SUCCESS,
            quality=quality,
            reasons=tuple(reasons),
        )


def event_risk_item(
    *,
    instrument: UniversalInstrument,
    category: EventCategory,
    severity: Decimal,
    confidence: Decimal,
    source: str,
    description: str,
    scheduled_at: datetime | None = None,
) -> EventRiskItem:
    return EventRiskItem(
        instrument=instrument,
        category=category,
        scheduled_at=scheduled_at,
        severity=severity,
        confidence=confidence,
        source=source,
        description=description,
    )


def _earnings_proximity(
    earnings: tuple[EventRiskItem, ...], *, as_of: datetime
) -> EarningsProximity:
    if not earnings:
        return EarningsProximity.NO_IMMEDIATE_EARNINGS
    future_dates = tuple(event.scheduled_at for event in earnings if event.scheduled_at is not None)
    if not future_dates:
        return EarningsProximity.UNKNOWN
    nearest = min(future_dates)
    distance = nearest - as_of
    if distance <= timedelta(days=3):
        return EarningsProximity.EARNINGS_IMMINENT
    if distance <= timedelta(days=14):
        return EarningsProximity.EARNINGS_SOON
    return EarningsProximity.NO_IMMEDIATE_EARNINGS
