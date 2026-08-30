"""Grounding checks for AI-authored explanations."""

from pydantic import Field

from app.domain.base import FrozenDomainModel


class GroundedClaim(FrozenDomainModel):
    text: str = Field(min_length=1)
    source_fact_id: str = Field(min_length=1)


class GroundingValidation(FrozenDomainModel):
    accepted: bool
    rejected_claims: tuple[str, ...] = ()


class GroundedExplanationValidator:
    def validate(
        self, *, claims: tuple[GroundedClaim, ...], allowed_fact_ids: frozenset[str]
    ) -> GroundingValidation:
        rejected = tuple(
            claim.text for claim in claims if claim.source_fact_id not in allowed_fact_ids
        )
        return GroundingValidation(accepted=not rejected, rejected_claims=rejected)
