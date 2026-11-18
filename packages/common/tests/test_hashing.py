from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Self

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailroom_common.hashing import canonical_json, hash_obj, sha256_file, sha256_hex

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(), children, max_size=5),
    max_leaves=30,
)


def test_known_encoding_is_sorted_compact_and_unescaped() -> None:
    assert canonical_json({"b": 1, "a": [1, 2], "é": "✓"}) == '{"a":[1,2],"b":1,"é":"✓"}'.encode()


def test_known_digest() -> None:
    assert hash_obj({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_key_order_does_not_change_the_hash() -> None:
    assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, {"x": [math.nan]}])
def test_nan_and_infinity_are_refused(value: Any) -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json(value)


@pytest.mark.parametrize("value", [{1: "a"}, {"ok": {None: 1}}, [{"x": {(1, 2): 3}}], {"a": [{2.5: 1}]}])
def test_non_string_keys_are_refused(value: Any) -> None:
    with pytest.raises(TypeError):
        canonical_json(value)


def test_int_key_does_not_collide_with_string_key() -> None:
    assert canonical_json({"1": "a"}) == b'{"1":"a"}'
    with pytest.raises(TypeError, match="keys must be str, got int"):
        canonical_json({1: "a"})


def test_cycles_raise() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="Circular reference"):
        canonical_json(cyclic)
    loop: dict[str, Any] = {}
    loop["self"] = loop
    with pytest.raises(ValueError, match="Circular reference"):
        hash_obj(loop)


def test_unserialisable_object_raises_type_error() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        canonical_json({"a": object()})


def test_sha256_file_matches_in_memory_hash_across_chunk_boundaries(tmp_path: Path) -> None:
    data = bytes(range(256)) * 1000 + b"tail"
    path = tmp_path / "blob.bin"
    path.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    for chunk_size in (1, 7, 256, 4096, len(data), len(data) + 1):
        assert sha256_file(path, chunk_size=chunk_size) == expected
    assert sha256_file(str(path)) == expected


def test_sha256_file_of_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert sha256_file(path) == sha256_hex(b"")


def test_sha256_file_reads_in_bounded_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x" * 10_000)
    sizes: list[int] = []
    real_open = open

    class Spy:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

        def read(self, size: int) -> bytes:
            sizes.append(size)
            return bytes(self._handle.read(size))

    monkeypatch.setattr("builtins.open", lambda p, mode: Spy(real_open(p, mode)))
    sha256_file(path, chunk_size=1000)
    assert sizes and set(sizes) == {1000}
    assert len(sizes) == 11


def test_sha256_file_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "missing")
    with pytest.raises(IsADirectoryError):
        sha256_file(tmp_path)
    (tmp_path / "f").write_bytes(b"x")
    with pytest.raises(ValueError, match="chunk_size must be at least 1, got 0"):
        sha256_file(tmp_path / "f", chunk_size=0)


@given(json_values)
def test_hash_is_sha256_of_canonical_bytes(value: Any) -> None:
    assert hash_obj(value) == hashlib.sha256(canonical_json(value)).hexdigest()
