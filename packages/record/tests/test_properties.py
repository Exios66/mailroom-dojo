"""Property-based tests for canonical JSON, hashing and the blob store (hypothesis).

Values are synthetic: hypothesis generates them, no document text is involved.
"""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailroom_record.blobs import BlobMissing, BlobStore, BlobTombstoned
from mailroom_record.hashing import canonical_json, hash_obj, sha256_hex

# JSON-safe values: what json.loads can give back (lists, str keys, finite floats).
scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text()
json_values = st.recursive(
    scalars,
    lambda inner: st.lists(inner, max_size=5) | st.dictionaries(st.text(max_size=8), inner, max_size=5),
    max_leaves=25,
)


def _shuffled(value: Any, rng: random.Random) -> Any:
    """The same value with every dict rebuilt in a random key insertion order."""
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        return {k: _shuffled(value[k], rng) for k in keys}
    if isinstance(value, list):
        return [_shuffled(v, rng) for v in value]
    return value


@given(json_values, st.randoms(use_true_random=False))
def test_canonical_json_ignores_key_order(value: Any, rng: random.Random) -> None:
    other = _shuffled(value, rng)
    assert canonical_json(other) == canonical_json(value)
    assert hash_obj(other) == hash_obj(value)


@given(json_values)
def test_canonical_json_round_trips(value: Any) -> None:
    data = canonical_json(value)
    back = json.loads(data)
    assert back == value
    assert canonical_json(back) == data  # a fixed point: re-encoding changes nothing
    assert hash_obj(value) == sha256_hex(data) == hashlib.sha256(data).hexdigest()


@given(st.dictionaries(st.text(max_size=4), json_values, max_size=4), st.text(max_size=4), json_values)
def test_changing_any_value_changes_the_hash(base: dict[str, Any], key: str, value: Any) -> None:
    changed = dict(base) | {key: value}
    assert (hash_obj(changed) == hash_obj(base)) == (canonical_json(changed) == canonical_json(base))


@given(json_values, st.sampled_from([float("nan"), float("inf"), float("-inf")]))
def test_non_finite_floats_raise_wherever_they_sit(value: Any, bad: float) -> None:
    with pytest.raises(ValueError):
        canonical_json([value, {"x": bad}])


@given(json_values, st.integers() | st.booleans() | st.none() | st.floats(allow_nan=False, allow_infinity=False))
def test_non_string_keys_raise_wherever_they_sit(value: Any, key: Any) -> None:
    with pytest.raises(TypeError, match="keys must be str"):
        canonical_json({"outer": [value, {key: 1}]})


@pytest.mark.parametrize("key", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_float_keys_raise(key: float) -> None:
    # json.dumps refuses these keys before the key check runs: still an error, never a hash.
    with pytest.raises(ValueError):
        canonical_json({key: 1})


# --- the blob store -----------------------------------------------------------------

# Small arbitrary bytes, plus large incompressible ones (built from a seed so hypothesis
# does not have to generate every byte) that span more than one write.
large_bytes = st.builds(lambda seed, n: random.Random(seed).randbytes(n), st.integers(0, 2**32), st.integers(65_536, 200_000))
blob_bytes = st.binary(max_size=4096) | large_bytes


@given(st.lists(blob_bytes, min_size=1, max_size=4))
def test_blobs_round_trip_and_put_is_idempotent(items: list[bytes]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = BlobStore(Path(tmp))
        refs = [store.put(data, "original") for data in items]
        for data, ref in zip(items, refs):
            assert ref.sha256 == hashlib.sha256(data).hexdigest() and ref.size == len(data)
            assert store.exists(ref.sha256) == "present"
            path = store.content_path(ref.sha256)
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            assert store.put(data, "transcript").sha256 == ref.sha256  # role is not part of identity
            assert (path.read_bytes(), path.stat().st_mtime_ns) == before  # not rewritten
            assert store.get(ref.sha256) == data
        distinct = {hashlib.sha256(d).hexdigest() for d in items}
        assert sorted(p.name for p in Path(tmp).rglob("*.zst")) == sorted(f"{s}.zst" for s in distinct)
        assert not list(Path(tmp).rglob("*.tmp"))


@given(blob_bytes, st.text(min_size=1, max_size=20).filter(str.strip), st.binary(max_size=64))
def test_tombstone_wins_over_content(data: bytes, reason: str, other: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = BlobStore(Path(tmp))
        sha = store.put(data, "original").sha256
        store.tombstone(sha, reason)
        assert store.exists(sha) == "tombstoned" and not store.content_path(sha).exists()
        with pytest.raises(BlobTombstoned):
            store.get(sha)
        with pytest.raises(BlobTombstoned):
            store.put(data, "original")
        first = store.tombstone_info(sha)
        store.tombstone(sha, reason + " again")  # idempotent: the first record is kept
        assert store.tombstone_info(sha) == first and first["reason"] == reason
        assert not store.content_path(sha).exists()
        # Other bytes are unaffected.
        if hashlib.sha256(other).hexdigest() != sha:
            assert store.get(store.put(other, "original").sha256) == other
            missing = hashlib.sha256(other + b"\x00never-stored").hexdigest()
            with pytest.raises(BlobMissing):
                store.tombstone(missing, reason)
