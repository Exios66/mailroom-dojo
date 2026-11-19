"""Calibration, risk-bound and ranking math over ``(confidence, correct)`` pairs.

Ported from llm-dojo-scoring 0.30.1 ``analysis/calibration.py``: an exact one-sided
Clopper-Pearson upper bound for wrong auto-accepts and the sample size that bound
requires, cutoff searches (a target accuracy, a cross-fit, a target precision),
expected calibration error, Brier score and reliability bins, AUROC (ranking power) and
Spearman correlation. :func:`calibration_by_group` is new: the confidence layer's
calibration error and ranking power for each agent and class.

No function reads a run, the environment or a clock, and none invents a missing value:
empty or one-class populations give ``None``. The cross-fit shuffle is seeded from its
``seed`` argument on each call. Same numbers as the old Dojo on the same inputs
(``tests/test_differential_statistics_parity.py``), except that a NaN or infinite
confidence raises in the cutoff searches (``packages/dojo/docs/score-changelog.md``).
"""

from __future__ import annotations

import math
import numbers
import random
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

import numpy
from scipy.special import betaincinv

from ._checks import is_non_finite, validate_confidence, validate_count
from .bootstrap import WilsonInterval, wilson_ci

__all__ = [
    "CrossFitResult",
    "GroupCalibration",
    "Pair",
    "PrecisionCutoff",
    "ReliabilityBin",
    "auroc",
    "brier_score",
    "calibration_by_group",
    "clopper_pearson_upper",
    "cross_fit_cutoffs",
    "cutoff_at_target",
    "cutoff_for_precision",
    "expected_calibration_error",
    "reliability_bins",
    "required_samples",
    "spearman",
]

Pair = tuple[float, bool]

#: Largest accept count :func:`required_samples` searches before giving up.
MAX_REQUIRED_SAMPLES = 1_000_000


@dataclass(frozen=True, slots=True)
class CrossFitResult:
    """A cutoff tuned on all rows, with pooled held-out accuracy from the cross-fit."""

    cutoff: float | None
    holdout_documents: int
    holdout_accepted: int
    holdout_accuracy: float | None
    holdout_accuracy_ci: WilsonInterval | None
    holdout_share_accepted: float | None
    meets_target: bool

    def to_dict(self) -> dict[str, Any]:
        """The old Dojo's dictionary (the interval as its own dictionary)."""
        return {
            "cutoff": self.cutoff,
            "holdout_documents": self.holdout_documents,
            "holdout_accepted": self.holdout_accepted,
            "holdout_accuracy": self.holdout_accuracy,
            "holdout_accuracy_ci": (
                self.holdout_accuracy_ci.to_dict() if self.holdout_accuracy_ci is not None else None
            ),
            "holdout_share_accepted": self.holdout_share_accepted,
            "meets_target": self.meets_target,
        }


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One equal-width confidence bin: count, mean confidence, accuracy and their gap."""

    bin: int
    low: float
    high: float
    n: int
    mean_confidence: float | None
    accuracy: float | None
    gap: float | None

    def to_dict(self) -> dict[str, Any]:
        """The old Dojo's dictionary."""
        return {
            "bin": self.bin,
            "low": self.low,
            "high": self.high,
            "n": self.n,
            "mean_confidence": self.mean_confidence,
            "accuracy": self.accuracy,
            "gap": self.gap,
        }


@dataclass(frozen=True, slots=True)
class PrecisionCutoff:
    """The lowest cutoff keeping a target precision, or ``cutoff=None`` when none does."""

    cutoff: float | None
    precision: float | None
    accept_rate: float | None
    recall: float | None

    def to_dict(self) -> dict[str, Any]:
        """The old Dojo's dictionary."""
        return {
            "cutoff": self.cutoff,
            "precision": self.precision,
            "accept_rate": self.accept_rate,
            "recall": self.recall,
        }


@dataclass(frozen=True, slots=True)
class GroupCalibration:
    """Calibration and ranking power of one group's confidences (an agent, a class, or both)."""

    n: int
    dropped: int
    ece: float | None
    brier: float | None
    auroc: float | None

    def to_dict(self) -> dict[str, Any]:
        """Plain data for reports."""
        return {
            "n": self.n,
            "dropped": self.dropped,
            "ece": self.ece,
            "brier": self.brier,
            "auroc": self.auroc,
        }


def clopper_pearson_upper(errors: int, n: int, *, confidence: float = 0.95) -> float | None:
    """Exact one-sided upper bound on an error rate.

    Solves ``P(X <= errors; n, p) = 1 - confidence`` for ``p``: zero errors in 299
    accepts gives 0.997%, zero in 149 gives 1.99%. Returns ``None`` for ``n=0``.
    """
    errors = validate_count(errors, "errors")
    n = validate_count(n, "n")
    if n == 0:
        return None
    if errors > n:
        raise ValueError(f"errors ({errors}) cannot exceed n ({n})")
    level = validate_confidence(confidence)
    if errors == n:
        return 1.0
    # The exact bound is the ``confidence`` quantile of Beta(errors + 1, n - errors). The
    # regularized incomplete-beta inverse needs no binomial coefficients, so large counts
    # cannot overflow a float (``math.comb(10**6, 67)`` does).
    return float(betaincinv(errors + 1, n - errors, level))


def required_samples(errors: int, *, risk: float = 0.01, confidence: float = 0.95) -> int | None:
    """Smallest number of accepts whose exact upper bound clears ``risk``.

    Zero errors need 299 accepts for a 1% bar at 95% confidence; one error needs 473.
    ``None`` means no sample up to one million accepts clears it.
    """
    errors = validate_count(errors, "errors")
    risk = validate_confidence(risk)
    confidence = validate_confidence(confidence)
    if errors == 0:
        # 1 - (1 - risk) ** n <= 1 - confidence. log1p keeps a tiny risk from rounding
        # ``1 - risk`` to 1.0 (a zero denominator).
        exact = math.log(1.0 - confidence) / math.log1p(-risk)
        # Compare before ``ceil``: a risk near the float minimum overflows to inf.
        if not math.isfinite(exact) or exact > MAX_REQUIRED_SAMPLES:
            return None
        return max(1, math.ceil(exact))
    low, high = max(errors, 1), MAX_REQUIRED_SAMPLES
    if errors > high:
        return None
    if (clopper_pearson_upper(errors, high, confidence=confidence) or 1.0) > risk:
        return None
    while low < high:
        middle = (low + high) // 2
        bound = clopper_pearson_upper(errors, middle, confidence=confidence)
        if bound is not None and bound <= risk:
            high = middle
        else:
            low = middle + 1
    return low


def _require_finite_confidences(rows: Sequence[Any], name: str) -> None:
    """A NaN or infinite confidence has no place in a cutoff search: refuse it, naming the row."""
    for index, row in enumerate(rows):
        if is_non_finite(row[0]):
            raise ValueError(f"{name}[{index}] has a non-finite confidence: {row[0]!r}")


def _accepted(rows: Sequence[Pair], cutoff: float) -> tuple[int, int]:
    """``(correct, accepted)`` at one inclusive confidence cutoff."""
    kept = [correct for confidence, correct in rows if confidence >= cutoff]
    return sum(kept), len(kept)


def cutoff_at_target(rows: Sequence[Pair], target: float) -> float | None:
    """Lowest confidence whose accepted set reaches ``target`` accuracy.

    Walks the rows from highest to lowest confidence once. Returns ``None`` when no
    positive-confidence cutoff reaches the target.
    """
    if not 0.0 < target <= 1.0:
        raise ValueError(f"target must be in (0, 1]; got {target!r}")
    rows = list(rows)  # read a one-shot iterable once: checked, then sorted
    _require_finite_confidences(rows, "rows")
    ordered = sorted(rows, key=lambda pair: -pair[0])
    best: float | None = None
    correct = 0
    for index, (confidence, hit) in enumerate(ordered, 1):
        correct += int(bool(hit))
        if index < len(ordered) and ordered[index][0] == confidence:
            continue
        if confidence > 0 and correct / index >= target:
            best = confidence
    return best


def _wilson_lower(p: float, n: int, z: float) -> float:
    """Unrounded Wilson lower bound (the formula of :func:`wilson_bounds`, no argument checks)."""
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    margin = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denominator
    return max(0.0, centre - margin)


def cross_fit_cutoffs(
    rows: Sequence[Pair],
    *,
    target: float = 0.95,
    folds: int = 5,
    seed: int = 42,
    confidence: float = 0.95,
) -> CrossFitResult:
    """Tune a cutoff on all folds but one and score the held-out one, every row once.

    Reports the pooled held-out accepted count and accuracy with a Wilson interval at
    ``confidence``; a population that never reaches the target gets ``cutoff=None`` and
    no held-out metrics. ``meets_target`` compares the unrounded Wilson lower bound with
    ``target``. The shuffle is ``random.Random(seed)``.
    """
    if folds < 2:
        raise ValueError("folds must be at least 2")
    validate_confidence(confidence)
    shuffled = list(rows)  # read a one-shot iterable once: checked, then shuffled
    _require_finite_confidences(shuffled, "rows")
    random.Random(seed).shuffle(shuffled)
    cutoff = cutoff_at_target(shuffled, target)
    if cutoff is None:
        return CrossFitResult(
            cutoff=None,
            holdout_documents=len(shuffled),
            holdout_accepted=0,
            holdout_accuracy=None,
            holdout_accuracy_ci=None,
            holdout_share_accepted=None,
            meets_target=False,
        )
    right = kept = 0
    for fold in range(folds):
        held = shuffled[fold::folds]
        training = [row for index, row in enumerate(shuffled) if index % folds != fold]
        fold_cutoff = cutoff_at_target(training, target)
        if fold_cutoff is None:
            continue
        fold_right, fold_kept = _accepted(held, fold_cutoff)
        right += fold_right
        kept += fold_kept
    z = NormalDist().inv_cdf(0.5 + validate_confidence(confidence) / 2.0)
    interval = wilson_ci(right / kept, kept, z=z) if kept else None
    return CrossFitResult(
        cutoff=cutoff,
        holdout_documents=len(shuffled),
        holdout_accepted=kept,
        holdout_accuracy=right / kept if kept else None,
        holdout_accuracy_ci=interval,
        holdout_share_accepted=kept / len(shuffled) if shuffled else None,
        # Decide on the exact bound, not the 4-place display value in the interval.
        meets_target=bool(kept and _wilson_lower(right / kept, kept, z) >= target),
    )


def _clean_pairs(pairs: Iterable[Pair]) -> list[Pair]:
    """Keep pairs whose confidence is a finite number in ``[0, 1]`` (not a bool); read correctness as bool."""
    clean: list[Pair] = []
    for confidence, correct in pairs:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        value = float(confidence)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            continue
        clean.append((value, bool(correct)))
    return clean


def expected_calibration_error(pairs: Iterable[Pair], *, bins: int = 10) -> float | None:
    """Equal-width ECE: the count-weighted mean of ``|mean confidence - accuracy|`` per bin.

    Pairs whose confidence is not a finite number in ``[0, 1]`` are left out. ``None``
    when no pair remains.
    """
    if bins < 1:
        raise ValueError("bins must be positive")
    clean = _clean_pairs(pairs)
    if not clean:
        return None
    buckets: list[list[Pair]] = [[] for _ in range(bins)]
    for confidence, correct in clean:
        buckets[min(int(confidence * bins), bins - 1)].append((confidence, correct))
    total = len(clean)
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        mean_confidence = sum(value for value, _ in bucket) / len(bucket)
        accuracy = sum(1 for _, correct in bucket if correct) / len(bucket)
        error += (len(bucket) / total) * abs(mean_confidence - accuracy)
    return round(error, 6)


def brier_score(pairs: Iterable[Pair]) -> float | None:
    """Mean squared difference between confidence and correctness (same pair filter as ECE)."""
    clean = _clean_pairs(pairs)
    if not clean:
        return None
    return round(sum((confidence - float(correct)) ** 2 for confidence, correct in clean) / len(clean), 6)


def reliability_bins(pairs: Iterable[Pair], *, bins: int = 10) -> list[ReliabilityBin]:
    """Every equal-width bin, empty ones included, with count, mean confidence, accuracy and gap."""
    if bins < 1:
        raise ValueError("bins must be positive")
    clean = _clean_pairs(pairs)
    out: list[ReliabilityBin] = []
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        bucket = [
            (confidence, correct)
            for confidence, correct in clean
            if low <= confidence < high or (index == bins - 1 and confidence == 1.0)
        ]
        if not bucket:
            out.append(ReliabilityBin(index, round(low, 6), round(high, 6), 0, None, None, None))
            continue
        mean_confidence = sum(value for value, _ in bucket) / len(bucket)
        accuracy = sum(1 for _, correct in bucket if correct) / len(bucket)
        out.append(
            ReliabilityBin(
                index,
                round(low, 6),
                round(high, 6),
                len(bucket),
                round(mean_confidence, 6),
                round(accuracy, 6),
                round(abs(mean_confidence - accuracy), 6),
            )
        )
    return out


def _is_nan(value: Any) -> bool:
    """NaN test that never converts: ``math.isnan(10**400)`` overflows."""
    return bool(value != value)


def _rank(values: Sequence[Any]) -> list[float]:
    """Average ranks, ties sharing the midpoint."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        rank = (index + end) / 2.0 + 1.0
        for cursor in range(index, end + 1):
            ranks[order[cursor]] = rank
        index = end + 1
    return ranks


def auroc(scores: Sequence[Any], labels: Sequence[Any]) -> float | None:
    """Probability that a positive outranks a negative, ties counting half (ranking power).

    Scores that are not real numbers (booleans included) and NaN scores are left out.
    Returns ``None`` when either class is absent: there is no discrimination to measure.
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    pairs = [
        (score, bool(label))
        for score, label in zip(scores, labels, strict=True)
        # numbers.Real admits numpy scalars (float32, int64); bools are labels, not scores.
        if isinstance(score, numbers.Real)
        and not isinstance(score, bool)
        and type(score).__name__ != "bool_"
        and not _is_nan(score)
    ]
    positives = [score for score, label in pairs if label]
    negatives = [score for score, label in pairs if not label]
    if not positives or not negatives:
        return None
    ranks = _rank([score for score, _ in pairs])
    positive_rank_sum = sum(rank for rank, (_, label) in zip(ranks, pairs, strict=True) if label)
    count = len(positives) * len(negatives)
    return round((positive_rank_sum - len(positives) * (len(positives) + 1) / 2.0) / count, 6)


def spearman(xs: Sequence[Any], ys: Sequence[Any]) -> float | None:
    """Rank correlation; ``None`` under three complete rows or with a constant axis.

    A pair carrying a NaN is left out. Values are ranked as given, so distinct integers
    above ``2**53`` stay distinct.
    """
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have the same length")
    kept = [(x, y) for x, y in zip(xs, ys, strict=True) if not (_is_nan(x) or _is_nan(y))]
    if len(kept) < 3:
        return None
    rx, ry = _rank([x for x, _ in kept]), _rank([y for _, y in kept])
    mean_x, mean_y = sum(rx) / len(rx), sum(ry) / len(ry)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=True))
    variance = math.sqrt(sum((a - mean_x) ** 2 for a in rx) * sum((b - mean_y) ** 2 for b in ry))
    return round(covariance / variance, 6) if variance else None


def cutoff_for_precision(scores: Sequence[Any], labels: Sequence[Any], precision: float) -> PrecisionCutoff:
    """Lowest inclusive cutoff whose accepted set keeps ``precision``.

    Returns the cutoff (unrounded, so no lower-scoring row slips in), the observed
    precision, the accept rate and the recall. When no cutoff reaches the target,
    ``cutoff`` and ``precision`` are ``None`` and the rates are 0.0.
    """
    if not 0.0 < precision <= 1.0:
        raise ValueError(f"precision must be in (0, 1]; got {precision!r}")
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    pairs = list(zip((float(score) for score in scores), (bool(label) for label in labels), strict=True))
    for index, (score, _) in enumerate(pairs):
        if not math.isfinite(score):
            raise ValueError(f"scores[{index}] is not a finite number: {score!r}")
    total_positive = sum(1 for _, label in pairs if label)
    for cutoff in sorted({score for score, _ in pairs}):
        accepted = [label for score, label in pairs if score >= cutoff]
        if accepted and sum(accepted) / len(accepted) >= precision:
            return PrecisionCutoff(
                cutoff=cutoff,
                precision=round(sum(accepted) / len(accepted), 6),
                accept_rate=round(len(accepted) / len(pairs), 6) if pairs else None,
                recall=round(sum(accepted) / total_positive, 6) if total_positive else None,
            )
    return PrecisionCutoff(cutoff=None, precision=None, accept_rate=0.0, recall=0.0)


def _group_confidence(confidence: Any) -> float | None:
    """A usable confidence for :func:`calibration_by_group`: a finite real in ``[0, 1]``, else ``None``."""
    if isinstance(confidence, (bool, numpy.bool_)) or not isinstance(confidence, numbers.Real):
        return None
    if isinstance(confidence, numbers.Integral):  # never float() a huge integer
        return float(confidence) if 0 <= confidence <= 1 else None
    if is_non_finite(confidence):
        return None
    value = float(confidence)
    return value if 0.0 <= value <= 1.0 else None


def calibration_by_group(
    rows: Iterable[tuple[Hashable, Any, Any]], *, bins: int = 10
) -> dict[Hashable, GroupCalibration]:
    """ECE, Brier score and AUROC of ``(group, confidence, correct)`` rows, per group.

    A group is whatever key the caller passes: an agent, a class, or an
    ``(agent, class)`` tuple. Groups keep the order they first appear in. ``correct``
    must be a bool (NumPy's included) or ``None``; anything else raises ``TypeError``
    naming the row. A row whose correctness is ``None``, or whose confidence is not a
    finite real number in ``[0, 1]`` (NumPy scalars included, bools excluded), is left
    out and counted in ``dropped``; ``n`` counts the rows used. ``auroc`` is ``None``
    for a group whose rows are all correct or all wrong. Unlike the ported population
    functions, which keep the old Dojo's lenient reading, this one never reads text or a
    number as correctness.
    """
    if bins < 1:
        raise ValueError("bins must be positive")
    grouped: dict[Hashable, list[Pair]] = {}
    dropped: dict[Hashable, int] = {}
    for index, (group, confidence, correct) in enumerate(rows):
        clean = grouped.setdefault(group, [])
        dropped.setdefault(group, 0)
        if correct is not None and not isinstance(correct, (bool, numpy.bool_)):
            raise TypeError(f"rows[{index}] ({group!r}): correct must be a bool or None; got {correct!r}")
        value = _group_confidence(confidence)
        if correct is None or value is None:
            dropped[group] += 1
        else:
            clean.append((value, bool(correct)))
    out: dict[Hashable, GroupCalibration] = {}
    for group, clean in grouped.items():
        out[group] = GroupCalibration(
            n=len(clean),
            dropped=dropped[group],
            ece=expected_calibration_error(clean, bins=bins),
            brier=brier_score(clean),
            auroc=auroc([confidence for confidence, _ in clean], [correct for _, correct in clean]),
        )
    return out
