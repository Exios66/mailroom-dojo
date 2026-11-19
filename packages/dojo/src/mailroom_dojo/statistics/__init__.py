"""Statistics for scoring runs: bootstrap and Wilson intervals, calibration, ranking and paired tests.

Pure, deterministic functions with explicit seeds; no global state, environment,
clock or network. ``bootstrap`` and ``calibration`` are ported from llm-dojo-scoring
0.30.1 with the same numbers; ``paired`` is new (the design's "Comparing runs").
"""

from __future__ import annotations

from .bootstrap import (
    DEFAULT_ALPHA,
    DEFAULT_N_BOOT,
    DEFAULT_SEED,
    BootstrapInterval,
    DeltaInterval,
    WilsonInterval,
    bootstrap_ci,
    delta_significance,
    round_down,
    round_up,
    wilson_bounds,
    wilson_ci,
)
from .calibration import (
    CrossFitResult,
    GroupCalibration,
    PrecisionCutoff,
    ReliabilityBin,
    auroc,
    brier_score,
    calibration_by_group,
    clopper_pearson_upper,
    cross_fit_cutoffs,
    cutoff_at_target,
    cutoff_for_precision,
    expected_calibration_error,
    reliability_bins,
    required_samples,
    spearman,
)
from .paired import FlipTest, PairedDelta, outcome_flip_test, paired_bootstrap_delta

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_N_BOOT",
    "DEFAULT_SEED",
    "BootstrapInterval",
    "CrossFitResult",
    "DeltaInterval",
    "FlipTest",
    "GroupCalibration",
    "PairedDelta",
    "PrecisionCutoff",
    "ReliabilityBin",
    "WilsonInterval",
    "auroc",
    "bootstrap_ci",
    "brier_score",
    "calibration_by_group",
    "clopper_pearson_upper",
    "cross_fit_cutoffs",
    "cutoff_at_target",
    "cutoff_for_precision",
    "delta_significance",
    "expected_calibration_error",
    "outcome_flip_test",
    "paired_bootstrap_delta",
    "reliability_bins",
    "required_samples",
    "round_down",
    "round_up",
    "spearman",
    "wilson_bounds",
    "wilson_ci",
]
