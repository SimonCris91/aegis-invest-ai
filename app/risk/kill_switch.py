"""Thread-safe global kill switch with explicit state transitions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    active: bool
    changed_at: datetime
    reason: str


class KillSwitch:
    def __init__(
        self,
        *,
        active: bool = True,
        reason: str = "safe default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._state = KillSwitchState(active=active, changed_at=self._clock(), reason=reason)

    @property
    def state(self) -> KillSwitchState:
        with self._lock:
            return self._state

    def activate(self, reason: str) -> KillSwitchState:
        if not reason.strip():
            raise ValueError("kill switch activation requires a reason")
        with self._lock:
            self._state = KillSwitchState(active=True, changed_at=self._clock(), reason=reason)
            return self._state

    def deactivate(self, reason: str) -> KillSwitchState:
        if not reason.strip():
            raise ValueError("kill switch deactivation requires a reason")
        with self._lock:
            self._state = KillSwitchState(active=False, changed_at=self._clock(), reason=reason)
            return self._state
