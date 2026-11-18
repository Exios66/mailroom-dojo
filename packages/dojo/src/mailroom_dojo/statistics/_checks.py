"""Argument checks shared by the statistics modules: counts, probabilities and finite numbers."""

from __future__ import annotations

import math
import numbers
import operator
from typing import Any


def validate_count(value: Any, name: str, *, upper: int | None = None) -> int:
    """Require a non-negative integer (a bool is not one), optionally at most ``upper``."""
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError(f"{name} must be an integer")
    value = int(value)  # NumPy integral counts
    if value < 0 or (upper is not None and value > upper):
        raise ValueError(f"{name} must be between 0 and {upper if upper is not None else 'infinity'}")
    return value


def validate_confidence(confidence: Any, name: str = "confidence") -> float:
    """Require a probability strictly inside ``(0, 1)``."""
    value = float(confidence)
    if not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be in (0, 1); got {confidence!r}")
    return value


def validate_resampling(n_boot: Any, alpha: Any, seed: Any) -> tuple[int, int]:
    """A bootstrap needs at least one resample, an ``alpha`` inside ``(0, 1)`` and an integer seed.

    Returns ``(n_boot, seed)`` as plain ints (``operator.index``), so a NumPy integer
    seeds ``random.Random`` and its result serialises to JSON.
    """
    if isinstance(n_boot, bool) or not isinstance(n_boot, numbers.Integral):
        raise TypeError("n_boot must be an integer")
    count = operator.index(n_boot)
    if count < 1:
        raise ValueError(f"n_boot must be at least 1; got {n_boot!r}")
    if isinstance(alpha, bool) or not isinstance(alpha, numbers.Real) or not 0.0 < float(alpha) < 1.0:
        raise ValueError(f"alpha must be in (0, 1); got {alpha!r}")
    if isinstance(seed, bool) or not isinstance(seed, numbers.Integral):
        raise TypeError("seed must be an integer")
    return count, operator.index(seed)


def is_non_finite(value: Any) -> bool:
    """True for a NaN or an infinity (integers are always finite, however large)."""
    if isinstance(value, numbers.Integral):
        return False
    return isinstance(value, numbers.Real) and not math.isfinite(value)
