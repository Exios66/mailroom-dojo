"""mailroom_common.hashing and mailroom_record.hashing must produce identical bytes.

Both packages are installed in the workspace, so this imports both unconditionally: a
missing package is a failure here, never a skip. Any divergence (a value one side accepts
and the other refuses, or different bytes) fails.
"""

from __future__ import annotations

import enum
import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import mailroom_common.hashing as common
import mailroom_record.hashing as record

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(), children, max_size=5),
    max_leaves=30,
)


class _Key(enum.StrEnum):
    A = "a"


FIXED_VECTORS: list[Any] = [
    None,
    True,
    0,
    -1,
    2**70,
    10**100,
    1.5,
    -0.0,
    1e-300,
    "",
    "plain",
    "naïve ✓ 文字 \U0001f600",
    "line\nbreak\ttab\r  \x00",
    "  ",
    "\ud800",
    [],
    {},
    [1, "a", None, [2, {"b": 3}]],
    {"b": 1, "a": 2, "A": 3, "é": 4, "": 5},
    {"nested": {"z": [1, 2, {"y": None}], "a": {"k": "v"}}},
    (1, 2, 3),
    ((1, (2,)),),
    {_Key.A: 1},
    math.nan,
    math.inf,
    {1: "a"},
    {True: 1},
    {None: 1},
    {1.5: 1},
    {"a": object()},
]


def _outcome(fn: Any, value: Any) -> tuple[str, Any]:
    try:
        return ("ok", fn(value))
    except Exception as exc:  # noqa: BLE001  (comparing which error each side raises)
        return ("error", type(exc))


@pytest.mark.parametrize("value", FIXED_VECTORS)
def test_parity_on_fixed_vectors(value: Any) -> None:
    assert _outcome(common.canonical_json, value) == _outcome(record.canonical_json, value)
    assert _outcome(common.hash_obj, value) == _outcome(record.hash_obj, value)


def test_fixed_vectors_include_both_accepted_and_refused_values() -> None:
    outcomes = {_outcome(common.canonical_json, value)[0] for value in FIXED_VECTORS}
    assert outcomes == {"ok", "error"}


@given(json_values)
def test_parity_on_generated_values(value: Any) -> None:
    assert common.canonical_json(value) == record.canonical_json(value)
    assert common.hash_obj(value) == record.hash_obj(value)


@given(st.binary())
def test_parity_of_sha256_hex(data: bytes) -> None:
    assert common.sha256_hex(data) == record.sha256_hex(data)
