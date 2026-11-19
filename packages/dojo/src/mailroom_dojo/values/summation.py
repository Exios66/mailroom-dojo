"""One float sum for every supported Python, so a score never depends on the interpreter.

CPython 3.12 made ``sum()`` of floats compensated (Neumaier's improved Kahan-Babuska
summation); 3.11 adds them naively. A mean rounded to four places can then differ in its last
digit between the two (the parity corpus, recorded on 3.12 or later, holds such a case). The
Dojo's float means go through ``compensated_sum``: on 3.12 and 3.13 it is the builtin, and on
every other version it is a port of the 3.12 loop, so every version gives the 3.12 answer.
CPython 3.14 changed ``sum()`` again: it also compensates integer addends in the float phase,
so ``sum([1e16, 1, 1, 1, 1])`` is ``1.0000000000000004e16`` there and ``1e16`` on 3.12.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable
from typing import Any

_C_LONG_MIN, _C_LONG_MAX = -(2**63), 2**63 - 1


def _neumaier_sum(values: Iterable[Any]) -> Any:
    """CPython 3.12's ``sum(values)`` with start ``0``, in Python (``Python/bltinmodule.c``)."""
    items = iter(values)
    result: Any = 0
    # Integer phase, while the running sum fits a C long; the first item that does not fit,
    # or is not an exact int or bool, is added generically.
    for item in items:
        if (
            (type(item) is int or type(item) is bool)
            and _C_LONG_MIN <= item <= _C_LONG_MAX
            and _C_LONG_MIN <= result + item <= _C_LONG_MAX
        ):
            result += item
            continue
        result = result + item
        break
    else:
        return result
    if type(result) is not float:
        for item in items:
            result = result + item
        return result
    total, compensation = result, 0.0
    for item in items:
        if type(item) is float:
            t = total + item
            if abs(total) >= abs(item):
                compensation += (total - t) + item
            else:
                compensation += (item - t) + total
            total = t
            continue
        if isinstance(item, int) and _C_LONG_MIN <= item <= _C_LONG_MAX:
            total += float(item)
            continue
        # Anything else: settle the compensation, then add generically as 3.12 does.
        if compensation and math.isfinite(compensation):
            total += compensation
        result = total + item
        for rest in items:
            result = result + rest
        return result
    # Keeps a negative zero's sign, and never turns an infinite or overflowed sum into NaN.
    if compensation and math.isfinite(compensation):
        total += compensation
    return total


def compensated_sum(values: Iterable[Any]) -> Any:
    """``sum(values)`` as CPython 3.12 computes it, on every supported Python."""
    if (3, 12) <= sys.version_info < (3, 14):
        return sum(values)
    return _neumaier_sum(values)
