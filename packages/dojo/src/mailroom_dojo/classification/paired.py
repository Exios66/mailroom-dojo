"""The cardinality rule for metrics over paired expected and predicted sequences."""

from __future__ import annotations

from collections.abc import Sized

__all__ = ["require_equal_lengths"]


def require_equal_lengths(expected: Sized, predicted: Sized) -> None:
    """Raise ``ValueError`` unless both sequences have the same length.

    The check runs before any row is normalized or dropped, so a missing or extra
    document can never be hidden by an empty or failed row. The message names both
    lengths: ``expected length 2 != predicted length 1``.
    """
    if len(expected) != len(predicted):
        raise ValueError(f"expected length {len(expected)} != predicted length {len(predicted)}")
