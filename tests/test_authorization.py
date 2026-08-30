from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.proposals import TradeProposal
from app.domain.risk import RiskAuthorization, RiskContext
from app.execution.gate import AuthorizationAlreadyConsumedError, RiskEnforcedExecutionGate
from app.risk.manager import RiskAuthorizationError, RiskManager


def approved_authorization(
    manager: RiskManager, proposal: TradeProposal, context: RiskContext
) -> RiskAuthorization:
    evaluation = manager.evaluate(proposal, context)
    assert evaluation.authorization is not None
    return evaluation.authorization


def test_valid_authorization_passes_programmatic_gate(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
) -> None:
    authorization = approved_authorization(risk_manager, proposal, context)
    gate = RiskEnforcedExecutionGate(risk_manager)

    admitted = gate.admit(proposal, authorization, at=now)

    assert admitted.proposal == proposal
    assert admitted.authorization == authorization


def test_forged_signature_is_rejected(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
) -> None:
    authorization = approved_authorization(risk_manager, proposal, context)
    forged = authorization.model_copy(update={"signature": "f" * 64})

    with pytest.raises(RiskAuthorizationError, match="signature is invalid"):
        risk_manager.assert_authorized(proposal, forged, at=now)


def test_proposal_change_after_approval_is_rejected(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
) -> None:
    authorization = approved_authorization(risk_manager, proposal, context)
    changed = proposal.model_copy(update={"amount": Decimal("11")})

    with pytest.raises(RiskAuthorizationError, match="changed after risk approval"):
        risk_manager.assert_authorized(changed, authorization, at=now)


def test_expired_authorization_is_rejected(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
) -> None:
    authorization = approved_authorization(risk_manager, proposal, context)

    with pytest.raises(RiskAuthorizationError, match="expired"):
        risk_manager.assert_authorized(proposal, authorization, at=now + timedelta(seconds=121))


def test_authorization_is_consumed_once(
    risk_manager: RiskManager,
    proposal: TradeProposal,
    context: RiskContext,
    now: datetime,
) -> None:
    authorization = approved_authorization(risk_manager, proposal, context)
    gate = RiskEnforcedExecutionGate(risk_manager)
    gate.admit(proposal, authorization, at=now)

    with pytest.raises(AuthorizationAlreadyConsumedError, match="already been consumed"):
        gate.admit(proposal, authorization, at=now)
