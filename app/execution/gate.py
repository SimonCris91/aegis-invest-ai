"""One-time admission gate for a future Order Manager."""

from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from uuid import UUID, uuid4

from app.domain.proposals import TradeProposal
from app.domain.risk import RiskAuthorization
from app.risk.manager import RiskManager


class AuthorizationAlreadyConsumedError(PermissionError):
    """Raised when the same risk capability is presented more than once."""


@dataclass(frozen=True, slots=True)
class AuthorizedTrade:
    proposal: TradeProposal
    authorization: RiskAuthorization
    admission_id: UUID
    admitted_at: datetime
    _origin_seal: object = field(repr=False, compare=False)


class RiskEnforcedExecutionGate:
    """Produces the only input type a future Order Manager may accept."""

    def __init__(self, risk_manager: RiskManager) -> None:
        self._risk_manager = risk_manager
        self._lock = RLock()
        self._consumed_authorizations: set[str] = set()
        self._admissions: dict[UUID, tuple[TradeProposal, RiskAuthorization, object]] = {}
        self._origin_seal = object()

    def admit(
        self,
        proposal: TradeProposal,
        authorization: RiskAuthorization,
        *,
        at: datetime | None = None,
    ) -> AuthorizedTrade:
        self._risk_manager.assert_authorized(proposal, authorization, at=at)
        authorization_id = str(authorization.authorization_id)
        with self._lock:
            if authorization_id in self._consumed_authorizations:
                raise AuthorizationAlreadyConsumedError(
                    "risk authorization has already been consumed"
                )
            self._consumed_authorizations.add(authorization_id)
            admission_id = uuid4()
            admitted = AuthorizedTrade(
                proposal=proposal,
                authorization=authorization,
                admission_id=admission_id,
                admitted_at=at or authorization.issued_at,
                _origin_seal=self._origin_seal,
            )
            self._admissions[admission_id] = (proposal, authorization, self._origin_seal)
        return admitted

    def assert_admitted(self, admitted: AuthorizedTrade, *, at: datetime | None = None) -> None:
        """Verify that this exact capability was minted by this gate."""

        if not isinstance(admitted, AuthorizedTrade):
            raise PermissionError("paper execution requires an AuthorizedTrade")
        with self._lock:
            record = self._admissions.get(admitted.admission_id)
            if (
                record is None
                or admitted._origin_seal is not self._origin_seal
                or record != (admitted.proposal, admitted.authorization, self._origin_seal)
            ):
                raise PermissionError("trade was not admitted by this execution gate")
        self._risk_manager.assert_authorized(admitted.proposal, admitted.authorization, at=at)
