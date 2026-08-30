"""Secret-free structured audit events."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AuditEventType, RiskDecisionStatus, RiskViolationCode


class AuditEvent(FrozenDomainModel):
    event_id: UUID
    event_type: AuditEventType
    timestamp: datetime
    correlation_id: UUID
    proposal_id: UUID | None = None
    portfolio_value: Decimal | None = Field(default=None, ge=0)
    decision: RiskDecisionStatus | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    risk_violations: tuple[RiskViolationCode, ...] = ()
    result: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "timestamp")
