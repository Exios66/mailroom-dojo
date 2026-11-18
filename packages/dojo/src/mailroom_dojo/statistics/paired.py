"""Paired comparisons of two runs over the same documents (new; the old Dojo had none).

The design's "Comparing runs" rule: two runs that share a draw are compared document by
document, with a bootstrap interval for the score difference and a paired test for
outcome flips. Pairing removes the between-document spread that an unpaired test
(:func:`mailroom_dojo.statistics.bootstrap.delta_significance`) has to carry, so the
same gain is detected with fewer documents.

- :func:`paired_bootstrap_delta`: percentile bootstrap of the mean per-document
  difference, B minus A, resampling documents (``random.Random(seed)``, the same
  resampler as :func:`~mailroom_dojo.statistics.bootstrap.bootstrap_ci`).
- :func:`outcome_flip_test`: exact two-sided McNemar test on the documents whose
  outcome differs between the runs.

Both take two ``{document id: value}`` mappings and pair them by id, never by position.
A document missing from one run, or ``None`` there, cannot be paired: by default that is
an error naming the ids (two runs that share a draw score the same documents); with
``allow_unpaired=True`` such documents are left out and returned in ``unpaired_ids``.
Documents are taken in sorted id order, so the result does not depend on the order of
the mappings.
"""

from __future__ import annotations

import math
import numbers
import random
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy
from scipy.special import bdtr

from ._checks import validate_resampling
from .bootstrap import DEFAULT_ALPHA, DEFAULT_N_BOOT, DEFAULT_SEED, percentile_indices, resample_means

__all__ = ["FlipTest", "PairedDelta", "outcome_flip_test", "paired_bootstrap_delta"]


@dataclass(frozen=True, slots=True)
class PairedDelta:
    """A paired bootstrap interval for the mean per-document difference, B minus A."""

    delta: float
    ci_lo: float
    ci_hi: float
    significant: bool
    n_pairs: int
    dropped_pairs: int
    unpaired_ids: tuple[Hashable, ...]
    seed: int
    n_boot: int
    alpha: float
    method: str = "paired-percentile-bootstrap"

    def to_dict(self) -> dict[str, Any]:
        """Plain data for reports."""
        return {
            "delta": self.delta,
            "ci_lo": self.ci_lo,
            "ci_hi": self.ci_hi,
            "significant": self.significant,
            "n_pairs": self.n_pairs,
            "dropped_pairs": self.dropped_pairs,
            "unpaired_ids": list(self.unpaired_ids),
            "seed": self.seed,
            "n_boot": self.n_boot,
            "alpha": self.alpha,
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class FlipTest:
    """Exact McNemar test of outcome flips between two runs over the same documents.

    ``a_only``: right in A, wrong in B (B lost them). ``b_only``: wrong in A, right in B
    (B gained them). ``net`` is ``b_only - a_only``.
    """

    n_pairs: int
    dropped_pairs: int
    unpaired_ids: tuple[Hashable, ...]
    both_right: int
    both_wrong: int
    a_only: int
    b_only: int
    net: int
    p_value: float
    significant: bool
    alpha: float
    method: str = "exact-mcnemar"

    def to_dict(self) -> dict[str, Any]:
        """Plain data for reports."""
        return {
            "n_pairs": self.n_pairs,
            "dropped_pairs": self.dropped_pairs,
            "unpaired_ids": list(self.unpaired_ids),
            "both_right": self.both_right,
            "both_wrong": self.both_wrong,
            "a_only": self.a_only,
            "b_only": self.b_only,
            "net": self.net,
            "p_value": self.p_value,
            "significant": self.significant,
            "alpha": self.alpha,
            "method": self.method,
        }


#: How many unpaired ids an error message names.
_NAMED_IDS = 5


def _pair_by_document(
    first: Mapping[Hashable, Any], second: Mapping[Hashable, Any], names: str, *, allow_unpaired: bool
) -> tuple[list[tuple[Hashable, Any, Any]], tuple[Hashable, ...]]:
    """Pair two ``{document id: value}`` mappings by id, in sorted id order.

    Returns the ``(id, first value, second value)`` triples of documents scored (not
    ``None``) in both, and the sorted ids of the others. Unpaired documents raise
    ``ValueError`` unless ``allow_unpaired``.
    """
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        raise TypeError(f"{names} must be mappings from document id to value")
    try:
        ordered = sorted(first.keys() | second.keys())
    except TypeError:
        kinds = ", ".join(sorted({type(key).__name__ for key in (*first, *second)}))
        raise TypeError(f"document ids must be mutually orderable; got ids of types {kinds}") from None
    paired: list[tuple[Hashable, Any, Any]] = []
    unpaired: list[Hashable] = []
    for document in ordered:
        a, b = first.get(document), second.get(document)
        if a is None or b is None:
            unpaired.append(document)
        else:
            paired.append((document, a, b))
    if unpaired and not allow_unpaired:
        named = repr(unpaired[:_NAMED_IDS])
        more = f" and {len(unpaired) - _NAMED_IDS} more" if len(unpaired) > _NAMED_IDS else ""
        raise ValueError(
            f"{len(unpaired)} documents are not scored in both runs: {named}{more}; "
            "pass allow_unpaired=True to leave them out"
        )
    return paired, tuple(unpaired)


def _score(value: Any, name: str, document: Hashable) -> float:
    """One per-document score: a bool or a finite real number."""
    if isinstance(value, (bool, numpy.bool_)):
        return 1.0 if value else 0.0
    if not isinstance(value, numbers.Real):
        raise TypeError(f"{name}[{document!r}] is not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}[{document!r}] is not a finite number: {value!r}")
    return number


def paired_bootstrap_delta(
    values_a: Mapping[Hashable, Any],
    values_b: Mapping[Hashable, Any],
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
    allow_unpaired: bool = False,
) -> PairedDelta | None:
    """Paired percentile bootstrap of the mean per-document difference, B minus A.

    ``values_a[doc]`` and ``values_b[doc]`` are document ``doc``'s scores in runs A and
    B; each must be a bool or a finite real number. A document missing or ``None`` in
    either run raises ``ValueError`` unless ``allow_unpaired``, which leaves it out and
    names it in ``unpaired_ids``. The differences are resampled in sorted id order.
    ``significant`` is True when the unrounded ``1 - alpha`` interval excludes zero.
    Returns ``None`` when fewer than two pairs remain.
    """
    n_boot, seed = validate_resampling(n_boot, alpha, seed)
    paired, unpaired = _pair_by_document(
        values_a, values_b, "values_a and values_b", allow_unpaired=allow_unpaired
    )
    differences = [
        _score(b, "values_b", document) - _score(a, "values_a", document) for document, a, b in paired
    ]
    if len(differences) < 2:
        return None
    means = resample_means(differences, n_boot, random.Random(seed))
    means.sort()
    lo_index, hi_index = percentile_indices(n_boot, alpha)
    lo, hi = means[lo_index], means[hi_index]
    return PairedDelta(
        delta=round(sum(differences) / len(differences), 4),
        ci_lo=round(lo, 4),
        ci_hi=round(hi, 4),
        significant=lo > 0 or hi < 0,
        n_pairs=len(differences),
        dropped_pairs=len(unpaired),
        unpaired_ids=unpaired,
        seed=seed,
        n_boot=n_boot,
        alpha=alpha,
    )


def _outcome(value: Any, name: str, document: Hashable) -> bool:
    """One document's outcome: a bool (numpy's included); no truthy text or numbers."""
    if not isinstance(value, (bool, numpy.bool_)):
        raise TypeError(f"{name}[{document!r}] must be a bool or None; got {value!r}")
    return bool(value)


def outcome_flip_test(
    outcomes_a: Mapping[Hashable, Any],
    outcomes_b: Mapping[Hashable, Any],
    *,
    alpha: float = DEFAULT_ALPHA,
    allow_unpaired: bool = False,
) -> FlipTest | None:
    """Exact two-sided McNemar test: did B flip more outcomes one way than the other?

    ``outcomes_a[doc]`` and ``outcomes_b[doc]`` say whether document ``doc``'s outcome
    was right in runs A and B (bools only). Under the null hypothesis a flip is equally
    likely either way, so the smaller flip count is binomial with ``p = 1/2`` over all
    flips; the p-value is twice its lower tail, capped at 1 (1.0 when nothing flipped),
    and ``significant`` means ``p < alpha``. A document missing or ``None`` in either
    run raises ``ValueError`` unless ``allow_unpaired``, which leaves it out and names
    it in ``unpaired_ids``. Returns ``None`` when no pair remains.
    """
    if isinstance(alpha, bool) or not isinstance(alpha, numbers.Real) or not 0.0 < float(alpha) < 1.0:
        raise ValueError(f"alpha must be in (0, 1); got {alpha!r}")
    paired, unpaired = _pair_by_document(
        outcomes_a, outcomes_b, "outcomes_a and outcomes_b", allow_unpaired=allow_unpaired
    )
    counts = {(True, True): 0, (False, False): 0, (True, False): 0, (False, True): 0}
    for document, a, b in paired:
        counts[(_outcome(a, "outcomes_a", document), _outcome(b, "outcomes_b", document))] += 1
    n_pairs = sum(counts.values())
    if n_pairs == 0:
        return None
    a_only, b_only = counts[(True, False)], counts[(False, True)]
    flips = a_only + b_only
    p_value = 1.0 if flips == 0 else min(1.0, 2.0 * float(bdtr(min(a_only, b_only), flips, 0.5)))
    return FlipTest(
        n_pairs=n_pairs,
        dropped_pairs=len(unpaired),
        unpaired_ids=unpaired,
        both_right=counts[(True, True)],
        both_wrong=counts[(False, False)],
        a_only=a_only,
        b_only=b_only,
        net=b_only - a_only,
        p_value=p_value,
        significant=p_value < alpha,
        alpha=alpha,
    )
