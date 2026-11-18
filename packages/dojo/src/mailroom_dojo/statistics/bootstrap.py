"""Bootstrap confidence intervals, small-sample delta testing and Wilson intervals.

Ported from llm-dojo-scoring 0.30.1 ``analysis/bootstrap.py``. Every function is pure
and deterministic: the resampling generator is a ``random.Random`` seeded from the
``seed`` argument on each call, so the same inputs give the same interval, and the
numbers are the old Dojo's (``tests/test_differential_statistics_parity.py``).

What changed (``packages/dojo/docs/score-changelog.md``): a NaN or infinite score, a
rate outside ``[0, 1]`` or given as text, a support that is not a whole number,
``n_boot < 1``, an ``alpha`` outside ``(0, 1)`` and a seed that is not an integer raise
instead of producing a meaningless or text-seeded interval; results are frozen
dataclasses whose ``to_dict()`` is the old dictionary. As in the old Dojo, fewer than two
usable values give ``None`` before the parameters are looked at.
"""

from __future__ import annotations

import decimal
import math
import numbers
import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ._checks import is_non_finite, validate_resampling

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_N_BOOT",
    "DEFAULT_SEED",
    "BootstrapInterval",
    "DeltaInterval",
    "WilsonInterval",
    "bootstrap_ci",
    "delta_significance",
    "round_down",
    "round_up",
    "wilson_bounds",
    "wilson_ci",
]

DEFAULT_N_BOOT = 2000
DEFAULT_ALPHA = 0.05
DEFAULT_SEED = 42


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    """A percentile-bootstrap interval for a mean, bounds rounded to 4 places."""

    lo: float
    hi: float
    half: float
    n: int
    seed: int
    n_boot: int
    method: str = "percentile-bootstrap"

    def to_dict(self) -> dict[str, Any]:
        """The old Dojo's dictionary (``lo``, ``hi``, ``half``, ``n``, ``seed``, ``n_boot``, ``method``)."""
        return {
            "lo": self.lo,
            "hi": self.hi,
            "half": self.half,
            "n": self.n,
            "seed": self.seed,
            "n_boot": self.n_boot,
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class DeltaInterval:
    """A two-sample bootstrap interval for the difference of means, B minus A."""

    delta: float
    ci_lo: float
    ci_hi: float
    significant: bool
    n_a: int
    n_b: int
    seed: int
    n_boot: int
    method: str = "two-sample-percentile-bootstrap"

    def to_dict(self) -> dict[str, Any]:
        """The old Dojo's dictionary."""
        return {
            "delta": self.delta,
            "ci_lo": self.ci_lo,
            "ci_hi": self.ci_hi,
            "significant": self.significant,
            "n_a": self.n_a,
            "n_b": self.n_b,
            "seed": self.seed,
            "n_boot": self.n_boot,
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    """A Wilson score interval, bounds rounded outward to 4 places (``z`` kept as given)."""

    lo: float
    hi: float
    half: float
    n: int
    p: float
    z: float

    def to_dict(self) -> dict[str, Any]:
        """The old Dojo's dictionary (``lo``, ``hi``, ``half``, ``n``, ``p``, ``z``)."""
        return {"lo": self.lo, "hi": self.hi, "half": self.half, "n": self.n, "p": self.p, "z": self.z}


def scores_as_floats(values: Iterable[Any], name: str = "values") -> list[float]:
    """Per-document scores as floats: None and non-numeric values dropped, booleans as 1.0 and 0.0.

    Numbers and numeric text are converted with ``float`` (the old Dojo's rule). A value
    that converts to NaN or an infinity raises ``ValueError`` naming its position: it
    would make every resampled mean NaN or infinite. An integer too large for a float
    raises ``OverflowError``.
    """
    out: list[float] = []
    for index, value in enumerate(values):
        if value is None:
            continue
        if isinstance(value, bool):
            out.append(1.0 if value else 0.0)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            raise ValueError(f"{name}[{index}] is not a finite number: {value!r}")
        out.append(number)
    return out


def percentile_indices(n_boot: int, alpha: float) -> tuple[int, int]:
    """The sorted-resample positions of the lower and upper percentile bounds.

    ``alpha`` is used as the caller gave it, not as a float: the old Dojo computed these
    positions in the alpha's own arithmetic, and a NumPy ``float32(0.05)`` converted to a
    float rounds to other positions at ties (``n_boot=20`` or ``100``).
    """
    lo_index = max(0, int(round((alpha / 2) * n_boot)) - 1)
    hi_index = min(n_boot - 1, int(round((1 - alpha / 2) * n_boot)) - 1)
    return lo_index, hi_index


def resample_means(values: list[float], n_boot: int, rng: random.Random) -> list[float]:
    """``n_boot`` means of same-sized resamples drawn with replacement, in draw order."""
    n = len(values)
    return [sum(values[i] for i in (rng.randrange(n) for _ in range(n))) / n for _ in range(n_boot)]


def bootstrap_ci(
    values: Iterable[Any],
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
) -> BootstrapInterval | None:
    """Percentile-bootstrap ``1 - alpha`` interval for the mean of per-document scores.

    ``half`` is the half-width in score points (0-1 scale). Returns ``None`` when fewer
    than two usable values remain (no interval on a single document), before the
    resampling parameters are checked, as in the old Dojo.
    """
    cleaned = scores_as_floats(values)
    if len(cleaned) < 2:
        return None
    n_boot, _, seed = validate_resampling(n_boot, alpha, seed)
    means = resample_means(cleaned, n_boot, random.Random(seed))
    means.sort()
    lo_index, hi_index = percentile_indices(n_boot, alpha)
    lo = round(means[lo_index], 4)
    hi = round(means[hi_index], 4)
    return BootstrapInterval(
        lo=lo, hi=hi, half=round((hi - lo) / 2, 4), n=len(cleaned), seed=seed, n_boot=n_boot
    )


def delta_significance(
    values_a: Iterable[Any],
    values_b: Iterable[Any],
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
) -> DeltaInterval | None:
    """Two-sample (unpaired) bootstrap on the mean difference, B minus A.

    ``significant`` is True when the ``1 - alpha`` interval, unrounded, excludes zero.
    Returns ``None`` when either side has fewer than two usable values (the delta is
    then unmeasurable, not "insignificant"), before the resampling parameters are
    checked. For two runs over the same documents use
    :func:`mailroom_dojo.statistics.paired.paired_bootstrap_delta`, which pairs by
    document and is the tighter test.
    """
    a = scores_as_floats(values_a, "values_a")
    b = scores_as_floats(values_b, "values_b")
    if len(a) < 2 or len(b) < 2:
        return None
    n_boot, _, seed = validate_resampling(n_boot, alpha, seed)
    rng = random.Random(seed)
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)

    def sample_mean(values: list[float]) -> float:
        """The mean of one same-sized resample."""
        n = len(values)
        return sum(values[rng.randrange(n)] for _ in range(n)) / n

    diffs = sorted(sample_mean(b) - sample_mean(a) for _ in range(n_boot))
    lo_index, hi_index = percentile_indices(n_boot, alpha)
    lo = diffs[lo_index]
    hi = diffs[hi_index]
    return DeltaInterval(
        delta=round(mean_b - mean_a, 4),
        ci_lo=round(lo, 4),
        ci_hi=round(hi, 4),
        significant=lo > 0 or hi < 0,
        n_a=len(a),
        n_b=len(b),
        seed=seed,
        n_boot=n_boot,
    )


def _validate_rate(p: Any) -> float:
    """A rate is a real number (not a bool) in ``[0, 1]``."""
    if isinstance(p, bool) or not isinstance(p, numbers.Real):
        raise TypeError(f"p must be a real number; got {p!r}")
    if is_non_finite(p) or not 0.0 <= float(p) <= 1.0:
        raise ValueError(f"p must be a rate in [0, 1]; got {p!r}")
    return float(p)


def _validate_support(n: Any) -> int | float:
    """A support is a whole number: an integer (not a bool) or an integer-valued float.

    Workbook rows arrive as ``float64``, and the old Dojo computed with ``n = 10.0`` as
    given; such a value is returned as a float so the arithmetic is the old one.
    """
    if isinstance(n, bool):
        raise TypeError(f"n must be an integer; got {n!r}")
    if isinstance(n, decimal.Decimal):  # the old arithmetic raised from float / Decimal
        raise TypeError(f"n must be an int or an integer-valued float, not a Decimal; got {n!r}")
    if isinstance(n, numbers.Integral):
        return int(n)
    if isinstance(n, numbers.Real) and float(n).is_integer():
        return float(n)
    raise TypeError(f"n must be an integer; got {n!r}")


def _validate_z(z: Any) -> float:
    """``z`` is finite and non-negative: a negative one would swap the bounds."""
    if not z >= 0:
        raise ValueError(f"z must be non-negative; got {z!r}")
    if not math.isfinite(z):
        raise ValueError(f"z must be finite; got {z!r}")
    return z


def wilson_bounds(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Exact (unrounded) Wilson score bounds ``(lo, hi)`` for rate ``p`` over ``n >= 1`` trials.

    ``p`` is a real number (a ``Decimal`` is refused, as the old arithmetic refused it);
    ``n`` is an integer or an integer-valued float.
    """
    _validate_z(z)
    p = _validate_rate(p)
    n = _validate_support(n)
    if n < 1:
        raise ValueError(f"n must be at least 1; got {n!r}")
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    margin = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def round_up(value: float, places: int) -> float:
    """``value`` rounded to ``places`` decimals, never below ``value``.

    Float noise just above a decimal (``0.00010000000000000002``) rounds up a whole
    step: conservative for an upper bound compared with a budget.
    """
    rounded = round(value, places)
    if rounded < value:
        rounded = round(rounded + 10.0**-places, places)
    return rounded


def round_down(value: float, places: int) -> float:
    """``value`` rounded to ``places`` decimals, never above ``value``."""
    rounded = round(value, places)
    if rounded > value:
        rounded = round(rounded - 10.0**-places, places)
    return rounded


def wilson_ci(p: float | None, n: int, z: float = 1.96) -> WilsonInterval | None:
    """Wilson score interval for an aggregate rate ``p`` over ``n`` trials.

    Used when only the rate and its support are known, not per-document scores. Returns
    ``None``, as the old Dojo did, when ``n`` is a real number below 1 (``0.5``, ``-inf``
    and ``False`` included) or when ``p`` is None and ``n`` is any real number (``2.5``,
    NaN and ``True`` included); these checks come before any argument check, and a
    non-NaN ``decimal.Decimal`` support takes part in them as in the old Dojo. Otherwise
    ``n`` must be a whole number: an integer or an integer-valued float (``10.0``); a
    fractional, NaN, infinite, bool, ``Decimal`` or non-numeric support is a
    ``TypeError``. ``p`` may
    be a ``decimal.Decimal``, read as the old Dojo read it; a text rate is refused. The
    bounds are rounded outward (``lo`` down, ``hi`` up) to 4 places, so the reported
    interval always contains the exact one and a bound compared with a budget never
    passes where the exact bound fails. Gates that need the exact value use
    :func:`wilson_bounds`.
    """
    comparable = isinstance(n, numbers.Real) or (isinstance(n, decimal.Decimal) and not n.is_nan())
    if comparable and (n < 1 or p is None):  # the old order: n < 1, then p
        return None
    support = _validate_support(n)  # a non-number: refused here, with or without a rate
    rate = _validate_rate(float(p) if isinstance(p, decimal.Decimal) else p)
    exact_lo, exact_hi = wilson_bounds(rate, support, z)
    lo, hi = round_down(exact_lo, 4), round_up(exact_hi, 4)
    return WilsonInterval(lo=lo, hi=hi, half=round((hi - lo) / 2, 4), n=int(support), p=round(rate, 4), z=z)
