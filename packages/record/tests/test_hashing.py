"""Canonical JSON and SHA-256 helpers."""

from __future__ import annotations

import json

import pytest

from mailroom_record.hashing import canonical_json, hash_obj, sha256_hex


def test_key_order_does_not_change_hash() -> None:
    assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})


def test_known_vector() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert canonical_json({"b": [1, "é"], "a": None}) == '{"a":null,"b":[1,"é"]}'.encode()


def test_nan_is_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_infinity_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json([value])


@pytest.mark.parametrize("value", [0.1, 1e16, -0.0, 1.5e-7, 123456789.123])
def test_floats_render_as_repr(value: float) -> None:
    assert canonical_json(value) == repr(value).encode()


def test_nested_keys_are_sorted() -> None:
    assert canonical_json({"z": {"y": 1, "x": 2}, "a": []}) == b'{"a":[],"z":{"x":2,"y":1}}'


@pytest.mark.parametrize("bad", [{1: "a"}, {"ok": {None: 1}}, [{("t",): 1}], {True: 1}])
def test_non_str_keys_are_rejected(bad: object) -> None:
    # json.dumps would coerce 1 -> "1", so {1: x} and {"1": x} would hash alike.
    with pytest.raises(TypeError):
        canonical_json(bad)


def test_unserialisable_value_is_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_json({"x": object()})


def test_hash_obj_is_sha256_of_canonical_json() -> None:
    obj = {"b": [1, 2.5, "é"], "a": {"c": None}}
    assert hash_obj(obj) == sha256_hex(canonical_json(obj))
    assert len(hash_obj(obj)) == 64 and hash_obj(obj) == hash_obj(obj).lower()


def test_output_round_trips_through_json() -> None:
    obj = {"text": "line\nbreak   \"quoted\" \\", "n": -3, "f": 0.30000000000000004}
    assert json.loads(canonical_json(obj).decode("utf-8")) == obj


def test_cyclic_object_raises_instead_of_hanging():
    a: list = []
    a.append(a)
    with pytest.raises(ValueError):
        canonical_json(a)
