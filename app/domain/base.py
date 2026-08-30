"""Shared validation helpers for immutable domain models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FrozenDomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value
