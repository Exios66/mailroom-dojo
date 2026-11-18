"""Reading money amounts from text."""

from __future__ import annotations

import math

__all__ = ["parse_money"]

_CURRENCY_WORDS_AFTER = (" USD", " DOLLARS", " DOLLAR", " EUROS", " EUR")
_CURRENCY_WORDS_BEFORE = ("USD ", "US ", "EUR ")
_MAGNITUDE_SUFFIXES = (("M", 1e6), ("K", 1e3), ("B", 1e9))


def parse_money(text: object) -> float | None:
    """Return the amount ``text`` states as a float, or None when it states none.

    An int or float (and a bool, which is an int) is returned as a float. A string is
    uppercased; commas and the symbols ``$``, ``€`` and ``£`` are removed; one
    trailing currency word (``USD``, ``DOLLARS``, ``DOLLAR``, ``EUROS``, ``EUR``) and
    then one leading one (``USD``, ``US``, ``EUR``) are removed; a trailing ``M``,
    ``K`` or ``B`` multiplies by a million, a thousand or a billion. What is left must
    parse as a Python float (so ``"nan"`` and ``"inf"`` do). Any other type gives None.
    """
    if isinstance(text, (int, float)):
        try:
            return float(text)
        except (
            OverflowError
        ):  # an int too large for a float (old Dojo: crash); signed infinity, like float("1e999")
            return math.inf if text > 0 else -math.inf
    if not isinstance(text, str):
        return None
    cleaned = text.strip().upper().replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    for tail in _CURRENCY_WORDS_AFTER:
        if cleaned.endswith(tail):
            cleaned = cleaned[: -len(tail)].rstrip()
            break
    for head in _CURRENCY_WORDS_BEFORE:
        if cleaned.startswith(head):
            cleaned = cleaned[len(head) :].lstrip()
            break
    multiplier = 1.0
    for suffix, factor in _MAGNITUDE_SUFFIXES:
        if cleaned.endswith(suffix):
            multiplier = factor
            cleaned = cleaned[:-1].rstrip()
            break
    if not cleaned:
        return None
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None
