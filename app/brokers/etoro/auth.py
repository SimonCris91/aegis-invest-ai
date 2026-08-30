"""eToro header authentication with redacted credentials."""

from collections.abc import Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, SecretStr


class EtoroCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_key: SecretStr
    user_key: SecretStr

    def headers(self, uuid_factory: Callable[[], UUID] = uuid4) -> dict[str, str]:
        return {
            "x-api-key": self.api_key.get_secret_value(),
            "x-user-key": self.user_key.get_secret_value(),
            "x-request-id": str(uuid_factory()),
        }
