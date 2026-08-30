"""Versioned, broker-neutral exit decisions for already-held long positions."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from app.domain.base import FrozenDomainModel, require_aware
from app.domain.enums import AssetClass, RecommendedAction
from app.domain.portfolio import PortfolioSnapshot
from app.intelligence.models import (
    AegisDecision,
    AegisOpportunityAnalysis,
    RegimeLabel,
    StrategyDirection,
)

EXIT_POLICY_VERSION = "exit-policy-v1"
EXIT_POLICY_V1_LEGACY = "EXITPOLICY_V1_LEGACY"
EXIT_POLICY_V2_GUARDED = "EXITPOLICY_V2_GUARDED"
EXIT_POLICY_V2_VERSION = "exit-policy-v2-guarded-framework"
EXIT_POLICY_V2_PARAMETER_BUNDLE_VERSION = "EXITPOLICY_V2_GUARDED_PARAMS_V1"
EXIT_POLICY_V2_VALIDATION_PROTOCOL_VERSION = "exit-policy-v2-validation-protocol-v1"
EXIT_POLICY_V2_EXPERIMENT_VERSION = "EXITPOLICY_V2_PREREGISTERED_EXPERIMENT_V1"
EXIT_POLICY_V2_EXPERIMENT_V2_VERSION = "EXITPOLICY_V2_PREREGISTERED_EXPERIMENT_V2"
EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE = "V2_B_GUARDED"


class ExitPolicyV2ValidationStatus(StrEnum):
    DRAFT = "DRAFT"
    FROZEN_FOR_VALIDATION = "FROZEN_FOR_VALIDATION"
    RESEARCH_EXPOSED = "RESEARCH_EXPOSED"
    VALIDATED = "VALIDATED"


class ExitPolicyV2ParameterScope(StrEnum):
    GLOBAL = "GLOBAL"
    ASSET_CLASS_SPECIFIC = "ASSET_CLASS_SPECIFIC"


class ExitPolicyV2ParameterDefinition(FrozenDomainModel):
    name: str = Field(min_length=1)
    value_type: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    semantic_meaning: str = Field(min_length=1)
    valid_domain: str = Field(min_length=1)
    equivalent_existing_value: str | None = Field(default=None, min_length=1)
    asset_class_specific: bool
    mandatory: bool


class ExitPolicyV2ParameterCandidate(FrozenDomainModel):
    parameter_name: str = Field(min_length=1)
    candidate_values: tuple[Decimal | int | bool, ...] = Field(min_length=1)
    unit: str = Field(min_length=1)
    asset_class_scope: tuple[AssetClass, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    shared_across_asset_classes: bool
    constraints: tuple[str, ...] = ()


class ExitPolicyV2ValidationPartition(FrozenDomainModel):
    name: str = Field(min_length=1)
    start: datetime
    end: datetime
    role: str = Field(min_length=1)
    research_exposed: bool = False

    @field_validator("start", "end")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "exit policy validation partition timestamp")

    @model_validator(mode="after")
    def end_is_after_start(self) -> "ExitPolicyV2ValidationPartition":
        if self.end <= self.start:
            raise ValueError("validation partition end must be after start")
        return self


class ExitPolicyV2ValidationProtocol(FrozenDomainModel):
    version: str = Field(default=EXIT_POLICY_V2_VALIDATION_PROTOCOL_VERSION, min_length=1)
    candidate_parameter_grid_declared_before_replay: bool
    train_selection_rule: str = Field(min_length=1)
    validation_selection_rule: str = Field(min_length=1)
    holdout_rule: str = Field(min_length=1)
    selection_constraints: tuple[str, ...] = Field(min_length=1)
    research_exposed_observations: tuple[ExitPolicyV2ValidationPartition, ...] = ()
    proposed_partitions: tuple[ExitPolicyV2ValidationPartition, ...] = Field(min_length=1)


class ExitPolicyV2CandidateRegistry(FrozenDomainModel):
    registry_version: str = Field(default="exit-policy-v2-candidate-registry-v1", min_length=1)
    frozen: bool
    created_at: datetime
    max_candidate_bundles: int = Field(gt=0, le=12)
    parameter_candidates: tuple[ExitPolicyV2ParameterCandidate, ...] = Field(min_length=1)
    candidate_bundles: tuple["ExitPolicyV2ParameterBundle", ...] = Field(min_length=1)
    selection_objective: tuple[str, ...] = Field(min_length=1)
    rejection_gates: tuple[str, ...] = Field(min_length=1)
    tie_breaking_rules: tuple[str, ...] = Field(min_length=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "exit policy candidate registry created_at")

    @model_validator(mode="after")
    def registry_is_frozen_and_fingerprinted(self) -> "ExitPolicyV2CandidateRegistry":
        if not self.frozen:
            raise ValueError("EXITPOLICY_V2 candidate registry must be frozen")
        if len(self.candidate_bundles) > self.max_candidate_bundles:
            raise ValueError("candidate registry exceeds maximum candidate bundle count")
        fingerprints = tuple(bundle.fingerprint for bundle in self.candidate_bundles)
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("candidate registry contains duplicate bundle fingerprints")
        if self.fingerprint != self.expected_fingerprint():
            raise ValueError("EXITPOLICY_V2 candidate registry fingerprint mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        created_at: datetime,
        max_candidate_bundles: int,
        parameter_candidates: tuple[ExitPolicyV2ParameterCandidate, ...],
        candidate_bundles: tuple["ExitPolicyV2ParameterBundle", ...],
        selection_objective: tuple[str, ...],
        rejection_gates: tuple[str, ...],
        tie_breaking_rules: tuple[str, ...],
    ) -> "ExitPolicyV2CandidateRegistry":
        payload = {
            "registry_version": "exit-policy-v2-candidate-registry-v1",
            "frozen": True,
            "created_at": require_aware(
                created_at, "exit policy candidate registry created_at"
            ).isoformat(),
            "max_candidate_bundles": max_candidate_bundles,
            "parameter_candidates": tuple(
                candidate.model_dump(mode="json") for candidate in parameter_candidates
            ),
            "candidate_bundle_fingerprints": tuple(
                bundle.fingerprint for bundle in candidate_bundles
            ),
            "selection_objective": tuple(selection_objective),
            "rejection_gates": tuple(rejection_gates),
            "tie_breaking_rules": tuple(tie_breaking_rules),
        }
        return cls.model_validate(
            {
                "registry_version": payload["registry_version"],
                "frozen": payload["frozen"],
                "created_at": payload["created_at"],
                "max_candidate_bundles": payload["max_candidate_bundles"],
                "selection_objective": payload["selection_objective"],
                "rejection_gates": payload["rejection_gates"],
                "tie_breaking_rules": payload["tie_breaking_rules"],
                "parameter_candidates": parameter_candidates,
                "candidate_bundles": candidate_bundles,
                "fingerprint": _stable_sha256(payload),
            }
        )

    def expected_fingerprint(self) -> str:
        return _stable_sha256(
            {
                "registry_version": self.registry_version,
                "frozen": self.frozen,
                "created_at": self.created_at.isoformat(),
                "max_candidate_bundles": self.max_candidate_bundles,
                "parameter_candidates": tuple(
                    candidate.model_dump(mode="json") for candidate in self.parameter_candidates
                ),
                "candidate_bundle_fingerprints": tuple(
                    bundle.fingerprint for bundle in self.candidate_bundles
                ),
                "selection_objective": tuple(self.selection_objective),
                "rejection_gates": tuple(self.rejection_gates),
                "tie_breaking_rules": tuple(self.tie_breaking_rules),
            }
        )


class ExitPolicyV2DatasetPartition(FrozenDomainModel):
    asset_class: AssetClass
    role: str = Field(min_length=1)
    start: datetime
    end: datetime
    eligible_symbols: tuple[str, ...]
    insufficient_history_symbols: tuple[str, ...] = ()
    research_exposed: bool = False

    @field_validator("start", "end")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "exit policy dataset partition timestamp")

    @model_validator(mode="after")
    def partition_is_well_formed(self) -> "ExitPolicyV2DatasetPartition":
        if self.end <= self.start:
            raise ValueError("dataset partition end must be after start")
        return self


class ExitPolicyV2ExperimentManifest(FrozenDomainModel):
    experiment_version: str = Field(default=EXIT_POLICY_V2_EXPERIMENT_VERSION, min_length=1)
    frozen: bool
    created_at: datetime
    confidence_profile: str | None = Field(default=None, min_length=1)
    exit_policy_profile: str | None = Field(default=None, min_length=1)
    dataset_partitions: tuple[ExitPolicyV2DatasetPartition, ...] = Field(min_length=1)
    candidate_registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_bundle_fingerprints: tuple[str, ...] = Field(min_length=1)
    objective_rules: tuple[str, ...] = Field(min_length=1)
    rejection_rules: tuple[str, ...] = Field(min_length=1)
    overfitting_controls: tuple[str, ...] = Field(min_length=1)
    safety_invariants: tuple[str, ...] = ()
    research_exposed_ranges: tuple[ExitPolicyV2DatasetPartition, ...] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "exit policy experiment manifest created_at")

    @model_validator(mode="after")
    def manifest_is_frozen_and_fingerprinted(self) -> "ExitPolicyV2ExperimentManifest":
        if self.experiment_version not in {
            EXIT_POLICY_V2_EXPERIMENT_VERSION,
            EXIT_POLICY_V2_EXPERIMENT_V2_VERSION,
        }:
            raise ValueError("unsupported EXITPOLICY_V2 experiment manifest version")
        if not self.frozen:
            raise ValueError("EXITPOLICY_V2 experiment manifest must be frozen")
        if self.experiment_version == EXIT_POLICY_V2_EXPERIMENT_V2_VERSION:
            if self.confidence_profile != EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE:
                raise ValueError("V2 experiment manifest must freeze V2_B_GUARDED confidence")
            if self.exit_policy_profile != EXIT_POLICY_V2_GUARDED:
                raise ValueError("V2 experiment manifest must freeze EXITPOLICY_V2_GUARDED")
            if not self.safety_invariants:
                raise ValueError("V2 experiment manifest must include safety invariants")
        if _partitions_overlap(self.dataset_partitions):
            raise ValueError("dataset partitions must not overlap within each asset class")
        if _holdout_overlaps_research_exposed(
            self.dataset_partitions, self.research_exposed_ranges
        ):
            raise ValueError("pristine HOLDOUT cannot overlap RESEARCH_EXPOSED data")
        if self.manifest_sha256 != self.expected_manifest_sha256():
            raise ValueError("EXITPOLICY_V2 experiment manifest fingerprint mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        created_at: datetime,
        dataset_partitions: tuple[ExitPolicyV2DatasetPartition, ...],
        candidate_registry: ExitPolicyV2CandidateRegistry,
        objective_rules: tuple[str, ...],
        rejection_rules: tuple[str, ...],
        overfitting_controls: tuple[str, ...],
        research_exposed_ranges: tuple[ExitPolicyV2DatasetPartition, ...],
        experiment_version: str = EXIT_POLICY_V2_EXPERIMENT_VERSION,
        confidence_profile: str | None = None,
        exit_policy_profile: str | None = None,
        safety_invariants: tuple[str, ...] = (),
    ) -> "ExitPolicyV2ExperimentManifest":
        payload = {
            "experiment_version": experiment_version,
            "frozen": True,
            "created_at": require_aware(
                created_at, "exit policy experiment manifest created_at"
            ).isoformat(),
            "confidence_profile": confidence_profile,
            "exit_policy_profile": exit_policy_profile,
            "dataset_partitions": tuple(
                partition.model_dump(mode="json") for partition in dataset_partitions
            ),
            "candidate_registry_fingerprint": candidate_registry.fingerprint,
            "candidate_bundle_fingerprints": tuple(
                bundle.fingerprint for bundle in candidate_registry.candidate_bundles
            ),
            "objective_rules": tuple(objective_rules),
            "rejection_rules": tuple(rejection_rules),
            "overfitting_controls": tuple(overfitting_controls),
            "safety_invariants": tuple(safety_invariants),
            "research_exposed_ranges": tuple(
                partition.model_dump(mode="json") for partition in research_exposed_ranges
            ),
        }
        return cls.model_validate(
            {
                **payload,
                "dataset_partitions": dataset_partitions,
                "research_exposed_ranges": research_exposed_ranges,
                "manifest_sha256": _stable_sha256(payload),
            }
        )

    def expected_manifest_sha256(self) -> str:
        return _stable_sha256(
            {
                "experiment_version": self.experiment_version,
                "frozen": self.frozen,
                "created_at": self.created_at.isoformat(),
                "confidence_profile": self.confidence_profile,
                "exit_policy_profile": self.exit_policy_profile,
                "dataset_partitions": tuple(
                    partition.model_dump(mode="json") for partition in self.dataset_partitions
                ),
                "candidate_registry_fingerprint": self.candidate_registry_fingerprint,
                "candidate_bundle_fingerprints": tuple(self.candidate_bundle_fingerprints),
                "objective_rules": tuple(self.objective_rules),
                "rejection_rules": tuple(self.rejection_rules),
                "overfitting_controls": tuple(self.overfitting_controls),
                "safety_invariants": tuple(self.safety_invariants),
                "research_exposed_ranges": tuple(
                    partition.model_dump(mode="json") for partition in self.research_exposed_ranges
                ),
            }
        )


class ExitPolicyV2ParameterBundle(FrozenDomainModel):
    version: str = Field(default=EXIT_POLICY_V2_PARAMETER_BUNDLE_VERSION, min_length=1)
    frozen: bool
    created_at: datetime
    creation_rationale: str = Field(min_length=1)
    parameter_provenance: tuple[str, ...] = Field(min_length=1)
    asset_class_scope: tuple[AssetClass, ...] = Field(min_length=1)
    validation_status: ExitPolicyV2ValidationStatus
    parameters: "ExitPolicyV2Parameters"
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "exit policy parameter bundle created_at")

    @model_validator(mode="after")
    def bundle_is_frozen_and_fingerprinted(self) -> "ExitPolicyV2ParameterBundle":
        if self.version != EXIT_POLICY_V2_PARAMETER_BUNDLE_VERSION:
            raise ValueError("unsupported EXITPOLICY_V2 parameter bundle version")
        if not self.frozen:
            raise ValueError("EXITPOLICY_V2 parameter bundle must be frozen")
        if self.fingerprint != self.expected_fingerprint():
            raise ValueError("EXITPOLICY_V2 parameter bundle fingerprint mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        created_at: datetime,
        creation_rationale: str,
        parameter_provenance: tuple[str, ...],
        asset_class_scope: tuple[AssetClass, ...],
        validation_status: ExitPolicyV2ValidationStatus,
        parameters: "ExitPolicyV2Parameters",
    ) -> "ExitPolicyV2ParameterBundle":
        payload = {
            "version": EXIT_POLICY_V2_PARAMETER_BUNDLE_VERSION,
            "frozen": True,
            "created_at": require_aware(
                created_at, "exit policy parameter bundle created_at"
            ).isoformat(),
            "creation_rationale": creation_rationale,
            "parameter_provenance": tuple(parameter_provenance),
            "asset_class_scope": tuple(item.value for item in asset_class_scope),
            "validation_status": validation_status.value,
            "parameters": parameters.model_dump(mode="json"),
        }
        return cls.model_validate(
            {
                **payload,
                "asset_class_scope": asset_class_scope,
                "validation_status": validation_status,
                "parameters": parameters,
                "fingerprint": _stable_sha256(payload),
            }
        )

    def expected_fingerprint(self) -> str:
        return _stable_sha256(
            {
                "version": self.version,
                "frozen": self.frozen,
                "created_at": self.created_at.isoformat(),
                "creation_rationale": self.creation_rationale,
                "parameter_provenance": tuple(self.parameter_provenance),
                "asset_class_scope": tuple(item.value for item in self.asset_class_scope),
                "validation_status": self.validation_status.value,
                "parameters": self.parameters.model_dump(mode="json"),
            }
        )


def exit_policy_v2_parameter_inventory() -> tuple[ExitPolicyV2ParameterDefinition, ...]:
    return (
        ExitPolicyV2ParameterDefinition(
            name="capital_reduce_drawdown_pct",
            value_type="Decimal",
            unit="fraction_of_cost_basis",
            semantic_meaning="unrealized loss that may reduce an existing long position",
            valid_domain="0 <= value <= 1 and value <= capital_close_drawdown_pct",
            equivalent_existing_value=(
                "RiskPolicy drawdown/concentration checks are related but not equivalent"
            ),
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="capital_close_drawdown_pct",
            value_type="Decimal",
            unit="fraction_of_cost_basis",
            semantic_meaning="unrealized loss that may close an existing long position",
            valid_domain="0 <= value <= 1 and value >= capital_reduce_drawdown_pct",
            equivalent_existing_value=(
                "RiskPolicy drawdown/concentration checks are related but not equivalent"
            ),
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="thesis_reduce_confidence_drop",
            value_type="Decimal",
            unit="confidence_points_0_to_1",
            semantic_meaning="confidence deterioration from entry that may reduce the position",
            valid_domain="0 <= value <= 1 and value <= thesis_close_confidence_drop",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="thesis_close_confidence_drop",
            value_type="Decimal",
            unit="confidence_points_0_to_1",
            semantic_meaning="confidence deterioration from entry that may close the position",
            valid_domain="0 <= value <= 1 and value >= thesis_reduce_confidence_drop",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="thesis_reduce_score_drop",
            value_type="Decimal",
            unit="opportunity_score_points_0_to_100",
            semantic_meaning=(
                "opportunity-score deterioration from entry that may reduce the position"
            ),
            valid_domain="0 <= value <= 100 and value <= thesis_close_score_drop",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="thesis_close_score_drop",
            value_type="Decimal",
            unit="opportunity_score_points_0_to_100",
            semantic_meaning=(
                "opportunity-score deterioration from entry that may close the position"
            ),
            valid_domain="0 <= value <= 100 and value >= thesis_reduce_score_drop",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="regime_deterioration_reduce_enabled",
            value_type="bool",
            unit="versioned_regime_transition_rule",
            semantic_meaning=(
                "enables reduction when the entry trend thesis deteriorates to transition"
            ),
            valid_domain="true or false; candidate value must be declared before replay",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="regime_invalidation_close_enabled",
            value_type="bool",
            unit="versioned_regime_transition_rule",
            semantic_meaning="enables close when a strong-uptrend entry thesis turns downtrend",
            valid_domain="true or false; candidate value must be declared before replay",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="trailing_min_mfe_pct",
            value_type="Decimal",
            unit="fraction_of_cost_basis",
            semantic_meaning=(
                "minimum favorable excursion required before trailing protection can fire"
            ),
            valid_domain="0 <= value <= 10",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="trailing_reduce_drawdown_pct",
            value_type="Decimal",
            unit="fraction_from_post_entry_peak",
            semantic_meaning="drawdown from post-entry peak that may reduce the position",
            valid_domain="0 <= value <= 1 and value <= trailing_close_drawdown_pct",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="trailing_close_drawdown_pct",
            value_type="Decimal",
            unit="fraction_from_post_entry_peak",
            semantic_meaning="drawdown from post-entry peak that may close the position",
            valid_domain="0 <= value <= 1 and value >= trailing_reduce_drawdown_pct",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="defensive_persistence_reduce_count",
            value_type="int",
            unit="consecutive_bars",
            semantic_meaning="defensive evidence persistence required before reducing",
            valid_domain="integer >= 1 and value <= defensive_persistence_close_count",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="defensive_persistence_close_count",
            value_type="int",
            unit="consecutive_bars",
            semantic_meaning="defensive evidence persistence required before closing",
            valid_domain="integer >= 1 and value >= defensive_persistence_reduce_count",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="concentration_reduce_weight",
            value_type="Decimal",
            unit="portfolio_weight",
            semantic_meaning="position weight that may trigger rebalance reduction",
            valid_domain="0 <= value <= 1",
            equivalent_existing_value="RiskPolicy.max_single_position",
            asset_class_specific=False,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="stagnation_bars",
            value_type="int",
            unit="bars",
            semantic_meaning="bars held before stagnation can be considered",
            valid_domain="integer >= 1",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="stagnation_abs_return_pct",
            value_type="Decimal",
            unit="absolute_fraction_of_cost_basis",
            semantic_meaning="maximum absolute return still considered stagnant",
            valid_domain="0 <= value <= 1",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="cooldown_bars_after_reduce",
            value_type="int",
            unit="bars",
            semantic_meaning="bars during which same-symbol ENTRY/INCREASE is blocked after REDUCE",
            valid_domain="integer >= 0",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
        ExitPolicyV2ParameterDefinition(
            name="cooldown_bars_after_close",
            value_type="int",
            unit="bars",
            semantic_meaning="bars during which same-symbol ENTRY/INCREASE is blocked after CLOSE",
            valid_domain="integer >= 0",
            equivalent_existing_value=None,
            asset_class_specific=True,
            mandatory=True,
        ),
    )


def default_exit_policy_v2_validation_protocol() -> ExitPolicyV2ValidationProtocol:
    return ExitPolicyV2ValidationProtocol(
        candidate_parameter_grid_declared_before_replay=True,
        train_selection_rule=(
            "select only from predeclared candidate grids using TRAIN evidence; "
            "reject configurations with inadequate lifecycle counts or unstable risk behavior"
        ),
        validation_selection_rule=(
            "choose at most one configuration on VALIDATION using lifecycle completion, "
            "drawdown, stability, and risk behavior before any pristine holdout review"
        ),
        holdout_rule=(
            "evaluate one frozen configuration on HOLDOUT only once; do not modify "
            "parameters from HOLDOUT outcomes"
        ),
        selection_constraints=(
            "profit alone is never sufficient",
            "completed lifecycle count must be sufficient",
            "drawdown and adverse excursion must be acceptable",
            "multiple symbols and regimes must be represented",
            "parameter stability must be non-fragile",
            "2024-2026 observations previously inspected are RESEARCH_EXPOSED",
        ),
        research_exposed_observations=(
            ExitPolicyV2ValidationPartition(
                name="core_1d_lifecycle_window_previously_inspected",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2026, 12, 31, tzinfo=UTC),
                role="RESEARCH_EXPOSED",
                research_exposed=True,
            ),
        ),
        proposed_partitions=(
            ExitPolicyV2ValidationPartition(
                name="pre_2021_train_candidate_selection",
                start=datetime(2016, 1, 1, tzinfo=UTC),
                end=datetime(2020, 12, 31, tzinfo=UTC),
                role="TRAIN",
            ),
            ExitPolicyV2ValidationPartition(
                name="pre_2024_validation_freeze_selection",
                start=datetime(2021, 1, 1, tzinfo=UTC),
                end=datetime(2023, 12, 31, tzinfo=UTC),
                role="VALIDATION",
            ),
        ),
    )


def default_exit_policy_v2_candidate_registry(
    *, created_at: datetime
) -> ExitPolicyV2CandidateRegistry:
    candidates = (
        _candidate(
            "capital_reduce_drawdown_pct",
            (Decimal("0.06"), Decimal("0.08"), Decimal("0.10")),
            "fraction_of_cost_basis",
            True,
            "capital protection search range declared before lifecycle replay",
        ),
        _candidate(
            "capital_close_drawdown_pct",
            (Decimal("0.12"), Decimal("0.16"), Decimal("0.20")),
            "fraction_of_cost_basis",
            True,
            "capital protection search range declared before lifecycle replay",
            constraints=("capital_close_drawdown_pct >= capital_reduce_drawdown_pct",),
        ),
        _candidate(
            "thesis_reduce_confidence_drop",
            (Decimal("0.10"), Decimal("0.15")),
            "confidence_points_0_to_1",
            True,
            "thesis deterioration search range declared before lifecycle replay",
        ),
        _candidate(
            "thesis_close_confidence_drop",
            (Decimal("0.25"), Decimal("0.35")),
            "confidence_points_0_to_1",
            True,
            "thesis invalidation search range declared before lifecycle replay",
            constraints=("thesis_close_confidence_drop >= thesis_reduce_confidence_drop",),
        ),
        _candidate(
            "thesis_reduce_score_drop",
            (Decimal("10"), Decimal("15")),
            "opportunity_score_points_0_to_100",
            True,
            "score deterioration search range declared before lifecycle replay",
        ),
        _candidate(
            "thesis_close_score_drop",
            (Decimal("25"), Decimal("35")),
            "opportunity_score_points_0_to_100",
            True,
            "score invalidation search range declared before lifecycle replay",
            constraints=("thesis_close_score_drop >= thesis_reduce_score_drop",),
        ),
        _candidate(
            "regime_deterioration_reduce_enabled",
            (True,),
            "versioned_regime_transition_rule",
            True,
            "existing framework branch is either admitted or rejected before replay",
        ),
        _candidate(
            "regime_invalidation_close_enabled",
            (True,),
            "versioned_regime_transition_rule",
            True,
            "existing framework branch is either admitted or rejected before replay",
        ),
        _candidate(
            "trailing_min_mfe_pct",
            (Decimal("0.08"), Decimal("0.12")),
            "fraction_of_cost_basis",
            True,
            "trailing protection search range declared before lifecycle replay",
        ),
        _candidate(
            "trailing_reduce_drawdown_pct",
            (Decimal("0.04"), Decimal("0.06")),
            "fraction_from_post_entry_peak",
            True,
            "trailing reduce search range declared before lifecycle replay",
            constraints=("trailing_reduce_drawdown_pct <= trailing_close_drawdown_pct",),
        ),
        _candidate(
            "trailing_close_drawdown_pct",
            (Decimal("0.10"), Decimal("0.14")),
            "fraction_from_post_entry_peak",
            True,
            "trailing close search range declared before lifecycle replay",
            constraints=("trailing_close_drawdown_pct >= trailing_reduce_drawdown_pct",),
        ),
        _candidate(
            "defensive_persistence_reduce_count",
            (2, 3),
            "consecutive_bars",
            True,
            "defensive persistence range declared before lifecycle replay",
        ),
        _candidate(
            "defensive_persistence_close_count",
            (4, 5),
            "consecutive_bars",
            True,
            "defensive persistence close range declared before lifecycle replay",
            constraints=(
                "defensive_persistence_close_count >= defensive_persistence_reduce_count",
            ),
        ),
        _candidate(
            "concentration_reduce_weight",
            (Decimal("0.20"), Decimal("0.25")),
            "portfolio_weight",
            False,
            "bounded around the existing RiskPolicy concentration ceiling before replay",
        ),
        _candidate(
            "stagnation_bars",
            (20, 40),
            "bars",
            True,
            "time-exposure range declared before lifecycle replay",
        ),
        _candidate(
            "stagnation_abs_return_pct",
            (Decimal("0.01"), Decimal("0.02")),
            "absolute_fraction_of_cost_basis",
            True,
            "stagnation band declared before lifecycle replay",
        ),
        _candidate(
            "cooldown_bars_after_reduce",
            (2, 4),
            "bars",
            True,
            "same-symbol reentry cooldown declared before lifecycle replay",
        ),
        _candidate(
            "cooldown_bars_after_close",
            (4, 8),
            "bars",
            True,
            "same-symbol reentry cooldown declared before lifecycle replay",
        ),
    )
    bundles = (
        _pre_registered_bundle(
            created_at=created_at,
            bundle_id="exitpolicy-v2-capital-guarded-candidate",
            capital_reduce=Decimal("0.06"),
            capital_close=Decimal("0.12"),
            thesis_reduce_confidence=Decimal("0.10"),
            thesis_close_confidence=Decimal("0.25"),
            thesis_reduce_score=Decimal("10"),
            thesis_close_score=Decimal("25"),
            trailing_min_mfe=Decimal("0.08"),
            trailing_reduce=Decimal("0.04"),
            trailing_close=Decimal("0.10"),
            defensive_reduce=2,
            defensive_close=4,
            concentration=Decimal("0.20"),
            stagnation_bars=20,
            stagnation_abs_return=Decimal("0.01"),
            cooldown_reduce=2,
            cooldown_close=4,
        ),
        _pre_registered_bundle(
            created_at=created_at,
            bundle_id="exitpolicy-v2-balanced-guarded-candidate",
            capital_reduce=Decimal("0.08"),
            capital_close=Decimal("0.16"),
            thesis_reduce_confidence=Decimal("0.15"),
            thesis_close_confidence=Decimal("0.25"),
            thesis_reduce_score=Decimal("15"),
            thesis_close_score=Decimal("25"),
            trailing_min_mfe=Decimal("0.12"),
            trailing_reduce=Decimal("0.06"),
            trailing_close=Decimal("0.10"),
            defensive_reduce=3,
            defensive_close=4,
            concentration=Decimal("0.25"),
            stagnation_bars=40,
            stagnation_abs_return=Decimal("0.02"),
            cooldown_reduce=2,
            cooldown_close=4,
        ),
        _pre_registered_bundle(
            created_at=created_at,
            bundle_id="exitpolicy-v2-patient-guarded-candidate",
            capital_reduce=Decimal("0.10"),
            capital_close=Decimal("0.20"),
            thesis_reduce_confidence=Decimal("0.15"),
            thesis_close_confidence=Decimal("0.35"),
            thesis_reduce_score=Decimal("15"),
            thesis_close_score=Decimal("35"),
            trailing_min_mfe=Decimal("0.12"),
            trailing_reduce=Decimal("0.06"),
            trailing_close=Decimal("0.14"),
            defensive_reduce=3,
            defensive_close=5,
            concentration=Decimal("0.25"),
            stagnation_bars=40,
            stagnation_abs_return=Decimal("0.01"),
            cooldown_reduce=4,
            cooldown_close=8,
        ),
    )
    return ExitPolicyV2CandidateRegistry.create(
        created_at=created_at,
        max_candidate_bundles=3,
        parameter_candidates=candidates,
        candidate_bundles=bundles,
        selection_objective=(
            "rank only configurations with sufficient completed lifecycle evidence",
            "prefer lower maximum drawdown and loss severity before higher realized return",
            "require stable REDUCE/CLOSE behavior without excessive churn",
            "require symbol and period stability before qualification",
            "treat catastrophic-loss avoidance as a hard priority",
        ),
        rejection_gates=(
            "reject insufficient completed lifecycle count",
            "reject inadequate validation exits",
            "reject excessive drawdown or adverse excursion",
            "reject excessive turnover or immediate reentry loops",
            "reject unstable symbol concentration",
            "reject severe period instability",
        ),
        tie_breaking_rules=(
            "lower drawdown wins ties",
            "lower turnover wins remaining ties",
            "higher completed lifecycle count wins remaining ties",
            "lexicographically smallest candidate bundle fingerprint wins final ties",
        ),
    )


def default_exit_policy_v2_experiment_manifest(
    *, created_at: datetime
) -> ExitPolicyV2ExperimentManifest:
    registry = default_exit_policy_v2_candidate_registry(created_at=created_at)
    research_exposed = (
        _partition(
            AssetClass.EQUITY,
            "RESEARCH_EXPOSED",
            "2024-01-01",
            "2026-08-28",
            _EQUITY_SYMBOLS,
            (),
            research_exposed=True,
        ),
        _partition(
            AssetClass.ETF,
            "RESEARCH_EXPOSED",
            "2024-01-01",
            "2026-08-28",
            _ETF_SYMBOLS,
            (),
            research_exposed=True,
        ),
        _partition(
            AssetClass.CRYPTO,
            "RESEARCH_EXPOSED",
            "2024-01-01",
            "2026-08-30",
            _CRYPTO_SYMBOLS,
            (),
            research_exposed=True,
        ),
    )
    partitions = (
        _partition(
            AssetClass.EQUITY,
            "TRAIN",
            "2016-01-04",
            "2019-12-31",
            _EQUITY_SYMBOLS,
            (),
        ),
        _partition(
            AssetClass.EQUITY,
            "VALIDATION",
            "2020-01-01",
            "2021-12-31",
            _EQUITY_SYMBOLS,
            (),
        ),
        _partition(
            AssetClass.EQUITY,
            "HOLDOUT",
            "2022-01-01",
            "2023-12-31",
            _EQUITY_SYMBOLS,
            (),
        ),
        _partition(AssetClass.ETF, "TRAIN", "2016-01-04", "2019-12-31", _ETF_SYMBOLS, ()),
        _partition(
            AssetClass.ETF,
            "VALIDATION",
            "2020-01-01",
            "2021-12-31",
            _ETF_SYMBOLS,
            (),
        ),
        _partition(AssetClass.ETF, "HOLDOUT", "2022-01-01", "2023-12-31", _ETF_SYMBOLS, ()),
        _partition(
            AssetClass.CRYPTO,
            "TRAIN",
            "2021-07-31",
            "2022-06-30",
            ("BTC", "ETH", "SOL", "BCH", "LINK", "LTC"),
            ("ADA", "AVAX", "DOT", "XRP"),
        ),
        _partition(
            AssetClass.CRYPTO,
            "VALIDATION",
            "2022-07-01",
            "2023-03-31",
            ("BTC", "ETH", "SOL", "BCH", "LINK", "LTC"),
            ("ADA", "AVAX", "DOT", "XRP"),
        ),
        _partition(
            AssetClass.CRYPTO,
            "HOLDOUT",
            "2023-04-01",
            "2023-12-31",
            ("BTC", "ETH", "SOL", "BCH", "LINK", "LTC"),
            ("ADA", "AVAX", "DOT", "XRP"),
        ),
    )
    return ExitPolicyV2ExperimentManifest.create(
        created_at=created_at,
        dataset_partitions=partitions,
        candidate_registry=registry,
        objective_rules=registry.selection_objective,
        rejection_rules=registry.rejection_gates,
        overfitting_controls=(
            "maximum candidate bundles = 3",
            "if no candidate passes rejection gates, V2 remains research-only",
            "no post-hoc parameter adjustment after manifest freeze",
            "HOLDOUT is evaluated only after TRAIN and VALIDATION selection is frozen",
            "RESEARCH_EXPOSED 2024-2026 observations cannot be pristine holdout",
        ),
        research_exposed_ranges=research_exposed,
    )


def default_exit_policy_v2_preregistered_experiment_v2_manifest(
    *, created_at: datetime
) -> ExitPolicyV2ExperimentManifest:
    registry = default_exit_policy_v2_candidate_registry(created_at=created_at)
    v1_manifest = default_exit_policy_v2_experiment_manifest(created_at=created_at)
    safety_invariants = (
        "confidence profile is frozen to V2_B_GUARDED for this experiment",
        "exit policy profile is frozen to EXITPOLICY_V2_GUARDED for this experiment",
        "candidate registry fingerprint is part of the immutable manifest fingerprint",
        "dataset partitions and eligible universe are part of the immutable manifest fingerprint",
        "selection protocol is frozen before TRAIN/VALIDATION execution",
        "HOLDOUT must not execute during candidate selection",
        "broker_write_calls must remain 0",
        "Demo execution remains disabled and Real execution remains unavailable",
        "cooldown after REDUCE/CLOSE blocks same-symbol OPEN/INCREASE while active",
        "REDUCE/CLOSE at timestamp T takes precedence over same-timestamp INCREASE",
    )
    return ExitPolicyV2ExperimentManifest.create(
        created_at=created_at,
        dataset_partitions=v1_manifest.dataset_partitions,
        candidate_registry=registry,
        objective_rules=v1_manifest.objective_rules,
        rejection_rules=v1_manifest.rejection_rules,
        overfitting_controls=v1_manifest.overfitting_controls,
        research_exposed_ranges=v1_manifest.research_exposed_ranges,
        experiment_version=EXIT_POLICY_V2_EXPERIMENT_V2_VERSION,
        confidence_profile=EXIT_POLICY_V2_REQUIRED_CONFIDENCE_PROFILE,
        exit_policy_profile=EXIT_POLICY_V2_GUARDED,
        safety_invariants=safety_invariants,
    )


class ExitPolicyReasonCode(StrEnum):
    CAPITAL_PROTECTION_REDUCE = "EXITPOLICY_V2_CAPITAL_PROTECTION_REDUCE"
    CAPITAL_PROTECTION_CLOSE = "EXITPOLICY_V2_CAPITAL_PROTECTION_CLOSE"
    THESIS_DETERIORATION_REDUCE = "EXITPOLICY_V2_THESIS_DETERIORATION_REDUCE"
    THESIS_INVALIDATION_CLOSE = "EXITPOLICY_V2_THESIS_INVALIDATION_CLOSE"
    TRAILING_PROFIT_REDUCE = "EXITPOLICY_V2_TRAILING_PROFIT_REDUCE"
    TRAILING_PROFIT_CLOSE = "EXITPOLICY_V2_TRAILING_PROFIT_CLOSE"
    REGIME_DETERIORATION_REDUCE = "EXITPOLICY_V2_REGIME_DETERIORATION_REDUCE"
    REGIME_INVALIDATION_CLOSE = "EXITPOLICY_V2_REGIME_INVALIDATION_CLOSE"
    PERSISTENT_DEFENSIVE_SIGNALS_REDUCE = "EXITPOLICY_V2_PERSISTENT_DEFENSIVE_SIGNALS_REDUCE"
    PERSISTENT_DEFENSIVE_SIGNALS_CLOSE = "EXITPOLICY_V2_PERSISTENT_DEFENSIVE_SIGNALS_CLOSE"
    POSITION_CONCENTRATION_REDUCE = "EXITPOLICY_V2_POSITION_CONCENTRATION_REDUCE"
    STAGNATION_REDUCE = "EXITPOLICY_V2_STAGNATION_REDUCE"
    COOLDOWN_BLOCKS_REENTRY = "EXITPOLICY_V2_COOLDOWN_BLOCKS_REENTRY"
    NO_EXIT_TRIGGER_HOLD = "EXITPOLICY_V2_NO_EXIT_TRIGGER_HOLD"
    PARAMETER_BUNDLE_REQUIRED = "EXITPOLICY_V2_PARAMETER_BUNDLE_REQUIRED"
    INSUFFICIENT_POSITION_STATE_HOLD = "EXITPOLICY_V2_INSUFFICIENT_POSITION_STATE_HOLD"


class ExitPolicyDecision(FrozenDomainModel):
    action: RecommendedAction
    amount: Decimal = Field(ge=0)
    reason: str = Field(min_length=1)
    policy_version: str = Field(default=EXIT_POLICY_VERSION, min_length=1)
    reason_code: str | None = Field(default=None, min_length=1)

    @property
    def is_exit_action(self) -> bool:
        return self.action in {RecommendedAction.REDUCE, RecommendedAction.CLOSE}


class ExitPolicy:
    """Deterministic exit policy for long-only positions.

    The policy only considers reductions for instruments already owned by the
    portfolio. A sell recommendation therefore reduces or closes a long holding;
    it never opens a short position.
    """

    policy_version = EXIT_POLICY_VERSION

    def evaluate(
        self,
        *,
        analysis: AegisOpportunityAnalysis,
        portfolio: PortfolioSnapshot,
        position_state: "PositionManagementState | None" = None,
    ) -> ExitPolicyDecision:
        del position_state
        instrument_id = analysis.candidate.instrument.numeric_instrument_id
        if instrument_id is None:
            return _hold("instrument ID is unavailable for exit evaluation")
        current_value = portfolio.market_value_for(instrument_id)
        if current_value <= 0:
            return _hold("no existing long position is available to reduce")

        if analysis.decision is AegisDecision.IGNORE:
            return ExitPolicyDecision(
                action=RecommendedAction.CLOSE,
                amount=current_value,
                reason="opportunity intelligence changed to IGNORE for an existing long position",
            )

        if analysis.decision is AegisDecision.REDUCE:
            if analysis.ensemble.strength >= Decimal("0.50"):
                return ExitPolicyDecision(
                    action=RecommendedAction.CLOSE,
                    amount=current_value,
                    reason=(
                        "strategy ensemble produced a strong REDUCE for an existing long position"
                    ),
                )
            return ExitPolicyDecision(
                action=RecommendedAction.REDUCE,
                amount=(current_value / Decimal("2")).quantize(Decimal("0.01")),
                reason="strategy ensemble produced REDUCE for an existing long position",
            )

        negative = sum(
            1
            for signal in analysis.strategy_signals
            if signal.direction in {StrategyDirection.REDUCE, StrategyDirection.AVOID}
        )
        active_buy = any(
            signal.direction in {StrategyDirection.STRONG_BUY, StrategyDirection.BUY}
            for signal in analysis.strategy_signals
        )
        if negative >= 2 and not active_buy:
            return ExitPolicyDecision(
                action=RecommendedAction.REDUCE,
                amount=(current_value / Decimal("2")).quantize(Decimal("0.01")),
                reason=(
                    "multiple defensive strategy signals reduced confidence in the held position"
                ),
            )

        return _hold("exit policy found no deterministic reduction trigger")


class ExitPolicyV2Parameters(FrozenDomainModel):
    parameter_bundle_id: str = Field(min_length=1)
    frozen: bool
    capital_reduce_drawdown_pct: Decimal = Field(ge=0, le=1)
    capital_close_drawdown_pct: Decimal = Field(ge=0, le=1)
    thesis_reduce_confidence_drop: Decimal = Field(ge=0, le=1)
    thesis_close_confidence_drop: Decimal = Field(ge=0, le=1)
    thesis_reduce_score_drop: Decimal = Field(ge=0, le=100)
    thesis_close_score_drop: Decimal = Field(ge=0, le=100)
    regime_deterioration_reduce_enabled: bool = True
    regime_invalidation_close_enabled: bool = True
    trailing_min_mfe_pct: Decimal = Field(ge=0, le=10)
    trailing_reduce_drawdown_pct: Decimal = Field(ge=0, le=1)
    trailing_close_drawdown_pct: Decimal = Field(ge=0, le=1)
    defensive_persistence_reduce_count: int = Field(ge=1)
    defensive_persistence_close_count: int = Field(ge=1)
    concentration_reduce_weight: Decimal = Field(ge=0, le=1)
    stagnation_bars: int = Field(ge=1)
    stagnation_abs_return_pct: Decimal = Field(ge=0, le=1)
    cooldown_bars_after_reduce: int = Field(ge=0)
    cooldown_bars_after_close: int = Field(ge=0)

    @model_validator(mode="after")
    def close_thresholds_are_at_least_reduce_thresholds(self) -> "ExitPolicyV2Parameters":
        if self.capital_close_drawdown_pct < self.capital_reduce_drawdown_pct:
            raise ValueError("capital close threshold must be at least reduce threshold")
        if self.thesis_close_confidence_drop < self.thesis_reduce_confidence_drop:
            raise ValueError("thesis close confidence drop must be at least reduce drop")
        if self.thesis_close_score_drop < self.thesis_reduce_score_drop:
            raise ValueError("thesis close score drop must be at least reduce drop")
        if self.trailing_close_drawdown_pct < self.trailing_reduce_drawdown_pct:
            raise ValueError("trailing close drawdown must be at least reduce drawdown")
        if self.defensive_persistence_close_count < self.defensive_persistence_reduce_count:
            raise ValueError("defensive close persistence must be at least reduce persistence")
        return self


ExitPolicyV2ParameterBundle.model_rebuild()
ExitPolicyV2CandidateRegistry.model_rebuild()
ExitPolicyV2ExperimentManifest.model_rebuild()


class PositionManagementState(FrozenDomainModel):
    instrument_id: int = Field(gt=0)
    symbol: str = Field(min_length=1)
    entry_timestamp: datetime
    entry_price: Decimal = Field(gt=0)
    cost_basis: Decimal = Field(gt=0)
    entry_confidence: Decimal = Field(ge=0, le=1)
    entry_opportunity_score: Decimal = Field(ge=0, le=100)
    entry_regime: RegimeLabel
    current_timestamp: datetime
    current_price: Decimal = Field(gt=0)
    current_confidence: Decimal = Field(ge=0, le=1)
    current_opportunity_score: Decimal = Field(ge=0, le=100)
    current_regime: RegimeLabel
    position_market_value: Decimal = Field(ge=0)
    position_weight: Decimal = Field(ge=0, le=1)
    bars_held: int = Field(ge=0)
    mfe: Decimal = Decimal("0")
    mae: Decimal = Decimal("0")
    post_entry_peak_price: Decimal = Field(gt=0)
    drawdown_from_post_entry_peak: Decimal = Field(ge=0)
    defensive_signal_persistence: int = Field(ge=0)
    last_reduce_timestamp: datetime | None = None
    last_close_timestamp: datetime | None = None
    last_exit_reason: str | None = Field(default=None, min_length=1)
    cooldown_bars_remaining: int = Field(default=0, ge=0)

    @field_validator(
        "entry_timestamp", "current_timestamp", "last_reduce_timestamp", "last_close_timestamp"
    )
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return require_aware(value, "position management timestamp")

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.current_price - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> Decimal:
        return self.unrealized_pnl / self.cost_basis

    @property
    def confidence_deterioration(self) -> Decimal:
        return max(Decimal("0"), self.entry_confidence - self.current_confidence)

    @property
    def opportunity_score_deterioration(self) -> Decimal:
        return max(Decimal("0"), self.entry_opportunity_score - self.current_opportunity_score)

    def update_from_observation(
        self,
        *,
        timestamp: datetime,
        price: Decimal,
        confidence: Decimal,
        opportunity_score: Decimal,
        regime: RegimeLabel,
        defensive_signal_active: bool,
    ) -> "PositionManagementState":
        timestamp = require_aware(timestamp, "position management observation timestamp")
        if timestamp <= self.current_timestamp:
            raise ValueError("position management updates must move forward in time")
        post_entry_peak = max(self.post_entry_peak_price, price)
        pnl_pct = (price - self.cost_basis) / self.cost_basis
        mfe = max(self.mfe, pnl_pct)
        mae = min(self.mae, pnl_pct)
        drawdown = Decimal("0")
        if post_entry_peak > 0 and price < post_entry_peak:
            drawdown = (post_entry_peak - price) / post_entry_peak
        persistence = self.defensive_signal_persistence + 1 if defensive_signal_active else 0
        cooldown = max(0, self.cooldown_bars_remaining - 1)
        return self.model_copy(
            update={
                "current_timestamp": timestamp,
                "current_price": price,
                "current_confidence": confidence,
                "current_opportunity_score": opportunity_score,
                "current_regime": regime,
                "bars_held": self.bars_held + 1,
                "mfe": mfe,
                "mae": mae,
                "post_entry_peak_price": post_entry_peak,
                "drawdown_from_post_entry_peak": drawdown,
                "defensive_signal_persistence": persistence,
                "cooldown_bars_remaining": cooldown,
            }
        )


class ExitPolicyV2Guarded:
    policy_version = EXIT_POLICY_V2_VERSION

    def __init__(
        self,
        parameters: ExitPolicyV2Parameters | None = None,
        *,
        parameter_bundle: ExitPolicyV2ParameterBundle | None = None,
    ) -> None:
        self._parameter_bundle = parameter_bundle
        self._parameters = (
            parameter_bundle.parameters if parameter_bundle is not None else parameters
        )

    def cooldown_bars_for_exit(self, action: RecommendedAction) -> int:
        if self._parameters is None:
            return 0
        if action is RecommendedAction.REDUCE:
            return self._parameters.cooldown_bars_after_reduce
        if action is RecommendedAction.CLOSE:
            return self._parameters.cooldown_bars_after_close
        return 0

    def evaluate(
        self,
        *,
        analysis: AegisOpportunityAnalysis,
        portfolio: PortfolioSnapshot,
        position_state: PositionManagementState | None = None,
    ) -> ExitPolicyDecision:
        instrument_id = analysis.candidate.instrument.numeric_instrument_id
        if instrument_id is None:
            return _hold_v2(
                ExitPolicyReasonCode.INSUFFICIENT_POSITION_STATE_HOLD,
                "instrument ID is unavailable for autonomous exit evaluation",
            )
        current_value = portfolio.market_value_for(instrument_id)
        if current_value <= 0 or position_state is None:
            return _hold_v2(
                ExitPolicyReasonCode.INSUFFICIENT_POSITION_STATE_HOLD,
                "no complete causal position state is available for autonomous exit evaluation",
            )
        if self._parameters is None or not self._parameters.frozen:
            return _hold_v2(
                ExitPolicyReasonCode.PARAMETER_BUNDLE_REQUIRED,
                "EXITPOLICY_V2_GUARDED requires an explicit frozen parameter bundle",
            )
        if position_state.cooldown_bars_remaining > 0:
            return _hold_v2(
                ExitPolicyReasonCode.COOLDOWN_BLOCKS_REENTRY,
                "cooldown is active after a previous autonomous exit action",
            )

        params = self._parameters
        candidates = (
            _capital_protection_decision(position_state, current_value, params),
            _thesis_decision(position_state, current_value, params),
            _trailing_decision(position_state, current_value, params),
            _regime_decision(position_state, current_value, params),
            _defensive_signal_decision(position_state, current_value, params),
            _concentration_decision(position_state, current_value, params),
            _stagnation_decision(position_state, current_value, params),
        )
        close = next(
            (decision for decision in candidates if decision.action is RecommendedAction.CLOSE),
            None,
        )
        if close is not None:
            return close
        reduce = next(
            (decision for decision in candidates if decision.action is RecommendedAction.REDUCE),
            None,
        )
        if reduce is not None:
            return reduce
        return _hold_v2(
            ExitPolicyReasonCode.NO_EXIT_TRIGGER_HOLD,
            "EXITPOLICY_V2_GUARDED found no autonomous reduction trigger",
        )


def select_exit_policy(
    profile: str,
    *,
    v2_parameters: ExitPolicyV2Parameters | None = None,
    v2_parameter_bundle: ExitPolicyV2ParameterBundle | None = None,
) -> ExitPolicy | ExitPolicyV2Guarded:
    if profile == EXIT_POLICY_V1_LEGACY:
        return ExitPolicy()
    if profile == EXIT_POLICY_V2_GUARDED:
        return ExitPolicyV2Guarded(v2_parameters, parameter_bundle=v2_parameter_bundle)
    raise ValueError("unsupported exit policy profile")


def select_exit_policy_for_historical_validation(
    profile: str,
    *,
    v2_parameter_bundle: ExitPolicyV2ParameterBundle | None = None,
    v2_candidate_registry: ExitPolicyV2CandidateRegistry | None = None,
    v2_experiment_manifest: ExitPolicyV2ExperimentManifest | None = None,
) -> ExitPolicy | ExitPolicyV2Guarded:
    if profile == EXIT_POLICY_V1_LEGACY:
        return ExitPolicy()
    if profile == EXIT_POLICY_V2_GUARDED:
        if v2_parameter_bundle is None:
            raise ValueError(
                "EXITPOLICY_V2_GUARDED historical validation requires a frozen parameter bundle"
            )
        if v2_candidate_registry is None:
            raise ValueError(
                "EXITPOLICY_V2_GUARDED historical validation requires a frozen candidate registry"
            )
        if v2_experiment_manifest is None:
            raise ValueError(
                "EXITPOLICY_V2_GUARDED historical validation requires a frozen experiment manifest"
            )
        if v2_parameter_bundle.fingerprint not in {
            bundle.fingerprint for bundle in v2_candidate_registry.candidate_bundles
        }:
            raise ValueError("parameter bundle is not registered in the frozen candidate registry")
        if (
            v2_parameter_bundle.fingerprint
            not in v2_experiment_manifest.candidate_bundle_fingerprints
        ):
            raise ValueError("parameter bundle is not registered in the frozen experiment manifest")
        if (
            v2_candidate_registry.fingerprint
            != v2_experiment_manifest.candidate_registry_fingerprint
        ):
            raise ValueError("candidate registry does not match the frozen experiment manifest")
        return ExitPolicyV2Guarded(parameter_bundle=v2_parameter_bundle)
    raise ValueError("unsupported exit policy profile")


def exit_action_blocks_entry(action: RecommendedAction) -> bool:
    return action in {RecommendedAction.REDUCE, RecommendedAction.CLOSE}


def reduce_amount_not_exceeding_long_position(
    requested_amount: Decimal,
    current_value: Decimal,
) -> Decimal:
    return max(Decimal("0"), min(requested_amount, current_value)).quantize(Decimal("0.01"))


def _capital_protection_decision(
    state: PositionManagementState,
    current_value: Decimal,
    params: ExitPolicyV2Parameters,
) -> ExitPolicyDecision:
    drawdown = abs(min(state.unrealized_pnl_pct, Decimal("0")))
    if drawdown >= params.capital_close_drawdown_pct:
        return _close_v2(
            current_value,
            ExitPolicyReasonCode.CAPITAL_PROTECTION_CLOSE,
            "capital protection close threshold was reached",
        )
    if drawdown >= params.capital_reduce_drawdown_pct:
        return _reduce_half_v2(
            current_value,
            ExitPolicyReasonCode.CAPITAL_PROTECTION_REDUCE,
            "capital protection reduce threshold was reached",
        )
    return _no_branch()


def _thesis_decision(
    state: PositionManagementState,
    current_value: Decimal,
    params: ExitPolicyV2Parameters,
) -> ExitPolicyDecision:
    if (
        state.confidence_deterioration >= params.thesis_close_confidence_drop
        or state.opportunity_score_deterioration >= params.thesis_close_score_drop
    ):
        return _close_v2(
            current_value,
            ExitPolicyReasonCode.THESIS_INVALIDATION_CLOSE,
            "thesis invalidation close threshold was reached",
        )
    if (
        state.confidence_deterioration >= params.thesis_reduce_confidence_drop
        or state.opportunity_score_deterioration >= params.thesis_reduce_score_drop
    ):
        return _reduce_half_v2(
            current_value,
            ExitPolicyReasonCode.THESIS_DETERIORATION_REDUCE,
            "thesis deterioration reduce threshold was reached",
        )
    return _no_branch()


def _trailing_decision(
    state: PositionManagementState,
    current_value: Decimal,
    params: ExitPolicyV2Parameters,
) -> ExitPolicyDecision:
    if state.mfe < params.trailing_min_mfe_pct:
        return _no_branch()
    if state.drawdown_from_post_entry_peak >= params.trailing_close_drawdown_pct:
        return _close_v2(
            current_value,
            ExitPolicyReasonCode.TRAILING_PROFIT_CLOSE,
            "trailing profit close threshold was reached",
        )
    if state.drawdown_from_post_entry_peak >= params.trailing_reduce_drawdown_pct:
        return _reduce_half_v2(
            current_value,
            ExitPolicyReasonCode.TRAILING_PROFIT_REDUCE,
            "trailing profit reduce threshold was reached",
        )
    return _no_branch()


def _regime_decision(
    state: PositionManagementState,
    current_value: Decimal,
    params: ExitPolicyV2Parameters,
) -> ExitPolicyDecision:
    if (
        params.regime_invalidation_close_enabled
        and state.entry_regime is RegimeLabel.STRONG_UPTREND
        and state.current_regime
        in {
            RegimeLabel.STRONG_DOWNTREND,
            RegimeLabel.DOWNTREND,
        }
    ):
        return _close_v2(
            current_value,
            ExitPolicyReasonCode.REGIME_INVALIDATION_CLOSE,
            "regime invalidated the original strong uptrend thesis",
        )
    if (
        params.regime_deterioration_reduce_enabled
        and state.entry_regime in {RegimeLabel.STRONG_UPTREND, RegimeLabel.UPTREND}
        and state.current_regime is RegimeLabel.TRANSITION
    ):
        return _reduce_half_v2(
            current_value,
            ExitPolicyReasonCode.REGIME_DETERIORATION_REDUCE,
            "regime deteriorated from the original trend thesis",
        )
    return _no_branch()


def _defensive_signal_decision(
    state: PositionManagementState,
    current_value: Decimal,
    params: ExitPolicyV2Parameters,
) -> ExitPolicyDecision:
    if state.defensive_signal_persistence >= params.defensive_persistence_close_count:
        return _close_v2(
            current_value,
            ExitPolicyReasonCode.PERSISTENT_DEFENSIVE_SIGNALS_CLOSE,
            "persistent defensive evidence reached close threshold",
        )
    if state.defensive_signal_persistence >= params.defensive_persistence_reduce_count:
        return _reduce_half_v2(
            current_value,
            ExitPolicyReasonCode.PERSISTENT_DEFENSIVE_SIGNALS_REDUCE,
            "persistent defensive evidence reached reduce threshold",
        )
    return _no_branch()


def _concentration_decision(
    state: PositionManagementState,
    current_value: Decimal,
    params: ExitPolicyV2Parameters,
) -> ExitPolicyDecision:
    if state.position_weight >= params.concentration_reduce_weight:
        return _reduce_half_v2(
            current_value,
            ExitPolicyReasonCode.POSITION_CONCENTRATION_REDUCE,
            "position concentration reached rebalance threshold",
        )
    return _no_branch()


def _stagnation_decision(
    state: PositionManagementState,
    current_value: Decimal,
    params: ExitPolicyV2Parameters,
) -> ExitPolicyDecision:
    if state.bars_held >= params.stagnation_bars and (
        abs(state.unrealized_pnl_pct) <= params.stagnation_abs_return_pct
    ):
        return _reduce_half_v2(
            current_value,
            ExitPolicyReasonCode.STAGNATION_REDUCE,
            "position stagnation reached reduce threshold",
        )
    return _no_branch()


def _close_v2(
    current_value: Decimal,
    reason_code: ExitPolicyReasonCode,
    reason: str,
) -> ExitPolicyDecision:
    return ExitPolicyDecision(
        action=RecommendedAction.CLOSE,
        amount=reduce_amount_not_exceeding_long_position(current_value, current_value),
        reason=reason,
        reason_code=reason_code.value,
        policy_version=EXIT_POLICY_V2_VERSION,
    )


def _reduce_half_v2(
    current_value: Decimal,
    reason_code: ExitPolicyReasonCode,
    reason: str,
) -> ExitPolicyDecision:
    return ExitPolicyDecision(
        action=RecommendedAction.REDUCE,
        amount=reduce_amount_not_exceeding_long_position(
            current_value / Decimal("2"),
            current_value,
        ),
        reason=reason,
        reason_code=reason_code.value,
        policy_version=EXIT_POLICY_V2_VERSION,
    )


def _hold_v2(reason_code: ExitPolicyReasonCode, reason: str) -> ExitPolicyDecision:
    return ExitPolicyDecision(
        action=RecommendedAction.HOLD,
        amount=Decimal("0"),
        reason=reason,
        reason_code=reason_code.value,
        policy_version=EXIT_POLICY_V2_VERSION,
    )


def _no_branch() -> ExitPolicyDecision:
    return ExitPolicyDecision(
        action=RecommendedAction.HOLD, amount=Decimal("0"), reason="no branch"
    )


def _hold(reason: str) -> ExitPolicyDecision:
    return ExitPolicyDecision(action=RecommendedAction.HOLD, amount=Decimal("0"), reason=reason)


_EQUITY_SYMBOLS = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "JPM",
    "UNH",
    "XOM",
    "COST",
)
_ETF_SYMBOLS = ("SPY", "QQQ", "VTI", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLU", "TLT", "GLD")
_CRYPTO_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "LINK", "LTC", "BCH", "DOT")


def _candidate(
    parameter_name: str,
    values: tuple[Decimal | int | bool, ...],
    unit: str,
    asset_class_specific: bool,
    rationale: str,
    *,
    constraints: tuple[str, ...] = (),
) -> ExitPolicyV2ParameterCandidate:
    return ExitPolicyV2ParameterCandidate(
        parameter_name=parameter_name,
        candidate_values=values,
        unit=unit,
        asset_class_scope=(
            AssetClass.EQUITY,
            AssetClass.ETF,
            AssetClass.CRYPTO,
        ),
        rationale=rationale,
        provenance="pre-registered governance contract; not derived from observed outcomes",
        shared_across_asset_classes=not asset_class_specific,
        constraints=constraints,
    )


def _pre_registered_bundle(
    *,
    created_at: datetime,
    bundle_id: str,
    capital_reduce: Decimal,
    capital_close: Decimal,
    thesis_reduce_confidence: Decimal,
    thesis_close_confidence: Decimal,
    thesis_reduce_score: Decimal,
    thesis_close_score: Decimal,
    trailing_min_mfe: Decimal,
    trailing_reduce: Decimal,
    trailing_close: Decimal,
    defensive_reduce: int,
    defensive_close: int,
    concentration: Decimal,
    stagnation_bars: int,
    stagnation_abs_return: Decimal,
    cooldown_reduce: int,
    cooldown_close: int,
) -> ExitPolicyV2ParameterBundle:
    return ExitPolicyV2ParameterBundle.create(
        created_at=created_at,
        creation_rationale=(
            "pre-registered candidate for guarded exit lifecycle validation; "
            "not selected from historical replay outcomes"
        ),
        parameter_provenance=("small bounded candidate grid declared before replay",),
        asset_class_scope=(AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO),
        validation_status=ExitPolicyV2ValidationStatus.FROZEN_FOR_VALIDATION,
        parameters=ExitPolicyV2Parameters(
            parameter_bundle_id=bundle_id,
            frozen=True,
            capital_reduce_drawdown_pct=capital_reduce,
            capital_close_drawdown_pct=capital_close,
            thesis_reduce_confidence_drop=thesis_reduce_confidence,
            thesis_close_confidence_drop=thesis_close_confidence,
            thesis_reduce_score_drop=thesis_reduce_score,
            thesis_close_score_drop=thesis_close_score,
            regime_deterioration_reduce_enabled=True,
            regime_invalidation_close_enabled=True,
            trailing_min_mfe_pct=trailing_min_mfe,
            trailing_reduce_drawdown_pct=trailing_reduce,
            trailing_close_drawdown_pct=trailing_close,
            defensive_persistence_reduce_count=defensive_reduce,
            defensive_persistence_close_count=defensive_close,
            concentration_reduce_weight=concentration,
            stagnation_bars=stagnation_bars,
            stagnation_abs_return_pct=stagnation_abs_return,
            cooldown_bars_after_reduce=cooldown_reduce,
            cooldown_bars_after_close=cooldown_close,
        ),
    )


def _partition(
    asset_class: AssetClass,
    role: str,
    start: str,
    end: str,
    eligible_symbols: tuple[str, ...],
    insufficient_history_symbols: tuple[str, ...],
    *,
    research_exposed: bool = False,
) -> ExitPolicyV2DatasetPartition:
    return ExitPolicyV2DatasetPartition(
        asset_class=asset_class,
        role=role,
        start=datetime.fromisoformat(f"{start}T00:00:00+00:00"),
        end=datetime.fromisoformat(f"{end}T23:59:59+00:00"),
        eligible_symbols=eligible_symbols,
        insufficient_history_symbols=insufficient_history_symbols,
        research_exposed=research_exposed,
    )


def _partitions_overlap(partitions: tuple[ExitPolicyV2DatasetPartition, ...]) -> bool:
    grouped: dict[AssetClass, list[ExitPolicyV2DatasetPartition]] = {}
    for partition in partitions:
        grouped.setdefault(partition.asset_class, []).append(partition)
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: item.start)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if left.end >= right.start:
                return True
    return False


def _holdout_overlaps_research_exposed(
    partitions: tuple[ExitPolicyV2DatasetPartition, ...],
    research_exposed: tuple[ExitPolicyV2DatasetPartition, ...],
) -> bool:
    holdouts = tuple(partition for partition in partitions if partition.role == "HOLDOUT")
    for holdout in holdouts:
        for exposed in research_exposed:
            if holdout.asset_class is not exposed.asset_class:
                continue
            if holdout.start <= exposed.end and exposed.start <= holdout.end:
                return True
    return False


def _stable_sha256(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
