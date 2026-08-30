"""Broker-neutral account identity continuity guard."""

from app.brokers.models import AccountKind, BrokerAccountContext, BrokerIdentity
from app.risk.kill_switch import KillSwitch


class AccountIdentityError(RuntimeError):
    pass


class AccountIdentityGuard:
    def __init__(
        self, identity: BrokerIdentity, kind: AccountKind, kill_switch: KillSwitch
    ) -> None:
        self._identity = identity
        self._kind = kind
        self._kill_switch = kill_switch

    @property
    def context(self) -> BrokerAccountContext:
        account_id = (
            self._identity.demo_account_id
            if self._kind is AccountKind.DEMO
            else self._identity.real_account_id
        )
        return BrokerAccountContext(
            stable_user_id=self._identity.stable_user_id,
            account_id=account_id,
            kind=self._kind,
        )

    def verify(self, identity: BrokerIdentity, context: BrokerAccountContext) -> None:
        if identity != self._identity or context != self.context:
            self._kill_switch.activate("broker identity or account context changed")
            raise AccountIdentityError("broker identity continuity check failed")
