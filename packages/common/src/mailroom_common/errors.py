"""The shared exception hierarchy.

Every error this workspace raises on purpose derives from :class:`MailroomError`, so a
caller can catch the family without catching bugs (``KeyError``, ``TypeError`` and the
like are never wrapped). An error carries a short machine-readable ``code`` and a
``context`` mapping of the facts needed to act on it (a path, a line number, a name),
and renders both in ``str()`` so a log line is enough to diagnose it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["ConfigError", "IntegrityError", "MailroomError", "RegistryError"]


class MailroomError(Exception):
    """Base class for every deliberate error in the workspace."""

    code = "mailroom_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, Any] = dict(context)

    def __str__(self) -> str:
        if not self.context:
            return self.message
        facts = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({facts})"


class ConfigError(MailroomError):
    """A configuration or data file is missing, malformed or fails validation."""

    code = "config_error"


class IntegrityError(MailroomError):
    """Stored data does not match its hash, chain or declared shape."""

    code = "integrity_error"


class RegistryError(MailroomError):
    """A plug-in registry refused a registration or a lookup."""

    code = "registry_error"
