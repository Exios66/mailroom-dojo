"""The provider-agnostic model-client interface.

Nothing here names a provider or a model: an agent asks a :class:`ModelClient` to complete
a :class:`ModelRequest`, and which provider and model serve which agent is configuration
(a :class:`ModelMap` read from the run spec or client profile). Adapters for real
providers live with the engine and translate their SDK errors into the
:class:`ProviderError` family below, so retry logic (``mailroom_common.retry``) never needs
to know a provider.
"""

from __future__ import annotations

import difflib
import math
from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from mailroom_common.errors import ConfigError, MailroomError

__all__ = [
    "ChatMessage",
    "ContextLengthError",
    "ModelClient",
    "ModelMap",
    "ModelRequest",
    "ModelResponse",
    "ModelSpec",
    "PermanentProviderError",
    "ProviderError",
    "RateLimitError",
    "TransientProviderError",
    "Usage",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ChatMessage(BaseModel):
    """One turn of a conversation."""

    model_config = _FROZEN

    role: Literal["system", "user", "assistant"]
    content: str


class ModelRequest(BaseModel):
    """What an agent asks for. ``response_format`` is a JSON Schema the reply must match."""

    model_config = _FROZEN

    messages: Sequence[ChatMessage] = Field(min_length=1)
    max_output_tokens: int = Field(gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    response_format: dict[str, Any] | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class Usage(BaseModel):
    """Token counts for one call; ``cached_tokens`` is the part of the input served from cache."""

    model_config = _FROZEN

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _cached_within_input(self) -> Usage:
        if self.cached_tokens > self.input_tokens:
            raise ValueError(
                f"cached_tokens ({self.cached_tokens}) exceeds input_tokens ({self.input_tokens})"
            )
        return self


class ModelResponse(BaseModel):
    """A completed call. ``model_id`` is what the provider reports it actually served."""

    model_config = _FROZEN

    text: str
    usage: Usage
    model_id: str = Field(min_length=1)
    latency_ms: float = Field(ge=0.0)
    finish_reason: str = Field(min_length=1)
    raw: dict[str, Any] | None = None


@runtime_checkable
class ModelClient(Protocol):
    """Anything that completes a request synchronously.

    Implementations raise only :class:`ProviderError` subclasses for provider failures;
    any other exception is a bug and must propagate. Async adapters belong to the engine,
    which runs this call in a worker thread or wraps an async SDK behind it.
    """

    def complete(self, request: ModelRequest) -> ModelResponse: ...


class ProviderError(MailroomError):
    """A model provider failed. ``retryable`` says whether the same request may succeed later;
    ``retry_after`` is the wait in seconds the provider asked for, if any."""

    code = "provider_error"

    def __init__(
        self, message: str, *, retryable: bool, retry_after: float | None = None, **context: Any
    ) -> None:
        if retry_after is not None and not (math.isfinite(retry_after) and retry_after >= 0):
            raise ValueError(f"retry_after must be a finite number >= 0, got {retry_after!r}")
        if retry_after is not None:
            context["retry_after"] = retry_after
        super().__init__(message, **context)
        self.retryable = retryable
        self.retry_after = retry_after


class RateLimitError(ProviderError):
    """The provider throttled the call (HTTP 429 or equivalent). Always retryable."""

    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None, **context: Any) -> None:
        super().__init__(message, retryable=True, retry_after=retry_after, **context)


class TransientProviderError(ProviderError):
    """A failure that may clear on its own: timeout, connection reset, 5xx, empty reply."""

    code = "provider_transient"

    def __init__(self, message: str, *, retry_after: float | None = None, **context: Any) -> None:
        super().__init__(message, retryable=True, retry_after=retry_after, **context)


class ContextLengthError(ProviderError):
    """The request does not fit the model's context window. Retrying cannot help."""

    code = "context_length"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, retryable=False, **context)


class PermanentProviderError(ProviderError):
    """The provider refused the request for good: auth, bad request, unknown model."""

    code = "provider_permanent"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message, retryable=False, **context)


class ModelSpec(BaseModel):
    """Which provider and model serve an agent, and the provider parameters to pass."""

    model_config = _FROZEN

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ModelMap(RootModel[dict[str, ModelSpec]]):
    """Agent name to :class:`ModelSpec`, validated straight from a mapping in config."""

    model_config = ConfigDict(frozen=True)

    def agents(self) -> list[str]:
        """The agent names, sorted."""
        return sorted(self.root)

    def resolve(self, agent: str) -> ModelSpec:
        """The spec for ``agent``; raises :class:`ConfigError` naming the known agents and the
        closest match when it is missing."""
        try:
            return self.root[agent]
        except KeyError:
            known = self.agents()
            raise ConfigError(
                f"no model configured for agent {agent!r}",
                agent=agent,
                known=known,
                did_you_mean=difflib.get_close_matches(agent, known, n=1),
            ) from None
