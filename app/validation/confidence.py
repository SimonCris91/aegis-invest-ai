"""Research-only confidence ablation and calibration diagnostics."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from math import sqrt

from app.intelligence.confidence import compute_v2b_signal_reliability_confidence
from app.intelligence.models import StrategyDirection
from app.validation.models import ReplayDecision

_ZERO = Decimal("0")
_ONE = Decimal("1")
_V2_A = "V2_A_SIGNAL_RELIABILITY_AGREEMENT_REGIME_REQUIRED_FEATURES"
_V2_B = "V2_B_WEIGHTED_RELIABILITY_COMPONENTS"
_V2_C = "V2_C_TRAIN_CALIBRATED_RELIABILITY_MAPPING"


def build_confidence_ablation_study(
    decisions: tuple[ReplayDecision, ...],
) -> dict[str, object]:
    """Compare research-only confidence variants without changing production behavior."""

    rows = tuple(_confidence_row(decision) for decision in decisions)
    if not rows:
        return {
            "status": "NO_DECISIONS",
            "production_formula_changed": False,
            "variants": (),
        }
    variants = {
        "BASELINE": tuple(row["baseline"] for row in rows),
        "ABLATION_A_REMOVE_REGIME_MULTIPLIER_FROM_ENSEMBLE": tuple(
            row["remove_regime_multiplier_from_ensemble"] for row in rows
        ),
        "ABLATION_B_REMOVE_FINAL_REGIME_TERM": tuple(
            row["remove_final_regime_term"] for row in rows
        ),
        "ABLATION_C_REMOVE_FEATURE_QUALITY_MULTIPLIER_FROM_ENSEMBLE": tuple(
            row["remove_feature_quality_multiplier_from_ensemble"] for row in rows
        ),
        "ABLATION_D_REMOVE_FINAL_DATA_QUALITY_TERM": tuple(
            row["remove_final_data_quality_term"] for row in rows
        ),
        "ABLATION_E_REGIME_ONCE_FINAL_ONLY": tuple(
            row["remove_regime_multiplier_from_ensemble"] for row in rows
        ),
        "ABLATION_F_DATA_QUALITY_ONCE_FINAL_ONLY": tuple(
            row["remove_feature_quality_multiplier_from_ensemble"] for row in rows
        ),
        "ABLATION_G_ENSEMBLE_ONLY_CURRENT_INTERNAL_PENALTIES": tuple(
            row["ensemble_current"] for row in rows
        ),
    }
    baseline = variants["BASELINE"]
    return {
        "status": "RESEARCH_COUNTERFACTUAL_ONLY",
        "production_formula_changed": False,
        "variants": tuple(
            _variant_payload(name, values, baseline, decisions) for name, values in variants.items()
        ),
        "component_marginal_deltas": _component_marginal_deltas(rows),
        "score_confidence_overlap": _score_confidence_overlap(decisions),
        "rounding_sensitivity": _rounding_sensitivity(rows),
        "quality_penalty_semantics": _quality_penalty_semantics(decisions),
        "calibration_methodology": _calibration_methodology(),
    }


def build_confidence_v2_research_study(
    decisions: tuple[ReplayDecision, ...],
) -> dict[str, object]:
    """Evaluate V2 confidence semantics research-only on score-qualified decisions."""

    score_pass = tuple(decision for decision in decisions if _score_passed(decision))
    if not score_pass:
        return {
            "status": "NO_SCORE_PASS_DECISIONS",
            "scope": "RESEARCH_COUNTERFACTUAL_ONLY",
            "production_changed": False,
        }
    train, validation, oos = _chronological_splits(score_pass)
    train_calibration = _train_calibration(train)
    variants = {
        "V1_BASELINE": tuple(decision.confidence for decision in score_pass),
        _V2_A: tuple(_v2_a(decision) for decision in score_pass),
        _V2_B: tuple(_v2_b(decision) for decision in score_pass),
        _V2_C: tuple(_v2_c(decision, train_calibration) for decision in score_pass),
    }
    thresholds = _candidate_thresholds(train, train_calibration)
    selected_thresholds = _select_thresholds(validation, thresholds, train_calibration)
    recommendation = _v2_recommendation(variants, score_pass, validation, oos)
    return {
        "status": "RESEARCH_COUNTERFACTUAL_ONLY",
        "production_changed": False,
        "score_pass_sample_count": len(score_pass),
        "semantic_definition": (
            "Signal reliability confidence estimates whether a score-qualified directional "
            "opportunity is trustworthy enough for proposal consideration. Execution readiness "
            "is separate market-quality evidence for liquidity, spread, volume, slippage, and "
            "transaction-cost realism."
        ),
        "input_classification_matrix": _input_classification_matrix(),
        "feature_matrix": _feature_matrix(),
        "quality_partitions": _quality_partitions(score_pass),
        "variants": tuple(
            _v2_variant_payload(name, values, score_pass) for name, values in variants.items()
        ),
        "train": _split_payload("TRAIN", train, variants, score_pass),
        "validation": _split_payload("VALIDATION", validation, variants, score_pass),
        "out_of_sample": _split_payload("OUT_OF_SAMPLE", oos, variants, score_pass),
        "threshold_candidates_from_train": thresholds,
        "selected_thresholds_from_validation": selected_thresholds,
        "walk_forward": _walk_forward_payload(score_pass),
        "v1_vs_v2_comparison": _v1_vs_v2_comparison(variants, score_pass),
        "recommendation": recommendation,
        "closure_status": _v2_closure_status(recommendation),
        "windows_command_required": False,
    }


def _score_passed(decision: ReplayDecision) -> bool:
    for gate in decision.proposal_gate_trace:
        if gate.get("gate") == "opportunity_score":
            return gate.get("passed") is True
    return False


def _v2_a(decision: ReplayDecision) -> Decimal:
    details = decision.confidence_decomposition
    base = Decimal(str(details["mean_base_strategy_confidence"]))
    agreement = Decimal(str(details["agreement_ratio"]))
    regime = Decimal(str(details["regime_confidence"]))
    disagreement = Decimal(str(details.get("strong_disagreement_multiplier", "1")))
    required_sufficiency = _required_feature_sufficiency(decision)
    value = (
        base * Decimal("0.45")
        + agreement * Decimal("0.25")
        + regime * Decimal("0.20")
        + required_sufficiency * Decimal("0.10")
    )
    return _clamp(value * disagreement).quantize(Decimal("0.0001"))


def _v2_b(decision: ReplayDecision) -> Decimal:
    details = decision.confidence_decomposition
    directions = tuple(
        direction
        for name, count in decision.strategy_signal_counts.items()
        for direction in (StrategyDirection(name),) * count
    )
    raw_strategy_confidences = details.get("strategy_confidences", ())
    strategy_confidences = (
        tuple(
            Decimal(str(item["confidence"]))
            for item in raw_strategy_confidences
            if isinstance(item, dict) and "confidence" in item
        )
        if isinstance(raw_strategy_confidences, tuple)
        else ()
    ) or (Decimal(str(details["mean_base_strategy_confidence"])),)
    return compute_v2b_signal_reliability_confidence(
        strategy_directions=directions,
        strategy_confidences=strategy_confidences,
        agreement_ratio=Decimal(str(details["agreement_ratio"])),
        regime_confidence=Decimal(str(details["regime_confidence"])),
        required_feature_sufficiency=_required_feature_sufficiency(decision),
    ).confidence


def _v2_c(decision: ReplayDecision, calibration: dict[str, Decimal]) -> Decimal:
    raw = _v2_b(decision)
    slope = calibration.get("slope", Decimal("0"))
    intercept = calibration.get("intercept", Decimal("0.50"))
    return _clamp(intercept + slope * raw).quantize(Decimal("0.0001"))


def _required_feature_sufficiency(decision: ReplayDecision) -> Decimal:
    missing = set(_missing_features(decision))
    required_missing = missing.intersection(_required_directional_features())
    return Decimal("1") if not required_missing else Decimal("0.35")


def _directional_consistency(decision: ReplayDecision) -> Decimal:
    counts = decision.strategy_signal_counts
    positive = counts.get("STRONG_BUY", 0) + counts.get("BUY", 0) + counts.get("WATCH", 0)
    negative = counts.get("REDUCE", 0) + counts.get("AVOID", 0)
    total = sum(counts.values())
    if total <= 0:
        return Decimal("0")
    return (Decimal(max(positive - negative, 0)) / Decimal(total)).quantize(Decimal("0.0001"))


def _chronological_splits(
    decisions: tuple[ReplayDecision, ...],
) -> tuple[tuple[ReplayDecision, ...], tuple[ReplayDecision, ...], tuple[ReplayDecision, ...]]:
    ordered = tuple(sorted(decisions, key=lambda item: item.timestamp))
    train_end = max(1, int(len(ordered) * 0.50))
    validation_end = max(train_end + 1, int(len(ordered) * 0.75))
    return ordered[:train_end], ordered[train_end:validation_end], ordered[validation_end:]


def _train_calibration(decisions: tuple[ReplayDecision, ...]) -> dict[str, Decimal]:
    pairs = tuple(
        (_v2_b(decision), _correctness_label(decision))
        for decision in decisions
        if decision.forward_return is not None
    )
    if len(pairs) < 2:
        return {"intercept": Decimal("0.50"), "slope": Decimal("0")}
    xs = tuple(item[0] for item in pairs)
    ys = tuple(item[1] for item in pairs)
    x_mean = _mean(xs)
    y_mean = _mean(ys)
    numerator = sum(((x - x_mean) * (y - y_mean) for x, y in pairs), _ZERO)
    denominator = sum(((x - x_mean) ** 2 for x in xs), _ZERO)
    slope = _ZERO if denominator == 0 else numerator / denominator
    return {
        "intercept": _clamp(y_mean - slope * x_mean),
        "slope": max(Decimal("-2"), min(Decimal("2"), slope)),
        "train_base_rate": y_mean,
    }


def _candidate_thresholds(
    train: tuple[ReplayDecision, ...], calibration: dict[str, Decimal]
) -> tuple[dict[str, object], ...]:
    candidates: list[dict[str, object]] = []
    for name, values in {
        _V2_A: tuple(_v2_a(decision) for decision in train),
        _V2_B: tuple(_v2_b(decision) for decision in train),
        _V2_C: tuple(_v2_c(decision, calibration) for decision in train),
    }.items():
        for percentile in (Decimal("0.50"), Decimal("0.60"), Decimal("0.70"), Decimal("0.80")):
            threshold = _percentile(list(values), percentile)
            candidates.append(
                {
                    "variant": name,
                    "source": "TRAIN_CONFIDENCE_QUANTILE",
                    "percentile": str(percentile),
                    "threshold": str(threshold),
                }
            )
    return tuple(candidates)


def _select_thresholds(
    validation: tuple[ReplayDecision, ...],
    candidates: tuple[dict[str, object], ...],
    calibration: dict[str, Decimal],
) -> tuple[dict[str, object], ...]:
    selected: list[dict[str, object]] = []
    values_by_name = {
        _V2_A: tuple(_v2_a(decision) for decision in validation),
        _V2_B: tuple(_v2_b(decision) for decision in validation),
        _V2_C: tuple(_v2_c(decision, calibration) for decision in validation),
    }
    for variant in (_V2_A, _V2_B, _V2_C):
        variant_candidates = tuple(item for item in candidates if item["variant"] == variant)
        best = max(
            variant_candidates,
            key=lambda item: _threshold_reliability(
                validation, values_by_name[variant], Decimal(str(item["threshold"]))
            ),
        )
        selected.append(
            {
                **best,
                "selection_source": "VALIDATION_ONLY",
                "oos_used_for_selection": False,
                "validation_reliability": str(
                    _threshold_reliability(
                        validation, values_by_name[variant], Decimal(str(best["threshold"]))
                    )
                ),
            }
        )
    return tuple(selected)


def _threshold_reliability(
    decisions: tuple[ReplayDecision, ...], values: tuple[Decimal, ...], threshold: Decimal
) -> Decimal:
    selected = tuple(
        decision
        for decision, value in zip(decisions, values, strict=True)
        if value >= threshold and decision.forward_return is not None
    )
    if len(selected) < 5:
        return Decimal("-1")
    return _mean(tuple(_correctness_label(decision) for decision in selected))


def _v2_variant_payload(
    name: str, values: tuple[Decimal, ...], decisions: tuple[ReplayDecision, ...]
) -> dict[str, object]:
    labels = tuple(
        _correctness_label(decision)
        for decision in decisions
        if decision.forward_return is not None
    )
    aligned_values = tuple(
        value
        for value, decision in zip(values, decisions, strict=True)
        if decision.forward_return is not None
    )
    return {
        "variant": name,
        "scope": "RESEARCH_COUNTERFACTUAL_ONLY"
        if name != "V1_BASELINE"
        else "CURRENT_PRODUCTION_FORMULA_OBSERVED",
        "distribution": _decimal_distribution(values),
        "mean": str(_mean(values).quantize(Decimal("0.0001"))),
        "standard_deviation": str(_stddev(values).quantize(Decimal("0.0001"))),
        "directional_correctness_rate": str(_mean(labels).quantize(Decimal("0.0001"))),
        "confidence_vs_correctness_spearman": str(
            _spearman(aligned_values, labels).quantize(Decimal("0.0001"))
        ),
        "confidence_vs_forward_return_spearman": str(
            _spearman_with_forward_return(values, decisions).quantize(Decimal("0.0001"))
        ),
        "brier_score": str(_brier(aligned_values, labels).quantize(Decimal("0.0001"))),
        "calibration_error": str(
            _calibration_error(aligned_values, labels).quantize(Decimal("0.0001"))
        ),
        "precision_high_confidence": str(
            _precision_high_confidence(values, decisions).quantize(Decimal("0.0001"))
        ),
        "false_positive_rate_above_median": str(
            _standard_false_positive_rate_above_median(values, decisions).quantize(
                Decimal("0.0001")
            )
        ),
        "false_discovery_rate_above_median": str(
            _false_discovery_rate_above_median(values, decisions).quantize(Decimal("0.0001"))
        ),
        "calibration_bins": _value_bins(values, decisions),
    }


def _split_payload(
    label: str,
    split: tuple[ReplayDecision, ...],
    variants: dict[str, tuple[Decimal, ...]],
    full_decisions: tuple[ReplayDecision, ...],
) -> tuple[dict[str, object], ...]:
    indexes = {id(decision) for decision in split}
    return tuple(
        _v2_variant_payload(
            name,
            tuple(
                value
                for value, decision in zip(values, full_decisions, strict=True)
                if id(decision) in indexes
            ),
            split,
        )
        | {"split": label}
        for name, values in variants.items()
    )


def _walk_forward_payload(decisions: tuple[ReplayDecision, ...]) -> tuple[dict[str, object], ...]:
    ordered = tuple(sorted(decisions, key=lambda item: item.timestamp))
    if len(ordered) < 80:
        return ()
    window_size = max(40, len(ordered) // 3)
    step = max(20, window_size // 2)
    windows: list[dict[str, object]] = []
    start = 0
    while start + window_size <= len(ordered):
        window = ordered[start : start + window_size]
        train, validation, oos = _chronological_splits(window)
        calibration = _train_calibration(train)
        candidate_thresholds = _candidate_thresholds(train, calibration)
        selected = _select_thresholds(validation, candidate_thresholds, calibration)
        windows.append(
            {
                "window": len(windows) + 1,
                "train_count": len(train),
                "validation_count": len(validation),
                "oos_count": len(oos),
                "selected_thresholds": selected,
                "oos_metrics": _oos_threshold_metrics(oos, selected, calibration),
            }
        )
        start += step
    return tuple(windows)


def _oos_threshold_metrics(
    oos: tuple[ReplayDecision, ...],
    selected: tuple[dict[str, object], ...],
    calibration: dict[str, Decimal],
) -> tuple[dict[str, object], ...]:
    values = {
        _V2_A: tuple(_v2_a(decision) for decision in oos),
        _V2_B: tuple(_v2_b(decision) for decision in oos),
        _V2_C: tuple(_v2_c(decision, calibration) for decision in oos),
    }
    rows: list[dict[str, object]] = []
    for item in selected:
        variant = str(item["variant"])
        threshold = Decimal(str(item["threshold"]))
        picked = tuple(
            decision
            for decision, value in zip(oos, values[variant], strict=True)
            if value >= threshold and decision.forward_return is not None
        )
        rows.append(
            {
                "variant": variant,
                "threshold": str(threshold),
                "selected_count": len(picked),
                "correctness_rate": str(
                    _mean(tuple(_correctness_label(decision) for decision in picked)).quantize(
                        Decimal("0.0001")
                    )
                    if picked
                    else _ZERO
                ),
                "mean_forward_return": str(
                    _mean(tuple(decision.forward_return or _ZERO for decision in picked)).quantize(
                        Decimal("0.0001")
                    )
                    if picked
                    else _ZERO
                ),
            }
        )
    return tuple(rows)


def _v1_vs_v2_comparison(
    variants: dict[str, tuple[Decimal, ...]], decisions: tuple[ReplayDecision, ...]
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "variant": name,
            "sample_count": len(values),
            "distribution": _decimal_distribution(values),
            "confidence_vs_score_spearman": str(
                _spearman(values, tuple(decision.score for decision in decisions)).quantize(
                    Decimal("0.0001")
                )
            ),
            "confidence_vs_correctness_spearman": str(
                _spearman(
                    tuple(
                        value
                        for value, decision in zip(values, decisions, strict=True)
                        if decision.forward_return is not None
                    ),
                    tuple(
                        _correctness_label(decision)
                        for decision in decisions
                        if decision.forward_return is not None
                    ),
                ).quantize(Decimal("0.0001"))
            ),
        }
        for name, values in variants.items()
    )


def _v2_recommendation(
    variants: dict[str, tuple[Decimal, ...]],
    decisions: tuple[ReplayDecision, ...],
    validation: tuple[ReplayDecision, ...],
    oos: tuple[ReplayDecision, ...],
) -> str:
    baseline = _variant_correctness_spearman(variants["V1_BASELINE"], decisions)
    best_v2 = max(
        _variant_correctness_spearman(values, decisions)
        for name, values in variants.items()
        if name != "V1_BASELINE"
    )
    if len(validation) < 30 or len(oos) < 30:
        return "INSUFFICIENT_EVIDENCE"
    if best_v2 - baseline >= Decimal("0.05"):
        return "PROMOTE_V2_RECOMMENDED"
    return "INSUFFICIENT_EVIDENCE"


def _v2_closure_status(recommendation: str) -> str:
    if recommendation == "PROMOTE_V2_RECOMMENDED":
        return "STEP_8_0A_CLOSE_READY_PENDING_V2_PROMOTION"
    if recommendation == "KEEP_V1":
        return "STEP_8_0A_KEEP_V1_AND_REQUIRES_MORE_EVIDENCE"
    return "STEP_8_0A_REQUIRES_MORE_CONFIDENCE_WORK"


def _variant_correctness_spearman(
    values: tuple[Decimal, ...], decisions: tuple[ReplayDecision, ...]
) -> Decimal:
    aligned_values = tuple(
        value
        for value, decision in zip(values, decisions, strict=True)
        if decision.forward_return is not None
    )
    labels = tuple(
        _correctness_label(decision)
        for decision in decisions
        if decision.forward_return is not None
    )
    return _spearman(aligned_values, labels)


def _correctness_label(decision: ReplayDecision) -> Decimal:
    if decision.forward_return is None:
        return _ZERO
    return _ONE if decision.forward_return > 0 else _ZERO


def _brier(values: tuple[Decimal, ...], labels: tuple[Decimal, ...]) -> Decimal:
    if not values or len(values) != len(labels):
        return _ZERO
    return _mean(tuple((value - label) ** 2 for value, label in zip(values, labels, strict=True)))


def _calibration_error(values: tuple[Decimal, ...], labels: tuple[Decimal, ...]) -> Decimal:
    if not values or len(values) != len(labels):
        return _ZERO
    buckets: dict[Decimal, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for value, label in zip(values, labels, strict=True):
        buckets[value.quantize(Decimal("0.1"))].append((value, label))
    total = Decimal(len(values))
    weighted_error = _ZERO
    for pairs in buckets.values():
        confidence_mean = _mean(tuple(pair[0] for pair in pairs))
        observed_rate = _mean(tuple(pair[1] for pair in pairs))
        weighted_error += (Decimal(len(pairs)) / total) * abs(confidence_mean - observed_rate)
    return weighted_error


def _precision_high_confidence(
    values: tuple[Decimal, ...], decisions: tuple[ReplayDecision, ...]
) -> Decimal:
    if not values:
        return _ZERO
    threshold = _percentile(list(values), Decimal("0.75"))
    selected = tuple(
        decision
        for value, decision in zip(values, decisions, strict=True)
        if value >= threshold and decision.forward_return is not None
    )
    if not selected:
        return _ZERO
    return _mean(tuple(_correctness_label(decision) for decision in selected))


def _standard_false_positive_rate_above_median(
    values: tuple[Decimal, ...], decisions: tuple[ReplayDecision, ...]
) -> Decimal:
    if not values:
        return _ZERO
    threshold = _percentile(list(values), Decimal("0.50"))
    false_positives = 0
    true_negatives = 0
    for value, decision in zip(values, decisions, strict=True):
        if decision.forward_return is None:
            continue
        high_confidence = value >= threshold
        correct = decision.forward_return > 0
        if high_confidence and not correct:
            false_positives += 1
        elif not high_confidence and not correct:
            true_negatives += 1
    denominator = false_positives + true_negatives
    if denominator == 0:
        return _ZERO
    return Decimal(false_positives) / Decimal(denominator)


def _false_discovery_rate_above_median(
    values: tuple[Decimal, ...], decisions: tuple[ReplayDecision, ...]
) -> Decimal:
    if not values:
        return _ZERO
    threshold = _percentile(list(values), Decimal("0.50"))
    selected = tuple(
        decision
        for value, decision in zip(values, decisions, strict=True)
        if value >= threshold and decision.forward_return is not None
    )
    if not selected:
        return _ZERO
    false_positives = sum(1 for decision in selected if (decision.forward_return or _ZERO) <= 0)
    return Decimal(false_positives) / Decimal(len(selected))


def _quality_partitions(decisions: tuple[ReplayDecision, ...]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for decision in decisions:
        missing = set(_missing_features(decision))
        warmup = bool(missing.intersection({"macd", "macd_signal", "macd_histogram"}))
        volume = bool(missing.intersection({"relative_volume", "volume_change", "liquidity_proxy"}))
        if missing == {"spread"}:
            counts["spread_only"] += 1
        elif volume:
            counts["volume_related"] += 1
        elif warmup:
            counts["warmup_related"] += 1
        else:
            counts["multiple_or_other"] += 1
    return dict(sorted(counts.items()))


def _missing_features(decision: ReplayDecision) -> tuple[str, ...]:
    names = decision.confidence_decomposition.get("insufficient_feature_names", ())
    return tuple(str(name) for name in names) if isinstance(names, tuple) else ()


def _required_directional_features() -> frozenset[str]:
    return frozenset(
        {
            "short_term_momentum",
            "medium_term_momentum",
            "long_term_momentum",
            "sma",
            "ema",
            "moving_average_slope",
            "price_vs_moving_average",
            "rsi",
            "atr",
            "realized_volatility",
            "rolling_standard_deviation",
            "drawdown",
            "distance_from_recent_high",
            "distance_from_recent_low",
            "breakout_strength",
            "range_position",
            "mean_reversion_z_score",
            "trend_persistence",
        }
    )


def _input_classification_matrix() -> tuple[dict[str, str], ...]:
    return (
        {
            "component": "strategy confidence",
            "classification": "SIGNAL_RELIABILITY",
            "rationale": "strategy-local estimate of signal strength and evidence quality",
        },
        {
            "component": "agreement ratio",
            "classification": "SIGNAL_RELIABILITY",
            "rationale": "measures consistency among directional strategy engines",
        },
        {
            "component": "disagreement multiplier",
            "classification": "SIGNAL_RELIABILITY",
            "rationale": "penalizes conflicting directional evidence",
        },
        {
            "component": "regime confidence",
            "classification": "BOTH_WITH_JUSTIFICATION",
            "rationale": "regime affects signal interpretation and broad execution/risk context",
        },
        {
            "component": "feature quality",
            "classification": "BOTH_WITH_JUSTIFICATION",
            "rationale": (
                "required feature sufficiency affects signal reliability; "
                "optional gaps affect readiness"
            ),
        },
        {
            "component": "spread",
            "classification": "EXECUTION_READINESS",
            "rationale": (
                "spread is market microstructure and cost realism, not core OHLC direction"
            ),
        },
        {
            "component": "volume",
            "classification": "EXECUTION_READINESS",
            "rationale": (
                "volume supports liquidity and confirmation but is optional for OHLC direction"
            ),
        },
        {
            "component": "relative volume",
            "classification": "EXECUTION_READINESS",
            "rationale": "liquidity and confirmation enrichment",
        },
        {
            "component": "liquidity proxy",
            "classification": "EXECUTION_READINESS",
            "rationale": "execution realism and tradability quality",
        },
        {
            "component": "news/event evidence",
            "classification": "BOTH_WITH_JUSTIFICATION",
            "rationale": "can validate direction and flag event risk when configured",
        },
        {
            "component": "portfolio fit",
            "classification": "NOT_CONFIDENCE_INPUT",
            "rationale": "affects score/fit, not the current confidence formula directly",
        },
        {
            "component": "data-quality score",
            "classification": "BOTH_WITH_JUSTIFICATION",
            "rationale": (
                "currently mixes required signal sufficiency and optional enrichment completeness"
            ),
        },
    )


def _feature_matrix() -> tuple[dict[str, str], ...]:
    return (
        {
            "feature": "spread",
            "category": "EXECUTION_MICROSTRUCTURE_FEATURE",
            "rationale": "needed for cost/liquidity realism, not OHLC directional inference",
        },
        {
            "feature": "relative_volume",
            "category": "OPTIONAL_SIGNAL_ENRICHMENT",
            "rationale": "can confirm breakouts/liquidity but should not be mandatory",
        },
        {
            "feature": "volume_change",
            "category": "OPTIONAL_SIGNAL_ENRICHMENT",
            "rationale": "volume confirmation is useful but provider-dependent",
        },
        {
            "feature": "liquidity_proxy",
            "category": "EXECUTION_MICROSTRUCTURE_FEATURE",
            "rationale": "execution-readiness indicator",
        },
        {
            "feature": "macd",
            "category": "OPTIONAL_SIGNAL_ENRICHMENT",
            "rationale": "useful confirmation but redundant with required trend/momentum set",
        },
        {
            "feature": "macd_signal",
            "category": "OPTIONAL_SIGNAL_ENRICHMENT",
            "rationale": "MACD confirmation component",
        },
        {
            "feature": "macd_histogram",
            "category": "OPTIONAL_SIGNAL_ENRICHMENT",
            "rationale": "MACD confirmation component",
        },
    )


def _confidence_row(decision: ReplayDecision) -> dict[str, Decimal]:
    details = decision.confidence_decomposition
    if not details:
        baseline = decision.confidence
        return {
            "baseline": baseline,
            "unrounded_baseline": baseline,
            "ensemble_current": baseline,
            "remove_regime_multiplier_from_ensemble": baseline,
            "remove_final_regime_term": baseline,
            "remove_feature_quality_multiplier_from_ensemble": baseline,
            "remove_final_data_quality_term": baseline,
        }
    base = Decimal(str(details["mean_base_strategy_confidence"]))
    agreement = Decimal(str(details["agreement_multiplier"]))
    quality = Decimal(str(details["feature_quality_multiplier"]))
    regime_multiplier = Decimal(str(details["regime_multiplier"]))
    disagreement = Decimal(str(details["strong_disagreement_multiplier"]))
    regime_confidence = Decimal(str(details["regime_confidence"]))
    data_quality_component = Decimal(str(details["data_quality_score"])) / Decimal("100")
    ensemble_current = _clamp(base * agreement * quality * regime_multiplier * disagreement)
    ensemble_without_regime = _clamp(base * agreement * quality * disagreement)
    ensemble_without_quality = _clamp(base * agreement * regime_multiplier * disagreement)
    return {
        "baseline": decision.confidence,
        "unrounded_baseline": Decimal(str(details["opportunity_confidence_before_rounding"])),
        "ensemble_current": ensemble_current,
        "remove_regime_multiplier_from_ensemble": _clamp(
            (ensemble_without_regime + regime_confidence + data_quality_component) / Decimal("3")
        ),
        "remove_final_regime_term": _clamp(
            (ensemble_current + data_quality_component) / Decimal("2")
        ),
        "remove_feature_quality_multiplier_from_ensemble": _clamp(
            (ensemble_without_quality + regime_confidence + data_quality_component) / Decimal("3")
        ),
        "remove_final_data_quality_term": _clamp(
            (ensemble_current + regime_confidence) / Decimal("2")
        ),
    }


def _variant_payload(
    name: str,
    values: tuple[Decimal, ...],
    baseline: tuple[Decimal, ...],
    decisions: tuple[ReplayDecision, ...],
) -> dict[str, object]:
    return {
        "variant": name,
        "scope": "RESEARCH_COUNTERFACTUAL_ONLY",
        "distribution": _decimal_distribution(values),
        "standard_deviation": str(_stddev(values).quantize(Decimal("0.0001"))),
        "unique_rounded_values": len({value.quantize(Decimal("0.01")) for value in values}),
        "correlation_with_baseline": str(_pearson(values, baseline).quantize(Decimal("0.0001"))),
        "correlation_with_opportunity_score": str(
            _pearson(values, tuple(decision.score for decision in decisions)).quantize(
                Decimal("0.0001")
            )
        ),
        "spearman_with_forward_return": str(
            _spearman_with_forward_return(values, decisions).quantize(Decimal("0.0001"))
        ),
        "forward_outcome_calibration": _value_bins(values, decisions),
        "asset_class_breakdown": _group_breakdown(values, decisions, "asset_class"),
        "regime_breakdown": _group_breakdown(values, decisions, "regime"),
    }


def _component_marginal_deltas(rows: tuple[dict[str, Decimal], ...]) -> tuple[dict[str, str], ...]:
    labels = (
        ("without_regime_multiplier_inside_ensemble", "remove_regime_multiplier_from_ensemble"),
        ("without_final_regime_term", "remove_final_regime_term"),
        (
            "without_feature_quality_multiplier_inside_ensemble",
            "remove_feature_quality_multiplier_from_ensemble",
        ),
        ("without_final_data_quality_term", "remove_final_data_quality_term"),
        ("ensemble_only", "ensemble_current"),
    )
    return tuple(
        {
            "component": label,
            "mean_counterfactual_delta": str(
                _mean(tuple(row[key] - row["baseline"] for row in rows)).quantize(Decimal("0.0001"))
            ),
            "min_delta": str(min(row[key] - row["baseline"] for row in rows)),
            "max_delta": str(max(row[key] - row["baseline"] for row in rows)),
        }
        for label, key in labels
    )


def _score_confidence_overlap(decisions: tuple[ReplayDecision, ...]) -> dict[str, str]:
    confidences = tuple(decision.confidence for decision in decisions)
    scores = tuple(decision.score for decision in decisions)
    return {
        "pearson_score_confidence": str(_pearson(confidences, scores).quantize(Decimal("0.0001"))),
        "spearman_score_confidence": str(
            _spearman(confidences, scores).quantize(Decimal("0.0001"))
        ),
        "shared_inputs": (
            "strategy ensemble direction/confidence, regime assessment, feature quality, "
            "risk/liquidity features"
        ),
        "interpretation": (
            "High correlation indicates score and confidence may not provide independent gates; "
            "low correlation indicates confidence contributes separate uncertainty information."
        ),
    }


def _rounding_sensitivity(rows: tuple[dict[str, Decimal], ...]) -> dict[str, object]:
    unrounded = tuple(row["unrounded_baseline"] for row in rows)
    rounded = tuple(row["baseline"] for row in rows)
    return {
        "unrounded_distribution": _decimal_distribution(unrounded),
        "rounded_distribution": _decimal_distribution(rounded),
        "unrounded_unique_values": len(set(unrounded)),
        "rounded_2dp_unique_values": len(set(rounded)),
        "ranking_correlation_unrounded_vs_rounded": str(
            _spearman(unrounded, rounded).quantize(Decimal("0.0001"))
        ),
        "production_rounding_changed": False,
    }


def _quality_penalty_semantics(decisions: tuple[ReplayDecision, ...]) -> dict[str, object]:
    quality_states = Counter(
        str(decision.confidence_decomposition.get("feature_quality_state", "UNKNOWN"))
        for decision in decisions
    )
    missing: Counter[str] = Counter()
    for decision in decisions:
        names = decision.confidence_decomposition.get("insufficient_feature_names", ())
        if isinstance(names, tuple):
            missing.update(str(name) for name in names)
    return {
        "quality_states": dict(sorted(quality_states.items())),
        "most_common_missing_or_insufficient_features": dict(missing.most_common(12)),
        "likely_current_limitations": (
            "historical bid/ask spread is usually absent from OHLCV candles",
            "volume may be unavailable for some broker/provider asset classes",
            "longer-lookback indicators are unavailable in early replay windows",
            "historical news/event evidence is not configured",
        ),
        "can_partial_become_good": (
            "Yes for later replay windows with richer OHLCV plus historical spread/liquidity/news "
            "sources; no if the selected provider permanently lacks those inputs."
        ),
    }


def _calibration_methodology() -> tuple[str, ...]:
    return (
        "Split evidence into TRAIN, VALIDATION, and OUT_OF_SAMPLE before selecting thresholds.",
        "Use TRAIN to estimate confidence reliability and candidate threshold bands.",
        "Use VALIDATION to select among predeclared candidates with minimum sample counts.",
        "Freeze the selected threshold before OUT_OF_SAMPLE evaluation.",
        (
            "Require walk-forward stability, drawdown constraints, false-positive controls, "
            "and monotonicity checks."
        ),
        (
            "Record dataset id, calibration version, timestamp, and validation status before "
            "any profile change."
        ),
        "Never select thresholds by maximizing return on the full historical dataset.",
    )


def _value_bins(
    values: tuple[Decimal, ...], decisions: tuple[ReplayDecision, ...]
) -> tuple[dict[str, object], ...]:
    groups: dict[str, list[Decimal]] = defaultdict(list)
    for value, decision in zip(values, decisions, strict=True):
        if decision.forward_return is None:
            continue
        groups[str(value.quantize(Decimal("0.01")))].append(decision.forward_return)
    return tuple(
        {
            "confidence": label,
            "sample_count": len(returns),
            "mean_forward_return": str(_mean(tuple(returns))),
            "median_forward_return": str(_median(tuple(returns))),
            "win_rate": str(
                Decimal(sum(1 for value in returns if value > 0)) / Decimal(len(returns))
            ),
            "mae": str(min(returns)),
            "mfe": str(max(returns)),
        }
        for label, returns in sorted(groups.items())
        if returns
    )


def _group_breakdown(
    values: tuple[Decimal, ...],
    decisions: tuple[ReplayDecision, ...],
    attribute: str,
) -> tuple[dict[str, object], ...]:
    groups: dict[str, list[Decimal]] = defaultdict(list)
    for value, decision in zip(values, decisions, strict=True):
        key = decision.asset_class.value if attribute == "asset_class" else decision.regime.value
        groups[key].append(value)
    return tuple(
        {
            attribute: label,
            "sample_count": len(group_values),
            "distribution": _decimal_distribution(tuple(group_values)),
        }
        for label, group_values in sorted(groups.items())
    )


def _decimal_distribution(values: tuple[Decimal, ...]) -> dict[str, str]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "minimum": str(ordered[0]),
        "p10": str(_percentile(ordered, Decimal("0.10"))),
        "p25": str(_percentile(ordered, Decimal("0.25"))),
        "median": str(_percentile(ordered, Decimal("0.50"))),
        "p75": str(_percentile(ordered, Decimal("0.75"))),
        "p90": str(_percentile(ordered, Decimal("0.90"))),
        "maximum": str(ordered[-1]),
    }


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal:
    index = int((Decimal(len(values) - 1) * percentile).to_integral_value())
    return values[index]


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return _ZERO
    return sum(values, _ZERO) / Decimal(len(values))


def _median(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return _ZERO
    ordered = sorted(values)
    return _percentile(ordered, Decimal("0.50"))


def _stddev(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        return _ZERO
    mean = _mean(values)
    variance = sum(((value - mean) ** 2 for value in values), _ZERO) / Decimal(len(values) - 1)
    return Decimal(str(sqrt(float(variance))))


def _pearson(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        return _ZERO
    left_mean = _mean(left)
    right_mean = _mean(right)
    numerator = sum(
        ((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True)), _ZERO
    )
    left_var = sum(((x - left_mean) ** 2 for x in left), _ZERO)
    right_var = sum(((y - right_mean) ** 2 for y in right), _ZERO)
    denominator = Decimal(str(sqrt(float(left_var * right_var))))
    return _ZERO if denominator == 0 else numerator / denominator


def _spearman_with_forward_return(
    values: tuple[Decimal, ...], decisions: tuple[ReplayDecision, ...]
) -> Decimal:
    pairs = tuple(
        (value, decision.forward_return)
        for value, decision in zip(values, decisions, strict=True)
        if decision.forward_return is not None
    )
    if len(pairs) < 2:
        return _ZERO
    return _spearman(
        tuple(pair[0] for pair in pairs),
        tuple(pair[1] for pair in pairs if pair[1] is not None),
    )


def _spearman(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        return _ZERO
    return _pearson(_ranks(left), _ranks(right))


def _ranks(values: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    ordered = sorted((value, index) for index, value in enumerate(values))
    ranks = [Decimal("0")] * len(values)
    position = 0
    while position < len(ordered):
        end = position
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[position][0]:
            end += 1
        rank = (Decimal(position + 1) + Decimal(end + 1)) / Decimal("2")
        for _, index in ordered[position : end + 1]:
            ranks[index] = rank
        position = end + 1
    return tuple(ranks)


def _clamp(value: Decimal) -> Decimal:
    return max(_ZERO, min(_ONE, value))
