from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mailroom_common.errors import ConfigError, MailroomError
from mailroom_common.models import (
    ChatMessage,
    ContextLengthError,
    ModelClient,
    ModelMap,
    ModelRequest,
    ModelResponse,
    ModelSpec,
    PermanentProviderError,
    ProviderError,
    RateLimitError,
    TransientProviderError,
    Usage,
)
from mailroom_common.testing import FakeModelClient
from mailroom_common.yamlio import load_yaml


def request(**overrides: Any) -> ModelRequest:
    fields: dict[str, Any] = {"messages": [ChatMessage(role="user", content="hi")], "max_output_tokens": 10}
    fields.update(overrides)
    return ModelRequest(**fields)


def test_chat_message_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError, match="role"):
        ChatMessage(role="tool", content="x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"messages": []}, "messages"),
        ({"max_output_tokens": 0}, "max_output_tokens"),
        ({"temperature": -0.1}, "temperature"),
        ({"temperature": 2.5}, "temperature"),
        ({"tags": {"run": 1}}, "tags"),
        ({"stream": True}, "stream"),
    ],
)
def test_model_request_rejects_bad_fields(overrides: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as info:
        request(**overrides)
    assert field in str(info.value)


def test_model_request_is_immutable_and_defaults() -> None:
    req = request()
    assert req.temperature == 0.0
    assert req.response_format is None
    assert req.tags == {}
    with pytest.raises(ValidationError):
        req.max_output_tokens = 5


def test_model_request_round_trips_through_json() -> None:
    req = request(
        response_format={"type": "object", "properties": {"label": {"type": "string"}}}, tags={"a": "b"}
    )
    assert ModelRequest.model_validate_json(req.model_dump_json()) == req


def test_usage_rejects_negative_and_cached_above_input() -> None:
    with pytest.raises(ValidationError, match="input_tokens"):
        Usage(input_tokens=-1, output_tokens=0)
    with pytest.raises(ValidationError, match=r"cached_tokens \(11\) exceeds input_tokens \(10\)"):
        Usage(input_tokens=10, output_tokens=0, cached_tokens=11)
    assert Usage(input_tokens=10, output_tokens=2, cached_tokens=10).cached_tokens == 10


def test_model_response_requires_model_id_and_nonnegative_latency() -> None:
    usage = Usage(input_tokens=1, output_tokens=1)
    with pytest.raises(ValidationError, match="model_id"):
        ModelResponse(text="x", usage=usage, model_id="", latency_ms=1.0, finish_reason="stop")
    with pytest.raises(ValidationError, match="latency_ms"):
        ModelResponse(text="x", usage=usage, model_id="m", latency_ms=-1.0, finish_reason="stop")


def test_fake_client_satisfies_the_protocol() -> None:
    assert isinstance(FakeModelClient(), ModelClient)
    assert not isinstance(object(), ModelClient)


@pytest.mark.parametrize(
    ("error", "retryable", "code"),
    [
        (RateLimitError("slow down"), True, "rate_limited"),
        (TransientProviderError("timeout"), True, "provider_transient"),
        (ContextLengthError("too long"), False, "context_length"),
        (PermanentProviderError("bad auth"), False, "provider_permanent"),
    ],
)
def test_provider_error_family(error: ProviderError, retryable: bool, code: str) -> None:
    assert isinstance(error, ProviderError)
    assert isinstance(error, MailroomError)
    assert error.retryable is retryable
    assert error.code == code
    assert error.retry_after is None


def test_retry_after_is_kept_and_shown() -> None:
    error = RateLimitError("slow down", retry_after=12.5, status=429)
    assert error.retry_after == 12.5
    assert str(error) == "slow down (retry_after=12.5, status=429)"


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_retry_after_must_be_finite_and_nonnegative(bad: float) -> None:
    with pytest.raises(ValueError, match="retry_after must be a finite number >= 0"):
        TransientProviderError("x", retry_after=bad)


def test_model_map_resolves_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        "sorter:\n  provider: p1\n  model: m-small\n  params: {top_p: 0.9}\n"
        "extractor:\n  provider: p2\n  model: m-large\n",
        encoding="utf-8",
    )
    models = ModelMap.model_validate(load_yaml(path))
    assert models.agents() == ["extractor", "sorter"]
    assert models.resolve("sorter") == ModelSpec(provider="p1", model="m-small", params={"top_p": 0.9})
    assert models.resolve("extractor").params == {}


def test_model_map_missing_agent_lists_known_and_suggests() -> None:
    models = ModelMap.model_validate(
        {"sorter": {"provider": "p", "model": "m"}, "judge": {"provider": "p", "model": "m"}}
    )
    with pytest.raises(ConfigError) as info:
        models.resolve("sortr")
    assert info.value.message == "no model configured for agent 'sortr'"
    assert info.value.context == {"agent": "sortr", "known": ["judge", "sorter"], "did_you_mean": ["sorter"]}


def test_model_map_missing_agent_on_empty_map() -> None:
    with pytest.raises(ConfigError) as info:
        ModelMap.model_validate({}).resolve("sorter")
    assert info.value.context["known"] == []
    assert info.value.context["did_you_mean"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"sorter": {"provider": "", "model": "m"}},
        {"sorter": {"provider": "p"}},
        {"sorter": {"provider": "p", "model": "m", "temperature": 0.2}},
        {"sorter": "p/m"},
    ],
)
def test_model_map_rejects_malformed_specs(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="sorter"):
        ModelMap.model_validate(bad)
