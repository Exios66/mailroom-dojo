"""Statistics for scoring runs: bootstrap and Wilson intervals, calibration and ranking.

Pure, deterministic functions with explicit seeds; no global state, environment,
clock or network. ``bootstrap`` and ``calibration`` are ported from llm-dojo-scoring
0.30.1 with the same numbers.
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
    PrecisionCutoff,
    ReliabilityBin,
    auroc,
    brier_score,
    clopper_pearson_upper,
    cross_fit_cutoffs,
    cutoff_at_target,
    cutoff_for_precision,
    expected_calibration_error,
    reliability_bins,
    required_samples,
    spearman,
)

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_N_BOOT",
    "DEFAULT_SEED",
    "BootstrapInterval",
    "CrossFitResult",
    "DeltaInterval",
    "PrecisionCutoff",
    "ReliabilityBin",
    "WilsonInterval",
    "auroc",
    "bootstrap_ci",
    "brier_score",
    "clopper_pearson_upper",
    "cross_fit_cutoffs",
    "cutoff_at_target",
    "cutoff_for_precision",
    "delta_significance",
    "expected_calibration_error",
    "reliability_bins",
    "required_samples",
    "round_down",
    "round_up",
    "spearman",
    "wilson_bounds",
    "wilson_ci",
]
