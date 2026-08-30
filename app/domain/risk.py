"""Risk inputs, deterministic decisions, and cryptographically sealed authorization."""

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import RiskDecisionStatus, RiskViolationCode
from app.domain.market import InstrumentMetadata, PriceSnapshot
from app.domain.portfolio import PortfolioSnapshot


class RiskViolation(FrozenDomainModel):
    code: RiskViolationCode
    message: str = Field(min_length=1)


class RiskDecision(FrozenDomainModel):
    decision_id: UUID
    proposal_id: UUID
    status: RiskDecisionStatus
    evaluated_at: datetime
    violations: tuple[RiskViolation, ...]
    metrics: dict[str, str]

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "evaluated_at")

    @model_validator(mode="after")
    def decision_matches_violations(self) -> "RiskDecision":
        if self.status is RiskDecisionStatus.APPROVED and self.violations:
            raise ValueError("approved decisions cannot contain violations")
        if self.status is RiskDecisionStatus.REJECTED and not self.violations:
            raise ValueError("rejected decisions must contain at least one violation")
        return self


class RiskAuthorization(FrozenDomainModel):
    """Signed capability consumed by the future execution boundary."""

    authorization_id: UUID
    proposal_id: UUID
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "authorization timestamp")

    @model_validator(mode="after")
    def expiry_follows_issue(self) -> "RiskAuthorization":
        if self.expires_at <= self.issued_at:
            raise ValueError("authorization must expire after it is issued")
        return self


class RiskContext(FrozenDomainModel):
    evaluated_at: datetime
    portfolio: PortfolioSnapshot
    price: PriceSnapshot | None
    instrument: InstrumentMetadata | None
    market_data_available: bool
    news_data_available: bool
    daily_new_trade_count: int = Field(ge=0)
    recent_idempotency_keys: frozenset[str] = frozenset()
    api_state_consistent: bool = True
    critical_operational_error: str | None = None

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "evaluated_at")


class RiskEvaluation(FrozenDomainModel):
    decision: RiskDecision
    authorization: RiskAuthorization | None = None
    authorization_deferred: bool = False

    @model_validator(mode="after")
    def authorization_matches_status(self) -> "RiskEvaluation":
        approved = self.decision.status is RiskDecisionStatus.APPROVED
        if self.authorization is not None and not approved:
            raise ValueError("rejected decisions cannot carry an authorization")
        if approved and self.authorization is None and not self.authorization_deferred:
            raise ValueError("approved execution decisions must carry an authorization")
        if not approved and self.authorization_deferred:
            raise ValueError("authorization can be deferred only for approved decisions")
        return self
