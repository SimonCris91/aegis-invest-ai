"""eToro Demo-only execution adapter with exact-route and replay guards."""

from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlparse

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.http import DisciplinedHttpClient, UnknownWriteOutcome
from app.brokers.models import (
    BrokerCapabilities,
    BrokerSubmission,
    ExecutionState,
    PreflightDecision,
)
from app.domain.enums import OperatingMode, SettlementType, TradeIntent, TradeSide
from app.execution.gate import AuthorizedTrade, RiskEnforcedExecutionGate
from app.risk.kill_switch import KillSwitch
from app.storage.sqlite import SqliteRecordStore

DEMO_ORDER_URL = "https://public-api.etoro.com/api/v3/trading/execution/demo/orders"


class DemoExecutionError(RuntimeError):
    pass


def assert_demo_route(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "public-api.etoro.com"
        or parsed.path != "/api/v3/trading/execution/demo/orders"
    ):
        raise DemoExecutionError("only the verified eToro Demo route is permitted")


class EtoroDemoAdapter:
    def __init__(
        self,
        *,
        credentials: EtoroCredentials,
        http: DisciplinedHttpClient,
        gate: RiskEnforcedExecutionGate,
        kill_switch: KillSwitch,
        registry: SqliteRecordStore,
        enabled: bool,
        explicit_opt_in: bool,
        clock: Callable[[], datetime],
    ) -> None:
        self._credentials = credentials
        self._http = http
        self._gate = gate
        self._kill_switch = kill_switch
        self._registry = registry
        self._enabled = enabled
        self._explicit_opt_in = explicit_opt_in
        self._clock = clock
        self.capabilities = BrokerCapabilities(
            provider="etoro",
            mode=OperatingMode.ETORO_DEMO,
            authenticated_reads=True,
            demo_execution=enabled and explicit_opt_in,
            real_execution=False,
        )

    def submit_demo(self, trade: AuthorizedTrade, preflight: PreflightDecision) -> BrokerSubmission:
        if not self._enabled:
            raise DemoExecutionError("Demo execution is disabled")
        if not self._explicit_opt_in:
            raise DemoExecutionError("explicit Demo smoke-test opt-in is required")
        self._assert_kill_switch_clear()
        if not preflight.allowed or preflight.minimum_trade_amount is None:
            raise DemoExecutionError("complete Demo preflight approval is required")
        self._gate.assert_admitted(trade, at=self._clock())
        proposal = trade.proposal
        if proposal.leverage != 1 or proposal.settlement_type is not SettlementType.REAL:
            raise DemoExecutionError("only unleveraged real-asset Demo orders are permitted")
        if (
            proposal.intent not in {TradeIntent.OPEN, TradeIntent.INCREASE}
            or proposal.side is not TradeSide.BUY
        ):
            raise DemoExecutionError("this adapter permits buy-side Demo opens only")
        assert_demo_route(DEMO_ORDER_URL)
        payload: dict[str, object] = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": proposal.instrument_id,
            "settlementType": "real",
            "orderType": "mkt",
            "leverage": 1,
            "amount": float(proposal.amount),
            "orderCurrency": proposal.currency.value.lower(),
            "stopLossType": "fixed",
        }
        headers = self._credentials.headers()
        persisted = {
            "proposal_id": str(proposal.proposal_id),
            "authorization_id": str(trade.authorization.authorization_id),
            "request_id": headers["x-request-id"],
            "instrument_id": proposal.instrument_id,
        }
        if not self._registry.reserve_demo_submission(proposal.idempotency_key, persisted):
            raise DemoExecutionError("duplicate or restarted Demo submission is blocked")

        # Final kill-switch/capability check immediately before HTTP.
        self._assert_kill_switch_clear()
        self._gate.assert_admitted(trade, at=self._clock())
        try:
            response = self._http.post_once(DEMO_ORDER_URL, headers, payload)
        except UnknownWriteOutcome:
            self._kill_switch.activate("ambiguous Demo submission outcome")
            self._registry.update_demo_submission(
                proposal.idempotency_key, ExecutionState.UNKNOWN.value, persisted
            )
            return self._submission(trade, headers["x-request-id"], ExecutionState.UNKNOWN)
        if not 200 <= response.status < 300:
            self._registry.update_demo_submission(
                proposal.idempotency_key,
                ExecutionState.REJECTED.value,
                {**persisted, "http_status": response.status},
            )
            return self._submission(trade, headers["x-request-id"], ExecutionState.REJECTED)
        try:
            raw = response.json()
            data = raw if isinstance(raw, dict) else {}
            order_id = str(data["orderId"])
        except (KeyError, ValueError, UnicodeDecodeError):
            self._kill_switch.activate("ambiguous Demo response payload")
            self._registry.update_demo_submission(
                proposal.idempotency_key, ExecutionState.UNKNOWN.value, persisted
            )
            return self._submission(trade, headers["x-request-id"], ExecutionState.UNKNOWN)
        submission = self._submission(
            trade,
            headers["x-request-id"],
            ExecutionState.SUBMITTED,
            order_id=order_id,
            reference_id=str(data["referenceId"]) if "referenceId" in data else None,
        )
        self._registry.update_demo_submission(
            proposal.idempotency_key,
            ExecutionState.SUBMITTED.value,
            {**persisted, "broker_order_id": order_id},
        )
        return submission

    def _assert_kill_switch_clear(self) -> None:
        if self._kill_switch.state.active:
            raise DemoExecutionError("global kill switch blocks Demo execution")

    def _submission(
        self,
        trade: AuthorizedTrade,
        request_id: str,
        state: ExecutionState,
        *,
        order_id: str | None = None,
        reference_id: str | None = None,
    ) -> BrokerSubmission:
        return BrokerSubmission(
            idempotency_key=trade.proposal.idempotency_key,
            request_id=request_id,
            state=state,
            broker_order_id=order_id,
            broker_reference_id=reference_id,
            proposal_id=str(trade.proposal.proposal_id),
            authorization_id=str(trade.authorization.authorization_id),
            submitted_at=self._clock(),
        )
