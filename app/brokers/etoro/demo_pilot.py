"""Automatic eToro Demo pilot guards for accepted A4C cycles."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from pydantic import Field

from app.brokers.etoro.auth import EtoroCredentials
from app.brokers.etoro.demo import (
    DEMO_ORDER_URL,
    DemoExecutionError,
    EtoroDemoAdapter,
    assert_demo_route,
)
from app.brokers.etoro.http import DisciplinedHttpClient
from app.brokers.models import BrokerSubmission, ExecutionState, PreflightDecision
from app.brokers.reconciliation import ReconciliationError, reconcile_demo_state
from app.domain.base import FrozenDomainModel
from app.domain.enums import AssetClass, Currency, OperatingMode, RiskDecisionStatus
from app.domain.proposals import TradeProposal
from app.domain.risk import RiskContext
from app.execution.gate import AuthorizationAlreadyConsumedError, RiskEnforcedExecutionGate
from app.orchestration.active_intelligence import ActiveIntelligenceCycleRecord
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskAuthorizationError, RiskManager
from app.scanner.active import ActiveScannerBucket, ActiveScannerCandidate, ActiveScannerResult
from app.storage.sqlite import SqliteRecordStore


class EtoroDemoPilotStatus(StrEnum):
    NO_CYCLE = "NO_CYCLE"
    NO_TOP_OPPORTUNITY = "NO_TOP_OPPORTUNITY"
    BLOCKED = "BLOCKED"
    SUBMITTED = "SUBMITTED"


class EtoroDemoPilotBlocker(StrEnum):
    REAL_ENVIRONMENT_REJECTED = "REAL_ENVIRONMENT_REJECTED"
    MISSING_PILOT_NOTIONAL = "MISSING_PILOT_NOTIONAL"
    INVALID_PILOT_NOTIONAL = "INVALID_PILOT_NOTIONAL"
    NON_TOP_OPPORTUNITY = "NON_TOP_OPPORTUNITY"
    AMBIGUOUS_INSTRUMENT_MAPPING = "AMBIGUOUS_INSTRUMENT_MAPPING"
    DUPLICATE_CYCLE_CANDIDATE = "DUPLICATE_CYCLE_CANDIDATE"
    UNRESOLVED_INSTRUMENT_SUBMISSION = "UNRESOLVED_INSTRUMENT_SUBMISSION"
    SUBMISSION_GATEWAY_UNAVAILABLE = "SUBMISSION_GATEWAY_UNAVAILABLE"
    VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE = "VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE"
    RISK_MANAGER_REJECTED = "RISK_MANAGER_REJECTED"
    EXECUTION_ADMISSION_GATE_REJECTED = "EXECUTION_ADMISSION_GATE_REJECTED"
    DEMO_PREFLIGHT_REJECTED = "DEMO_PREFLIGHT_REJECTED"
    DEMO_SUBMISSION_FAILED = "DEMO_SUBMISSION_FAILED"


class EtoroDemoPilotSettings(FrozenDomainModel):
    enabled: bool
    notional_eur: Decimal | None = Field(default=None, gt=0)


class EtoroDemoOrderIntent(FrozenDomainModel):
    cycle_id: str = Field(min_length=16)
    symbol: str = Field(min_length=1)
    asset_class: str = Field(min_length=1)
    broker_instrument_id: int = Field(gt=0)
    action: str = "OPEN"
    notional_eur: Decimal = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=128)
    endpoint: str = DEMO_ORDER_URL
    environment: OperatingMode = OperatingMode.ETORO_DEMO


class EtoroDemoSubmissionOutcome(FrozenDomainModel):
    submitted: bool
    symbol: str | None = None
    asset_class: str | None = None
    broker_order_id: str | None = Field(default=None, min_length=1)
    broker_reference_id: str | None = Field(default=None, min_length=1)
    sanitized_status: str = Field(min_length=1)
    risk_manager_reached: bool = False
    execution_admission_gate_reached: bool = False
    reconciliation_status: str | None = Field(default=None, min_length=1)
    risk_violation_codes: tuple[str, ...] = ()
    preflight_reasons: tuple[str, ...] = ()
    demo_broker_write_calls: int = Field(default=0, ge=0)
    broker_write_calls_real: int = Field(default=0, ge=0, le=0)


class EtoroDemoPilotResult(FrozenDomainModel):
    status: EtoroDemoPilotStatus
    blockers: tuple[EtoroDemoPilotBlocker, ...] = ()
    cycle_id: str | None = Field(default=None, min_length=16)
    eligible_intents: tuple[EtoroDemoOrderIntent, ...] = ()
    submissions: tuple[EtoroDemoSubmissionOutcome, ...] = ()
    demo_broker_write_calls: int = Field(default=0, ge=0)
    broker_write_calls_real: int = Field(default=0, ge=0, le=0)
    write_request_sent_to_real: bool = False


class EtoroDemoSubmissionPackage(FrozenDomainModel):
    proposal: TradeProposal
    risk_context: RiskContext
    preflight: PreflightDecision
    exploratory: bool = False


class DemoSubmissionGateway(Protocol):
    def submit_demo_order(
        self,
        intent: EtoroDemoOrderIntent,
        *,
        package: EtoroDemoSubmissionPackage | None = None,
    ) -> EtoroDemoSubmissionOutcome: ...


class DemoSubmissionReadback(Protocol):
    def demo_order_state(self, instrument_id: int, order_id: str) -> ExecutionState: ...


def _diversified_candidate_order(
    candidates: tuple[ActiveScannerCandidate, ...],
) -> tuple[ActiveScannerCandidate, ...]:
    """Keep execution lanes from starving non-crypto asset classes.

    Ranking remains the score inside each class, but the order lane takes one
    candidate per supported class before taking a second candidate. This makes
    the Demo pilot evaluate equities and ETFs when they are present and
    package-ready, instead of silently spending every cycle on crypto.
    """
    if len(candidates) < 2:
        return candidates
    by_class: dict[str, list[ActiveScannerCandidate]] = {}
    for candidate in sorted(
        candidates,
        key=lambda item: (item.opportunity_score, item.confidence),
        reverse=True,
    ):
        by_class.setdefault(candidate.asset_class.value, []).append(candidate)
    class_order = [asset_class.value for asset_class in (
        AssetClass.EQUITY,
        AssetClass.ETF,
        AssetClass.CRYPTO,
    ) if asset_class.value in by_class]
    class_order.extend(sorted(key for key in by_class if key not in class_order))
    ordered: list[ActiveScannerCandidate] = []
    index = 0
    while class_order:
        next_order: list[str] = []
        for asset_class in class_order:
            bucket = by_class[asset_class]
            if index < len(bucket):
                ordered.append(bucket[index])
            if index + 1 < len(bucket):
                next_order.append(asset_class)
        class_order = next_order
        index += 1
    return tuple(ordered)


class RiskCheckedEtoroDemoSubmissionGateway:
    """Risk/admission checked gateway into the existing eToro Demo adapter."""

    def __init__(
        self,
        *,
        environment: OperatingMode,
        credentials: EtoroCredentials,
        http: DisciplinedHttpClient,
        risk_manager: RiskManager,
        gate: RiskEnforcedExecutionGate,
        kill_switch: KillSwitch,
        registry: SqliteRecordStore,
        readback: DemoSubmissionReadback | None = None,
        tradability_revalidator: Callable[[int, str, datetime], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._environment = environment
        self._credentials = credentials
        self._http = http
        self._risk_manager = risk_manager
        self._gate = gate
        self._kill_switch = kill_switch
        self._registry = registry
        self._readback = readback
        self._tradability_revalidator = tradability_revalidator
        self._clock = clock or (lambda: datetime.now(UTC))

    def submit_demo_order(
        self,
        intent: EtoroDemoOrderIntent,
        *,
        package: EtoroDemoSubmissionPackage | None = None,
    ) -> EtoroDemoSubmissionOutcome:
        if (
            self._environment is not OperatingMode.ETORO_DEMO
            or intent.environment is not OperatingMode.ETORO_DEMO
        ):
            return _submission_outcome("REAL_ENVIRONMENT_REJECTED")
        assert_demo_route(intent.endpoint)
        if package is None:
            return _submission_outcome("VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE")
        if not _package_matches_intent(package, intent):
            return _submission_outcome("VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE")
        if not package.preflight.allowed or package.preflight.minimum_trade_amount is None:
            return _submission_outcome(
                "DEMO_PREFLIGHT_REJECTED",
                preflight_reasons=tuple(package.preflight.reasons),
            )

        evaluation = self._risk_manager.evaluate_with_authorized_capital(
            package.proposal,
            package.risk_context,
            minimum_confidence_override=(
                DEMO_EXPLORATORY_MIN_CONFIDENCE if package.exploratory else None
            ),
        )
        if evaluation.decision.status is not RiskDecisionStatus.APPROVED:
            return _submission_outcome(
                "RISK_MANAGER_REJECTED",
                risk_manager_reached=True,
                risk_violation_codes=tuple(
                    violation.code.value for violation in evaluation.decision.violations
                ),
            )
        if evaluation.authorization is None:
            return _submission_outcome("RISK_MANAGER_REJECTED", risk_manager_reached=True)
        approved_proposal = evaluation.approved_proposal
        if approved_proposal is None or evaluation.approved_notional_eur is None:
            return _submission_outcome("RISK_MANAGER_REJECTED", risk_manager_reached=True)
        sized_package = package.model_copy(update={"proposal": approved_proposal})
        try:
            authorized = self._gate.admit(
                approved_proposal,
                evaluation.authorization,
                at=self._clock(),
            )
        except (AuthorizationAlreadyConsumedError, PermissionError, RiskAuthorizationError):
            return _submission_outcome(
                "EXECUTION_ADMISSION_GATE_REJECTED",
                risk_manager_reached=True,
                execution_admission_gate_reached=True,
            )
        adapter = EtoroDemoAdapter(
            credentials=self._credentials,
            http=self._http,
            gate=self._gate,
            kill_switch=self._kill_switch,
            registry=self._registry,
            enabled=True,
            explicit_opt_in=True,
            clock=self._clock,
            tradability_revalidator=self._tradability_revalidator,
        )
        try:
            submission = adapter.submit_demo(authorized, sized_package.preflight)
        except (DemoExecutionError, PermissionError, RiskAuthorizationError) as exc:
            return _submission_outcome(
                type(exc).__name__,
                risk_manager_reached=True,
                execution_admission_gate_reached=True,
            )
        return self._submission_outcome_from_broker_submission(
            submission,
            intent=intent,
            risk_manager_reached=True,
            execution_admission_gate_reached=True,
        )

    def _submission_outcome_from_broker_submission(
        self,
        submission: BrokerSubmission,
        *,
        intent: EtoroDemoOrderIntent,
        risk_manager_reached: bool,
        execution_admission_gate_reached: bool,
    ) -> EtoroDemoSubmissionOutcome:
        reconciliation_status: str | None = None
        if (
            self._readback is not None
            and submission.broker_order_id is not None
            and submission.state is not ExecutionState.UNKNOWN
        ):
            try:
                observed = self._readback.demo_order_state(
                    intent.broker_instrument_id,
                    submission.broker_order_id,
                )
                reconciliation_status = reconcile_demo_state(
                    submission,
                    observed,
                    self._kill_switch,
                    self._registry,
                ).value
            except (ReconciliationError, RuntimeError, ValueError, TypeError):
                reconciliation_status = "RECONCILIATION_FAILED"
        return EtoroDemoSubmissionOutcome(
            submitted=submission.state
            in {
                ExecutionState.SUBMITTED,
                ExecutionState.PENDING,
                ExecutionState.FILLED,
                ExecutionState.PARTIALLY_FILLED,
            },
            broker_order_id=submission.broker_order_id,
            broker_reference_id=submission.broker_reference_id,
            sanitized_status=submission.state.value,
            risk_manager_reached=risk_manager_reached,
            execution_admission_gate_reached=execution_admission_gate_reached,
            reconciliation_status=reconciliation_status,
            demo_broker_write_calls=1,
            broker_write_calls_real=0,
        )


def demo_pilot_settings(
    values: Mapping[str, str], *, currency: Currency = Currency.EUR
) -> EtoroDemoPilotSettings:
    raw = values.get("AEGIS_ETORO_DEMO_PILOT_NOTIONAL")
    if raw is None:
        raw = values.get(f"AEGIS_ETORO_DEMO_PILOT_NOTIONAL_{currency.value}")
    if raw is None and currency is Currency.EUR:
        raw = values.get("AEGIS_ETORO_DEMO_PILOT_NOTIONAL_EUR")
    if raw is None or not raw.strip():
        return EtoroDemoPilotSettings(enabled=False, notional_eur=None)
    try:
        notional = Decimal(raw.strip())
    except InvalidOperation:
        return EtoroDemoPilotSettings(enabled=False, notional_eur=None)
    if notional <= 0:
        return EtoroDemoPilotSettings(enabled=False, notional_eur=None)
    return EtoroDemoPilotSettings(enabled=True, notional_eur=notional)


class EtoroAutomaticDemoPilot:
    """Routes only accepted TOP_OPPORTUNITY cycle candidates to Demo submission."""

    def __init__(
        self,
        *,
        environment: OperatingMode,
        settings: EtoroDemoPilotSettings,
        registry: SqliteRecordStore,
        gateway: DemoSubmissionGateway | None = None,
    ) -> None:
        self._environment = environment
        self._settings = settings
        self._registry = registry
        self._gateway = gateway

    def run(
        self,
        *,
        cycle: ActiveIntelligenceCycleRecord | None,
        scanner_result: ActiveScannerResult | None,
        broker_instrument_ids: Mapping[str, int],
        submission_packages: Mapping[str, EtoroDemoSubmissionPackage] | None = None,
        allow_exploratory_watchlist: bool = False,
    ) -> EtoroDemoPilotResult:
        if cycle is None or scanner_result is None:
            return EtoroDemoPilotResult(status=EtoroDemoPilotStatus.NO_CYCLE)
        if self._environment is not OperatingMode.ETORO_DEMO:
            return _blocked(cycle, EtoroDemoPilotBlocker.REAL_ENVIRONMENT_REJECTED)
        top = tuple(scanner_result.top_opportunities)
        exploratory_watchlist = not top and allow_exploratory_watchlist
        if exploratory_watchlist:
            # Demo-only smoke lane: let one coherent WATCHLIST candidate reach
            # the normal RiskManager/preflight gates.  This fixes the historic
            # zero-order dead end without relabelling the candidate as TOP.
            top = _diversified_candidate_order(tuple(scanner_result.watchlist))
        else:
            top = _diversified_candidate_order(top)
        if not top:
            return EtoroDemoPilotResult(
                status=EtoroDemoPilotStatus.NO_TOP_OPPORTUNITY,
                cycle_id=cycle.cycle_id,
            )
        if not self._settings.enabled or self._settings.notional_eur is None:
            return _blocked(cycle, EtoroDemoPilotBlocker.MISSING_PILOT_NOTIONAL)
        if submission_packages is not None:
            # Package construction performs live broker reads and can fail for
            # one symbol while succeeding for another.  Do not let the first
            # ranked, un-packaged candidate hide a package-ready fallback.
            package_ready = tuple(
                candidate for candidate in top if candidate.symbol in submission_packages
            )
            if not package_ready and not exploratory_watchlist:
                package_ready = tuple(
                    candidate
                    for candidate in _diversified_candidate_order(tuple(scanner_result.watchlist))
                    if candidate.symbol in submission_packages
                )
            if package_ready:
                top = package_ready
            else:
                # Preserve actionable validation failures (for example an
                # ambiguous eToro mapping) instead of masking them with the
                # later missing-package summary. Only report unavailable
                # execution material when at least one candidate is otherwise
                # a valid Demo intent.
                valid_intent_exists = False
                specific_blockers: list[EtoroDemoPilotBlocker] = []
                for candidate in top:
                    diagnostic = self._intent_for(
                        cycle=cycle,
                        candidate=candidate,
                        broker_instrument_ids=broker_instrument_ids,
                        package=None,
                        allow_exploratory_watchlist=allow_exploratory_watchlist,
                    )
                    if isinstance(diagnostic, EtoroDemoPilotBlocker):
                        specific_blockers.append(diagnostic)
                    else:
                        valid_intent_exists = True
                if specific_blockers and not valid_intent_exists:
                    return _blocked(cycle, specific_blockers[0])
                # Never create a nominal intent from the configured exposure
                # limit when live quote, eligibility, cash sizing, or preflight
                # material is missing. That produced misleading 98k intents
                # which could only fail later at the gateway.
                return _blocked(
                    cycle,
                    EtoroDemoPilotBlocker.VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE,
                )

        intents: list[EtoroDemoOrderIntent] = []
        blockers: list[EtoroDemoPilotBlocker] = []
        unresolved_instrument_ids = _unresolved_instrument_ids(self._registry)
        for candidate in top:
            intent = self._intent_for(
                cycle=cycle,
                candidate=candidate,
                broker_instrument_ids=broker_instrument_ids,
                package=(
                    None
                    if submission_packages is None
                    else submission_packages.get(candidate.symbol)
                ),
                allow_exploratory_watchlist=allow_exploratory_watchlist,
            )
            if isinstance(intent, EtoroDemoPilotBlocker):
                blockers.append(intent)
                continue
            if self._registry.demo_submission(intent.idempotency_key) is not None:
                blockers.append(EtoroDemoPilotBlocker.DUPLICATE_CYCLE_CANDIDATE)
                continue
            if intent.broker_instrument_id in unresolved_instrument_ids:
                blockers.append(EtoroDemoPilotBlocker.UNRESOLVED_INSTRUMENT_SUBMISSION)
                continue
            intents.append(intent)
        if blockers and not (exploratory_watchlist and intents):
            return EtoroDemoPilotResult(
                status=EtoroDemoPilotStatus.BLOCKED,
                blockers=tuple(blockers),
                cycle_id=cycle.cycle_id,
                eligible_intents=tuple(intents),
            )
        if self._gateway is None:
            return EtoroDemoPilotResult(
                status=EtoroDemoPilotStatus.BLOCKED,
                blockers=(EtoroDemoPilotBlocker.SUBMISSION_GATEWAY_UNAVAILABLE,),
                cycle_id=cycle.cycle_id,
                eligible_intents=tuple(intents),
            )

        submissions: list[EtoroDemoSubmissionOutcome] = []
        for intent in intents:
            package = (
                None if submission_packages is None else submission_packages.get(intent.symbol)
            )
            outcome = self._gateway.submit_demo_order(intent, package=package).model_copy(
                update={"symbol": intent.symbol, "asset_class": intent.asset_class}
            )
            submissions.append(outcome)
            if not outcome.submitted:
                blocker = _submission_blocker(outcome.sanitized_status)
                if exploratory_watchlist:
                    # A Demo exploratory lane may probe more than one
                    # package-ready candidate, but it must stop immediately
                    # after the first accepted order. Rejections are gate
                    # results, not broker writes, so trying the next candidate
                    # does not weaken RiskManager or the one-order cap.
                    blockers.append(blocker)
                    continue
                return EtoroDemoPilotResult(
                    status=EtoroDemoPilotStatus.BLOCKED,
                    blockers=(blocker,),
                    cycle_id=cycle.cycle_id,
                    eligible_intents=tuple(intents),
                    submissions=tuple(submissions),
                    demo_broker_write_calls=sum(s.demo_broker_write_calls for s in submissions),
                )
            if exploratory_watchlist:
                # A Demo cycle is capped at one successful order. Once a
                # candidate passes, do not probe or submit any further
                # candidates from the same cycle.
                return EtoroDemoPilotResult(
                    status=EtoroDemoPilotStatus.SUBMITTED,
                    cycle_id=cycle.cycle_id,
                    eligible_intents=tuple(intents),
                    submissions=tuple(submissions),
                    demo_broker_write_calls=sum(
                        s.demo_broker_write_calls for s in submissions
                    ),
                )
            # The configured Demo envelope is a single smoke-test budget for
            # this cycle. Stop after the first successful submission even if
            # the scanner produced several TOP candidates; a later cycle can
            # evaluate the remaining candidates against the updated account.
            return EtoroDemoPilotResult(
                status=EtoroDemoPilotStatus.SUBMITTED,
                cycle_id=cycle.cycle_id,
                eligible_intents=tuple(intents),
                submissions=tuple(submissions),
                demo_broker_write_calls=sum(
                    s.demo_broker_write_calls for s in submissions
                ),
            )
        if exploratory_watchlist and submissions:
            return EtoroDemoPilotResult(
                status=EtoroDemoPilotStatus.BLOCKED,
                blockers=tuple(dict.fromkeys(blockers)) or (
                    EtoroDemoPilotBlocker.DEMO_SUBMISSION_FAILED,
                ),
                cycle_id=cycle.cycle_id,
                eligible_intents=tuple(intents),
                submissions=tuple(submissions),
                demo_broker_write_calls=sum(s.demo_broker_write_calls for s in submissions),
            )
        return EtoroDemoPilotResult(
            status=EtoroDemoPilotStatus.SUBMITTED,
            cycle_id=cycle.cycle_id,
            eligible_intents=tuple(intents),
            submissions=tuple(submissions),
            demo_broker_write_calls=sum(s.demo_broker_write_calls for s in submissions),
        )

    def _intent_for(
        self,
        *,
        cycle: ActiveIntelligenceCycleRecord,
        candidate: ActiveScannerCandidate,
        broker_instrument_ids: Mapping[str, int],
        package: EtoroDemoSubmissionPackage | None,
        allow_exploratory_watchlist: bool = False,
    ) -> EtoroDemoOrderIntent | EtoroDemoPilotBlocker:
        if (
            candidate.bucket is not ActiveScannerBucket.TOP_OPPORTUNITIES
            and not (
                allow_exploratory_watchlist
                and candidate.bucket is ActiveScannerBucket.WATCHLIST
            )
        ):
            return EtoroDemoPilotBlocker.NON_TOP_OPPORTUNITY
        instrument_id = broker_instrument_ids.get(candidate.symbol)
        if instrument_id is None or instrument_id <= 0:
            return EtoroDemoPilotBlocker.AMBIGUOUS_INSTRUMENT_MAPPING
        assert_demo_route(DEMO_ORDER_URL)
        notional = package.proposal.amount if package is not None else self._settings.notional_eur
        if notional is None or notional <= 0:
            return EtoroDemoPilotBlocker.MISSING_PILOT_NOTIONAL
        return EtoroDemoOrderIntent(
            cycle_id=cycle.cycle_id,
            symbol=candidate.symbol,
            asset_class=candidate.asset_class.value,
            broker_instrument_id=instrument_id,
            notional_eur=notional,
            idempotency_key=f"etoro-demo-pilot:{cycle.cycle_id}:{candidate.symbol}:OPEN",
        )


def _blocked(
    cycle: ActiveIntelligenceCycleRecord,
    blocker: EtoroDemoPilotBlocker,
) -> EtoroDemoPilotResult:
    return EtoroDemoPilotResult(
        status=EtoroDemoPilotStatus.BLOCKED,
        blockers=(blocker,),
        cycle_id=cycle.cycle_id,
    )


def _submission_outcome(
    status: str,
    *,
    risk_manager_reached: bool = False,
    execution_admission_gate_reached: bool = False,
    risk_violation_codes: tuple[str, ...] = (),
    preflight_reasons: tuple[str, ...] = (),
    demo_broker_write_calls: int = 0,
) -> EtoroDemoSubmissionOutcome:
    return EtoroDemoSubmissionOutcome(
        submitted=False,
        sanitized_status=status,
        risk_manager_reached=risk_manager_reached,
        execution_admission_gate_reached=execution_admission_gate_reached,
        risk_violation_codes=risk_violation_codes,
        preflight_reasons=preflight_reasons,
        demo_broker_write_calls=demo_broker_write_calls,
        broker_write_calls_real=0,
    )


# This is deliberately limited to the explicit eToro Demo exploratory lane.
# It is not a production or real-money confidence threshold: all other Risk
# Manager, preflight, market-data, news, exposure, and execution gates remain
# unchanged, and the runtime still allows at most one successful Demo order.
DEMO_EXPLORATORY_MIN_CONFIDENCE = Decimal("0.50")


def _submission_blocker(status: str) -> EtoroDemoPilotBlocker:
    mapping = {
        "REAL_ENVIRONMENT_REJECTED": EtoroDemoPilotBlocker.REAL_ENVIRONMENT_REJECTED,
        "VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE": (
            EtoroDemoPilotBlocker.VERIFIED_EXECUTION_MATERIAL_UNAVAILABLE
        ),
        "DEMO_PREFLIGHT_REJECTED": EtoroDemoPilotBlocker.DEMO_PREFLIGHT_REJECTED,
        "RISK_MANAGER_REJECTED": EtoroDemoPilotBlocker.RISK_MANAGER_REJECTED,
        "EXECUTION_ADMISSION_GATE_REJECTED": (
            EtoroDemoPilotBlocker.EXECUTION_ADMISSION_GATE_REJECTED
        ),
    }
    return mapping.get(status, EtoroDemoPilotBlocker.DEMO_SUBMISSION_FAILED)


def _unresolved_instrument_ids(registry: SqliteRecordStore) -> frozenset[int]:
    """Prevent a new entry while an earlier entry for the same instrument is unresolved."""
    instrument_ids: set[int] = set()
    for record in registry.unresolved_demo_submissions():
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        try:
            instrument_id = int(payload["instrument_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if instrument_id > 0:
            instrument_ids.add(instrument_id)
    return frozenset(instrument_ids)


def _package_matches_intent(
    package: EtoroDemoSubmissionPackage,
    intent: EtoroDemoOrderIntent,
) -> bool:
    proposal = package.proposal
    return (
        proposal.idempotency_key == intent.idempotency_key
        and proposal.symbol.casefold() == intent.symbol.casefold()
        and proposal.instrument_id == intent.broker_instrument_id
        and proposal.amount == intent.notional_eur
    )
