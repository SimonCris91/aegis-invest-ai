"""Scanner configuration models."""

from pydantic import Field, model_validator

from app.domain.base import FrozenDomainModel


class ScannerLimits(FrozenDomainModel):
    discovery_limit: int = Field(default=50, ge=1, le=500)
    ranked_shortlist_limit: int = Field(default=20, ge=1, le=100)
    deep_analysis_limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def limits_are_ordered(self) -> "ScannerLimits":
        if self.deep_analysis_limit > self.ranked_shortlist_limit:
            raise ValueError("deep_analysis_limit cannot exceed ranked_shortlist_limit")
        if self.ranked_shortlist_limit > self.discovery_limit:
            raise ValueError("ranked_shortlist_limit cannot exceed discovery_limit")
        return self
