"""Canonical JSON encoding and SHA-256 helpers.

Canonical form: UTF-8, keys sorted, separators ``(",", ":")``, no ASCII escaping,
floats rendered by ``repr``. NaN and infinity raise ``ValueError``. Dict keys must
be ``str``: ``json`` would silently turn ``1`` into ``"1"``, so two different
objects could share a hash; non-string keys raise ``TypeError`` instead.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json", "hash_obj", "sha256_hex"]


def _check_keys(obj: Any) -> None:
    stack = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if not isinstance(key, str):
                    raise TypeError(f"canonical JSON keys must be str, got {type(key).__name__}")
                stack.append(value)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def canonical_json(obj: Any) -> bytes:
    """Encode ``obj`` as canonical JSON bytes."""
    # json.dumps runs first so a cyclic object raises instead of looping in _check_keys.
    text = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    _check_keys(obj)
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    """Lowercase hex SHA-256 of the canonical JSON of ``obj``."""
    return sha256_hex(canonical_json(obj))
