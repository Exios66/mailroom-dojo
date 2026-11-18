"""Read-time schema upgrades for stored runs, events and seals (ADR-0005).

Stored bytes are never rewritten. A reader parses a stored line into a dict and passes it
through ``upgrade_event`` / ``upgrade_run`` / ``upgrade_seal``, which apply a table of
``{from_version: step}`` functions in order until the dict is at the current version. A
version newer than this code raises ``UnknownSchemaVersion``. Input that is not a mapping
raises ``TypeError``; a mapping nested deeper than the reader can copy raises ``ValueError``,
so a damaged record never escapes a reader as ``AttributeError`` or ``RecursionError``.
Error messages name types and versions, never stored values.

Hashes are never checked on the upgraded dict: an event's ``hash`` covers the form it was
written in, so a verifier hashes the parsed stored dict *before* upgrading it. Upgrades
return a new dict and never mutate their input, so the stored form stays available.

Version 0 exists only to prove the path: a v0 event lacked ``tokens_cached`` and a v0 run
lacked ``signed_by``. Both become ``None`` (absent, never guessed).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "CURRENT_EVENT_VERSION",
    "CURRENT_RUN_VERSION",
    "CURRENT_SEAL_VERSION",
    "UnknownSchemaVersion",
    "upgrade_event",
    "upgrade_run",
    "upgrade_seal",
]

CURRENT_EVENT_VERSION = 1
CURRENT_RUN_VERSION = 1
CURRENT_SEAL_VERSION = 1

Step = Callable[[dict[str, Any]], dict[str, Any]]


class UnknownSchemaVersion(ValueError):
    """A stored object's ``schema_version`` is missing, malformed or newer than the code."""


def _event_0_to_1(raw: dict[str, Any]) -> dict[str, Any]:
    raw.setdefault("tokens_cached", None)
    raw["schema_version"] = 1
    return raw


def _run_0_to_1(raw: dict[str, Any]) -> dict[str, Any]:
    raw.setdefault("signed_by", None)
    raw["schema_version"] = 1
    return raw


EVENT_STEPS: Mapping[int, Step] = {0: _event_0_to_1}
RUN_STEPS: Mapping[int, Step] = {0: _run_0_to_1}
SEAL_STEPS: Mapping[int, Step] = {}


def _upgrade(raw: Mapping[str, Any], steps: Mapping[int, Step], current: int, what: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):  # a stored file or line that parsed to a list, a number, ...
        raise TypeError(f"{what}: a stored {what} must be a JSON object, got {type(raw).__name__}")
    version = raw.get("schema_version")
    # bool is an int subclass; True must not pass as version 1.
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        # Name the type, never the value: a damaged record may hold document text there.
        shown = repr(version) if isinstance(version, int) else type(version).__name__
        raise UnknownSchemaVersion(f"{what}: schema_version must be a non-negative int, got {shown}")
    if version > current:
        raise UnknownSchemaVersion(f"{what}: schema_version {version} is newer than this code ({current})")
    try:
        out = copy.deepcopy(dict(raw))
    except RecursionError:  # parsed, but nests deeper than copying can recurse (any Python version)
        raise ValueError(f"{what}: nested too deeply to read") from None
    while version < current:
        step = steps.get(version)
        if step is None:
            raise UnknownSchemaVersion(f"{what}: no upgrade from schema_version {version}")
        out = step(out)
        version = out["schema_version"]
    return out


def upgrade_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a stored event dict at ``CURRENT_EVENT_VERSION``."""
    return _upgrade(raw, EVENT_STEPS, CURRENT_EVENT_VERSION, "event")


def upgrade_run(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a stored run header dict at ``CURRENT_RUN_VERSION``."""
    return _upgrade(raw, RUN_STEPS, CURRENT_RUN_VERSION, "run")


def upgrade_seal(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a stored seal dict at ``CURRENT_SEAL_VERSION``."""
    return _upgrade(raw, SEAL_STEPS, CURRENT_SEAL_VERSION, "seal")
