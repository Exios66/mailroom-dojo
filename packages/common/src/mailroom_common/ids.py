"""Run ids, path-safe names (slugs) and short content hashes.

A run id is ``YYYYMMDDTHHMMSSZ-xxxxxxxx``: the UTC second it was made, then eight
lowercase hex digits from a random source. Ids made in different seconds sort lexically
in time order; ids made in the same second sort by their random suffix. With a
:class:`~mailroom_common.clock.ManualClock` and a seeded ``random.Random`` the id is
deterministic.

A slug is a name that becomes one path segment (a client, a prompt set, a snapshot):
``[a-z0-9][a-z0-9._-]*``, at most :data:`MAX_SLUG_LENGTH` characters, never containing
``..``. That rules out empty names, ``.``/``..``, separators, NUL, hidden files and names
a command line would read as an option.

Bad data (an invalid slug or run id) raises :class:`~mailroom_common.errors.ConfigError`;
a bad argument from code (``short_hash`` length) raises ``ValueError``.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from mailroom_common.clock import Clock, SystemClock
from mailroom_common.errors import ConfigError
from mailroom_common.hashing import hash_obj, sha256_hex

__all__ = [
    "MAX_SLUG_LENGTH",
    "ParsedRunId",
    "RandomSource",
    "new_run_id",
    "parse_run_id",
    "short_hash",
    "slugify",
    "validate_slug",
]

MAX_SLUG_LENGTH = 64

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_RUN_ID_RE = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-([0-9a-f]{8})")
_NOT_SLUG_CHARS = re.compile(r"[^a-z0-9._-]+")
_DOTS = re.compile(r"\.{2,}")
_LEADING = re.compile(r"^[._-]+")


class RandomSource(Protocol):
    """Anything with ``getrandbits``: ``random.Random`` (seeded, for tests) or ``secrets.SystemRandom``."""

    def getrandbits(self, k: int, /) -> int: ...


@dataclass(frozen=True)
class ParsedRunId:
    """The parts of a run id: its aware UTC timestamp and its eight-hex suffix."""

    timestamp: datetime
    suffix: str


def new_run_id(clock: Clock | None = None, rng: RandomSource | None = None) -> str:
    """A new run id from ``clock`` (default: the system clock) and ``rng`` (default: OS randomness)."""
    now = (clock or SystemClock()).now().astimezone(UTC)
    bits = (rng or secrets.SystemRandom()).getrandbits(32)
    return (
        f"{now.year:04d}{now.month:02d}{now.day:02d}T{now.hour:02d}{now.minute:02d}{now.second:02d}Z"
        f"-{bits:08x}"
    )


def parse_run_id(run_id: str) -> ParsedRunId:
    """Split a run id into its parts; anything else (bad shape or impossible date) is a ConfigError."""
    match = _RUN_ID_RE.fullmatch(run_id) if isinstance(run_id, str) else None
    if match is None:
        raise ConfigError("not a run id (expected YYYYMMDDTHHMMSSZ-xxxxxxxx)", run_id=run_id)
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    try:
        timestamp = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError as exc:
        raise ConfigError(f"run id has an impossible timestamp: {exc}", run_id=run_id) from exc
    return ParsedRunId(timestamp=timestamp, suffix=match.group(7))


def validate_slug(name: str) -> str:
    """Return ``name`` unchanged if it is a valid slug; otherwise raise ConfigError saying why."""
    if not isinstance(name, str):
        raise ConfigError(f"slug must be a string, got {type(name).__name__}", name=name)
    if not name:
        raise ConfigError("slug must not be empty", name=name)
    if len(name) > MAX_SLUG_LENGTH:
        raise ConfigError(f"slug is longer than {MAX_SLUG_LENGTH} characters", name=name)
    if ".." in name:
        raise ConfigError("slug must not contain '..'", name=name)
    if _SLUG_RE.fullmatch(name) is None:
        raise ConfigError(
            "slug must be lowercase [a-z0-9] followed by [a-z0-9._-] (no separators, NUL, "
            "leading dot or dash)",
            name=name,
        )
    return name


def slugify(text: str) -> str:
    """Turn any text into a valid slug; never raises and never returns an invalid slug.

    Lossy on purpose: case-folds, strips accents (``Ünïcödé`` -> ``unicode``), turns every
    run of other characters into ``-``, squeezes ``..`` to ``.``, drops leading ``.``,
    ``_`` and ``-`` and cuts to :data:`MAX_SLUG_LENGTH`. A valid slug comes back
    unchanged. Text with nothing usable left (empty, only emoji or punctuation) becomes
    ``x-`` plus twelve hex digits of its SHA-256, so it is still deterministic and
    distinct texts rarely collide. Distinct texts can share a slug (``A`` and ``a``).
    """
    folded = unicodedata.normalize("NFKD", text.casefold())
    ascii_text = folded.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NOT_SLUG_CHARS.sub("-", ascii_text)
    slug = _DOTS.sub(".", slug)
    slug = _LEADING.sub("", slug)[:MAX_SLUG_LENGTH]
    if slug:
        return slug
    return "x-" + sha256_hex(text.encode("utf-8", "surrogatepass"))[:12]


def short_hash(obj: Any, length: int = 12) -> str:
    """The first ``length`` hex digits of :func:`~mailroom_common.hashing.hash_obj` of ``obj``.

    ``length`` must be 4 to 64 (``ValueError`` otherwise): shorter prefixes collide too
    easily to name anything.
    """
    if not 4 <= length <= 64:
        raise ValueError(f"short_hash length must be between 4 and 64, got {length}")
    return hash_obj(obj)[:length]
