"""Reading raw field values: stringified JSON containers and empty values.

Label rows and model outputs carry lists and objects either as Python values or as
JSON text (``'["Bylaws"]'``, ``"[]"``). These helpers give every scorer one rule for
what such a value holds and for when a value counts as absent.
"""

from __future__ import annotations

import json
import math
from typing import Any

__all__ = ["NULL_TOKENS", "is_empty_value", "parse_json_container"]

#: Text that means "no value" when it is the whole of a string, compared case-blind.
#: ``nan`` is how a table export writes a missing cell; it is never a real value.
NULL_TOKENS: frozenset[str] = frozenset({"null", "none", "n/a", "n.a.", "nan"})


def parse_json_container(value: Any) -> Any:
    """Return the list or dict a JSON string holds, or ``value`` unchanged.

    Only a string whose stripped text starts with ``[`` or ``{`` and ends with ``]``
    or ``}`` is parsed, and only a list or dict result is returned: ``"[]"`` gives
    ``[]``, ``'["Bylaws"]'`` gives ``["Bylaws"]``. Bare strings, JSON scalars
    (``"41"``, ``"true"``), malformed JSON and every non-string value come back as
    they were, so a numeric string stays a string for the field scorers.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{" or stripped[-1] not in "]}":
        return value
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return value
    return parsed if isinstance(parsed, (list, dict)) else value


def is_empty_value(value: Any) -> bool:
    """Return True when ``value`` carries no expected or predicted value.

    Empty means: ``None``; a blank string; a string that is one of
    :data:`NULL_TOKENS` (case-blind, surrounding spaces ignored); a float NaN; an
    empty list, dict, tuple or set; or a string holding an empty JSON list or object
    (``"[]"``, ``"{}"``, and ``'["  "]'`` is not empty, it holds one item). Numbers
    and booleans, including ``0`` and ``False``, are values.
    """
    if isinstance(value, str):
        parsed = parse_json_container(value)
        if parsed is not value:
            return is_empty_value(parsed)
        stripped = value.strip()
        return not stripped or stripped.lower() in NULL_TOKENS
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, (list, dict, tuple, set)) and len(value) == 0:
        return True
    return False
