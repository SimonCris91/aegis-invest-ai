"""Fail-closed reconciliation for uncertain or inconsistent Demo outcomes."""

from app.brokers.models import BrokerSubmission, ExecutionState
from app.risk.kill_switch import KillSwitch
from app.storage.sqlite import SqliteRecordStore


class ReconciliationError(RuntimeError):
    pass


def reconcile_submission(
    local: BrokerSubmission,
    observed_order_ids: frozenset[str],
    kill_switch: KillSwitch,
) -> bool:
    if local.state is ExecutionState.UNKNOWN or (
        local.state in {ExecutionState.SUBMITTED, ExecutionState.PENDING}
        and (local.broker_order_id is None or local.broker_order_id not in observed_order_ids)
    ):
        kill_switch.activate("Demo reconciliation mismatch")
        raise ReconciliationError("Demo state is not reconciled")
    return local.state in {
        ExecutionState.SUBMITTED,
        ExecutionState.PENDING,
        ExecutionState.FILLED,
        ExecutionState.PARTIALLY_FILLED,
    }


def reconcile_demo_state(
    local: BrokerSubmission,
    observed: ExecutionState,
    kill_switch: KillSwitch,
    registry: SqliteRecordStore,
) -> ExecutionState:
    if local.broker_order_id is None or observed is ExecutionState.UNKNOWN:
        kill_switch.activate("unknown Demo execution state")
        registry.update_demo_submission(
            local.idempotency_key,
            ExecutionState.UNKNOWN.value,
            {"broker_order_id": local.broker_order_id, "reconciliation": "unknown"},
        )
        raise ReconciliationError("Demo execution state is unknown")
    valid_transitions = {
        ExecutionState.SUBMITTED: {
            ExecutionState.SUBMITTED,
            ExecutionState.PENDING,
            ExecutionState.FILLED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.REJECTED,
            ExecutionState.CANCELLED,
        },
        ExecutionState.PENDING: {
            ExecutionState.PENDING,
            ExecutionState.FILLED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.REJECTED,
            ExecutionState.CANCELLED,
        },
        ExecutionState.PARTIALLY_FILLED: {
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.CANCELLED,
        },
    }
    allowed = valid_transitions.get(local.state, {local.state})
    if observed not in allowed:
        kill_switch.activate("Demo reconciliation divergence")
        raise ReconciliationError("invalid Demo state transition")
    registry.update_demo_submission(
        local.idempotency_key,
        observed.value,
        {"broker_order_id": local.broker_order_id, "reconciliation": "verified"},
    )
    return observed
