"""The reviewer registry: who may act at the desk, in which roles, and how they prove it.

The registry lives at ``config/review/reviewers.yaml``::

    mode: strict | open
    reviewers:
      - id: <reviewer id>             # [a-z0-9][a-z0-9_.-]{0,63}
        initials: <2-6 capital letters>
        roles: [<token>, ...]         # the first is the default role
        token_sha256: <64 hex>        # optional: SHA-256 of the reviewer's API token

Identity
--------
Every desk event records a ``ReviewerRef`` ``{id, role, identity}``:

- ``identify(id, role=None, token=None)``. A listed reviewer who gives a token that hashes
  to their ``token_sha256`` is ``authenticated``; a wrong token, or a token for a reviewer
  with none registered, raises ``TokenRejected``; no token gives ``asserted``. The role
  must be one the registry lists for them (``RoleNotHeld``); without one the first is used.
- An id the registry does not list raises ``UnknownReviewer`` in ``strict`` mode; in
  ``open`` mode (development, tests) it is accepted as ``asserted``, in the role asked for
  (default ``reviewer``), and a token given for it is refused (nothing to check it against).
- ``authenticate(token)`` finds the reviewer whose ``token_sha256`` the token hashes to
  (the HTTP API's bearer token) or raises ``UnknownReviewer``.

Token hashes are compared in constant time. The registry never holds a token itself. Ids,
initials and token hashes are unique. ``load_registry`` collects every problem before
raising ``RegistryError``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from mailroom_desk._yaml import load_yaml_file
from mailroom_desk.errors import RegistryError, RoleNotHeld, TokenRejected, UnknownReviewer
from mailroom_desk.models import ReviewerRef, is_token

__all__ = ["Reviewer", "ReviewerRegistry", "load_registry", "parse_registry", "token_sha256"]

_TOP_KEYS = {"mode", "reviewers"}
_REVIEWER_KEYS = {"id", "initials", "roles", "token_sha256"}
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_INITIALS_RE = re.compile(r"[A-Z]{2,6}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_OPEN_DEFAULT_ROLE = "reviewer"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def token_sha256(token: str) -> str:
    """The SHA-256 (hex) of an API token, as ``token_sha256`` stores it."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Reviewer(BaseModel):
    model_config = _FROZEN

    id: str
    initials: str
    roles: tuple[str, ...]
    token_sha256: str | None = None


class ReviewerRegistry(BaseModel):
    """A validated registry. Build it with ``load_registry`` or ``parse_registry``."""

    model_config = _FROZEN

    mode: Literal["strict", "open"]
    reviewers: tuple[Reviewer, ...]

    def get(self, reviewer_id: str) -> Reviewer | None:
        for r in self.reviewers:
            if r.id == reviewer_id:
                return r
        return None

    def identify(self, reviewer_id: str, *, role: str | None = None, token: str | None = None) -> ReviewerRef:
        """The ``ReviewerRef`` to record for ``reviewer_id`` (see the module doc)."""
        known = self.get(reviewer_id)
        if known is None:
            if self.mode == "strict" or not isinstance(reviewer_id, str) or not _ID_RE.fullmatch(reviewer_id):
                raise UnknownReviewer(str(reviewer_id))
            if token is not None:
                raise TokenRejected(reviewer_id)
            use = _OPEN_DEFAULT_ROLE if role is None else role
            if not is_token(use):
                raise RoleNotHeld(reviewer_id, str(use))
            return ReviewerRef(id=reviewer_id, role=use, identity="asserted")
        use = known.roles[0] if role is None else role
        if use not in known.roles:
            raise RoleNotHeld(reviewer_id, str(use))
        if token is None:
            return ReviewerRef(id=known.id, role=use, identity="asserted")
        if known.token_sha256 is None or not hmac.compare_digest(token_sha256(token), known.token_sha256):
            raise TokenRejected(reviewer_id)
        return ReviewerRef(id=known.id, role=use, identity="authenticated")

    def authenticate(self, token: str, *, role: str | None = None) -> ReviewerRef:
        """The authenticated ``ReviewerRef`` of whoever holds ``token``."""
        digest = token_sha256(token)
        found = None
        for r in self.reviewers:  # check every entry: the time does not depend on the match
            if r.token_sha256 is not None and hmac.compare_digest(digest, r.token_sha256):
                found = r
        if found is None:
            raise UnknownReviewer("<token>")
        return self.identify(found.id, role=role, token=token)


def _problems_of(i: int, entry: Mapping[str, Any], seen: dict[str, set[str]], problems: list[str]) -> bool:
    where = f"reviewers[{i}]"
    bad = len(problems)
    for key in sorted(set(entry) - _REVIEWER_KEYS, key=str):
        problems.append(f"{where} has unknown key {key!r}")
    for key in ("id", "initials", "roles"):
        if key not in entry:
            problems.append(f"{where} is missing {key!r}")
    rid = entry.get("id")
    if "id" in entry and (not isinstance(rid, str) or not _ID_RE.fullmatch(rid)):
        problems.append(f"{where}.id must match [a-z0-9][a-z0-9_.-]{{0,63}}, got {rid!r}")
    initials = entry.get("initials")
    if "initials" in entry and (not isinstance(initials, str) or not _INITIALS_RE.fullmatch(initials)):
        problems.append(f"{where}.initials must be 2-6 capital letters, got {initials!r}")
    roles = entry.get("roles")
    if "roles" in entry:
        if not isinstance(roles, list) or not roles:
            problems.append(f"{where}.roles must be a non-empty list")
        else:
            for role in roles:
                if not is_token(role):
                    problems.append(f"{where}.roles holds {role!r}, not a token")
            if len(set(map(str, roles))) != len(roles):
                problems.append(f"{where}.roles lists a role twice")
    sha = entry.get("token_sha256")
    if "token_sha256" in entry and (not isinstance(sha, str) or not _SHA_RE.fullmatch(sha)):
        problems.append(f"{where}.token_sha256 must be 64 lowercase hex digits")
    for key, value in (("id", rid), ("initials", initials), ("token_sha256", sha)):
        if isinstance(value, str):
            if value in seen[key]:
                problems.append(f"{where}.{key} is used by another reviewer")
            seen[key].add(value)
    return len(problems) == bad


def parse_registry(raw: object, path: Path | None = None) -> ReviewerRegistry:
    """Validate a registry already parsed from YAML; raise ``RegistryError`` listing every problem."""
    if not isinstance(raw, dict):
        raise RegistryError(path, ["a reviewer registry must be a mapping"])
    problems: list[str] = []
    for key in sorted(set(raw) - _TOP_KEYS, key=str):
        problems.append(f"unknown key {key!r}")
    mode = raw.get("mode")
    if mode not in ("strict", "open"):
        problems.append(f"mode must be 'strict' or 'open', got {mode!r}")
    entries = raw.get("reviewers")
    good: list[Reviewer] = []
    if not isinstance(entries, list):
        problems.append("reviewers must be a list")
        entries = []
    seen: dict[str, set[str]] = {"id": set(), "initials": set(), "token_sha256": set()}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"reviewers[{i}] must be a mapping")
            continue
        if _problems_of(i, entry, seen, problems):
            good.append(
                Reviewer(
                    id=entry["id"],
                    initials=entry["initials"],
                    roles=tuple(entry["roles"]),
                    token_sha256=entry.get("token_sha256"),
                )
            )
    if mode == "strict" and isinstance(raw.get("reviewers"), list) and not raw["reviewers"]:
        problems.append("a strict registry must list at least one reviewer")
    if problems:
        raise RegistryError(path, problems)
    return ReviewerRegistry(mode=mode, reviewers=tuple(good))


def load_registry(path: Path | str) -> ReviewerRegistry:
    """Read and validate ``config/review/reviewers.yaml``; raise ``RegistryError`` on any problem."""
    path = Path(path)
    raw, problems = load_yaml_file(path)
    if problems:
        raise RegistryError(path, problems)
    return parse_registry(raw, path)
