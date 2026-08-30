"""Prospective shadow validation manifests and sanitized persistence."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from app.agent.exit_policy import (
    EXIT_POLICY_V2_GUARDED,
    EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE,
    ExitPolicyV2CandidateRegistry,
    ExitPolicyV2ExperimentManifest,
    ExitPolicyV2ParameterBundle,
)
from app.config.models import ApplicationConfig
from app.domain.base import FrozenDomainModel, require_aware
from app.risk.kill_switch import KillSwitch
from app.risk.manager import RiskManager
from app.storage.sqlite import SqliteRecordStore
from app.validation.models import TransactionCostAssumptions

DEFAULT_PROSPECTIVE_SHADOW_STORE_PATH = Path("work") / "prospective-shadow-validation.sqlite3"
EUR200_RESEARCH_BASELINE_VERSION = "AEGIS_EUR200_RESEARCH_BASELINE_V1"
PROSPECTIVE_SHADOW_MANIFEST_VERSION = "AEGIS_PROSPECTIVE_SHADOW_VALIDATION_V1"


class Eur200ResearchBaseline(FrozenDomainModel):
    version: str = Field(default=EUR200_RESEARCH_BASELINE_VERSION, min_length=1)
    initial_equity: Decimal = Field(gt=0)
    final_research_equity: Decimal = Field(gt=0)
    classification: str = Field(min_length=1)
    accounting_status: str = Field(min_length=1)
    frozen: bool
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def baseline_is_frozen_and_fingerprinted(self) -> "Eur200ResearchBaseline":
        if self.version != EUR200_RESEARCH_BASELINE_VERSION:
            raise ValueError("unsupported EUR200 research baseline version")
        if not self.frozen:
            raise ValueError("EUR200 research baseline must be frozen")
        if self.classification != "RESEARCH_EXPOSED":
            raise ValueError("EUR200 baseline must remain RESEARCH_EXPOSED")
        if self.accounting_status != "RECONCILED":
            raise ValueError("EUR200 baseline accounting must be RECONCILED")
        if self.fingerprint != self.expected_fingerprint():
            raise ValueError("EUR200 research baseline fingerprint mismatch")
        return self

    @classmethod
    def create(cls) -> "Eur200ResearchBaseline":
        payload = {
            "version": EUR200_RESEARCH_BASELINE_VERSION,
            "initial_equity": "200",
            "final_research_equity": "224.5301270773",
            "classification": "RESEARCH_EXPOSED",
            "accounting_status": "RECONCILED",
            "frozen": True,
        }
        return cls.model_validate({**payload, "fingerprint": _stable_sha256(payload)})

    def expected_fingerprint(self) -> str:
        return _stable_sha256(
            {
                "version": self.version,
                "initial_equity": str(self.initial_equity),
                "final_research_equity": str(self.final_research_equity),
                "classification": self.classification,
                "accounting_status": self.accounting_status,
                "frozen": self.frozen,
            }
        )


class ProspectiveShadowValidationManifest(FrozenDomainModel):
    version: str = Field(default=PROSPECTIVE_SHADOW_MANIFEST_VERSION, min_length=1)
    activation_timestamp: datetime
    frozen: bool
    baseline_version: str = Field(min_length=1)
    baseline_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    starting_equity: Decimal = Field(gt=0)
    confidence_profile: str = Field(min_length=1)
    exit_policy_profile: str = Field(min_length=1)
    frozen_candidate_id: str = Field(min_length=1)
    frozen_candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_manifest_version: str = Field(min_length=1)
    experiment_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_provenance: tuple[str, ...] = Field(min_length=1)
    transaction_cost_assumptions: dict[str, object]
    causal_data_rule: str = Field(min_length=1)
    audit_pipeline: tuple[str, ...] = Field(min_length=1)
    safety_invariants: tuple[str, ...] = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("activation_timestamp")
    @classmethod
    def activation_timestamp_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "prospective shadow activation timestamp")

    @model_validator(mode="after")
    def manifest_is_frozen_and_fingerprinted(self) -> "ProspectiveShadowValidationManifest":
        if self.version != PROSPECTIVE_SHADOW_MANIFEST_VERSION:
            raise ValueError("unsupported prospective shadow manifest version")
        if not self.frozen:
            raise ValueError("prospective shadow manifest must be frozen")
        if self.starting_equity != Decimal("200"):
            raise ValueError("prospective shadow account must start at exactly 200")
        if self.confidence_profile != EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE:
            raise ValueError("prospective shadow confidence profile must be V2_B_GUARDED")
        if self.exit_policy_profile != EXIT_POLICY_V2_GUARDED:
            raise ValueError("prospective shadow exit policy must be EXITPOLICY_V2_GUARDED")
        if self.fingerprint != self.expected_fingerprint():
            raise ValueError("prospective shadow manifest fingerprint mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        activation_timestamp: datetime,
        baseline: Eur200ResearchBaseline,
        config: ApplicationConfig,
        candidate_bundle: ExitPolicyV2ParameterBundle,
        candidate_registry: ExitPolicyV2CandidateRegistry,
        experiment_manifest: ExitPolicyV2ExperimentManifest,
        risk_policy_digest: str,
    ) -> "ProspectiveShadowValidationManifest":
        cost_assumptions = TransactionCostAssumptions().model_dump(mode="json")
        payload = {
            "version": PROSPECTIVE_SHADOW_MANIFEST_VERSION,
            "activation_timestamp": require_aware(
                activation_timestamp, "prospective shadow activation timestamp"
            ).isoformat(),
            "frozen": True,
            "baseline_version": baseline.version,
            "baseline_fingerprint": baseline.fingerprint,
            "starting_equity": "200",
            "confidence_profile": config.strategy.confidence_profile,
            "exit_policy_profile": config.strategy.exit_policy_profile,
            "frozen_candidate_id": candidate_bundle.parameters.parameter_bundle_id,
            "frozen_candidate_fingerprint": candidate_bundle.fingerprint,
            "candidate_registry_fingerprint": candidate_registry.fingerprint,
            "experiment_manifest_version": experiment_manifest.experiment_version,
            "experiment_manifest_fingerprint": experiment_manifest.manifest_sha256,
            "risk_policy_digest": risk_policy_digest,
            "data_provenance": (
                "prospective data only after activation_timestamp",
                "provider-specific OHLCV provenance remains distinct",
                "no historical parameter tuning after activation",
            ),
            "transaction_cost_assumptions": cost_assumptions,
            "causal_data_rule": (
                "consume only observations with timestamp >= activation_timestamp "
                "and available at decision time"
            ),
            "audit_pipeline": (
                "market_observation",
                "intelligence",
                "decision",
                "trade_proposal",
                "risk_manager",
                "open_or_increase",
                "position_management",
                "reduce_or_close",
                "realized_unrealized_pnl",
                "equity",
            ),
            "safety_invariants": (
                "prospective account starts independently with no research positions",
                "broker_write_calls must remain 0",
                "Demo execution remains disabled",
                "Real execution remains unavailable",
                "RiskManager and RiskPolicy remain unchanged",
            ),
        }
        return cls.model_validate({**payload, "fingerprint": _stable_sha256(payload)})

    def expected_fingerprint(self) -> str:
        return _stable_sha256(
            {
                "version": self.version,
                "activation_timestamp": self.activation_timestamp.isoformat(),
                "frozen": self.frozen,
                "baseline_version": self.baseline_version,
                "baseline_fingerprint": self.baseline_fingerprint,
                "starting_equity": str(self.starting_equity),
                "confidence_profile": self.confidence_profile,
                "exit_policy_profile": self.exit_policy_profile,
                "frozen_candidate_id": self.frozen_candidate_id,
                "frozen_candidate_fingerprint": self.frozen_candidate_fingerprint,
                "candidate_registry_fingerprint": self.candidate_registry_fingerprint,
                "experiment_manifest_version": self.experiment_manifest_version,
                "experiment_manifest_fingerprint": self.experiment_manifest_fingerprint,
                "risk_policy_digest": self.risk_policy_digest,
                "data_provenance": tuple(self.data_provenance),
                "transaction_cost_assumptions": self.transaction_cost_assumptions,
                "causal_data_rule": self.causal_data_rule,
                "audit_pipeline": tuple(self.audit_pipeline),
                "safety_invariants": tuple(self.safety_invariants),
            }
        )


class ProspectiveShadowValidationStore:
    def __init__(self, store: SqliteRecordStore) -> None:
        self._store = store

    def record_baseline(self, baseline: Eur200ResearchBaseline) -> int:
        return self._store.append("prospective-shadow-baseline", baseline.model_dump(mode="json"))

    def record_manifest(self, manifest: ProspectiveShadowValidationManifest) -> int:
        return self._store.append("prospective-shadow-manifest", manifest.model_dump(mode="json"))

    def record_decision(self, payload: Mapping[str, object]) -> int:
        return self._store.append("prospective-shadow-decision", payload)

    def baselines(self) -> tuple[dict[str, object], ...]:
        return self._store.list("prospective-shadow-baseline")

    def manifests(self) -> tuple[dict[str, object], ...]:
        return self._store.list("prospective-shadow-manifest")

    def decisions(self) -> tuple[dict[str, object], ...]:
        return self._store.list("prospective-shadow-decision")


def default_prospective_shadow_store(
    path: Path | None = None,
) -> ProspectiveShadowValidationStore:
    return ProspectiveShadowValidationStore(
        SqliteRecordStore(path or DEFAULT_PROSPECTIVE_SHADOW_STORE_PATH)
    )


def prospective_risk_policy_digest(config: ApplicationConfig) -> str:
    manager = RiskManager(
        config.risk,
        KillSwitch(active=True, reason="prospective shadow preparation"),
        authorization_key=b"prospective-shadow-risk-key-32b!",
    )
    return manager.policy_digest


def _stable_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
