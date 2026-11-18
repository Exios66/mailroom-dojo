from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mailroom_common.errors import ConfigError
from mailroom_common.logging import (
    REDACTED,
    bind_context,
    configure_logging,
    current_context,
    get_logger,
    redact,
    redact_text,
)

SECRET = "SECRET-VALUE-9f8e7d6c"


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def configured(level: str = "DEBUG", json: bool = True) -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level, json=json, stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# --- output shape -------------------------------------------------------------------------


def test_json_line_has_base_fields_context_and_extras() -> None:
    stream = configured()
    with bind_context(run_id="r1"):
        get_logger("mailroom.test").info("node %s done", "sorter", extra={"seq": 3})
    (record,) = lines(stream)
    assert list(record)[:4] == ["ts", "level", "logger", "event"]
    assert record["level"] == "INFO"
    assert record["logger"] == "mailroom.test"
    assert record["event"] == "node sorter done"
    assert record["run_id"] == "r1"
    assert record["seq"] == 3
    ts = datetime.fromisoformat(record["ts"])
    assert ts.utcoffset() == UTC.utcoffset(None)
    assert abs((datetime.now(UTC) - ts).total_seconds()) < 60


def test_multiline_message_stays_on_one_line() -> None:
    stream = configured()
    get_logger("t").warning("line one\nline two")
    assert len(stream.getvalue().splitlines()) == 1
    assert lines(stream)[0]["event"] == "line one\nline two"


def test_base_keys_cannot_be_spoofed_by_fields() -> None:
    stream = configured()
    with bind_context(logger="spoof", ts="spoof"):
        get_logger("real").error("x", extra={"event": "spoof", "level": "spoof"})
    (record,) = lines(stream)
    assert (record["logger"], record["level"], record["event"]) == ("real", "ERROR", "x")
    assert record["ts"] != "spoof"


def test_extra_overrides_bound_context_of_same_name() -> None:
    stream = configured()
    with bind_context(stage="outer"):
        get_logger("t").info("x", extra={"stage": "inner"})
    assert lines(stream)[0]["stage"] == "inner"


def test_unserializable_field_is_rendered_as_text() -> None:
    class Thing:
        def __str__(self) -> str:
            return f"Thing(key-{SECRET})"

    stream = configured()
    get_logger("t").info("x", extra={"thing": Thing()})
    (record,) = lines(stream)
    assert record["thing"] == f"Thing({REDACTED})"


def test_level_filters_records() -> None:
    stream = configured("WARNING")
    log = get_logger("t")
    log.info("dropped")
    log.warning("kept")
    assert [r["event"] for r in lines(stream)] == ["kept"]


def test_text_mode_renders_fields_and_redacts() -> None:
    stream = configured(json=False)
    with bind_context(run_id="r1"):
        get_logger("t").info("using password=%s", SECRET, extra={"token": SECRET})
    text = stream.getvalue()
    assert SECRET not in text
    assert 'run_id="r1"' in text
    assert f'token="{REDACTED}"' in text
    assert f"INFO t: using password={REDACTED}" in text


# --- configure_logging ----------------------------------------------------------------------


def test_configure_twice_does_not_duplicate_lines() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    configure_logging("INFO", stream=stream)
    get_logger("t").info("once")
    assert len(stream.getvalue().splitlines()) == 1


def test_reconfigure_moves_output_and_keeps_foreign_handlers() -> None:
    foreign_stream = io.StringIO()
    foreign = logging.StreamHandler(foreign_stream)
    logging.getLogger().addHandler(foreign)
    first, second = io.StringIO(), io.StringIO()
    configure_logging("INFO", stream=first)
    configure_logging("INFO", stream=second)
    get_logger("t").info("hello")
    assert first.getvalue() == ""
    assert len(second.getvalue().splitlines()) == 1
    assert foreign in logging.getLogger().handlers
    assert "hello" in foreign_stream.getvalue()


def test_unknown_level_raises_config_error() -> None:
    with pytest.raises(ConfigError) as info:
        configure_logging("LOUD")
    assert info.value.message == "unknown log level"
    assert info.value.context["level"] == "LOUD"
    assert "DEBUG" in info.value.context["known"]


def test_level_accepts_any_case_and_numbers() -> None:
    configure_logging("debug", stream=io.StringIO())
    assert logging.getLogger().level == logging.DEBUG
    configure_logging(logging.ERROR, stream=io.StringIO())
    assert logging.getLogger().level == logging.ERROR


# --- bind_context ---------------------------------------------------------------------------


def test_bind_context_nests_and_restores() -> None:
    assert current_context() == {}
    with bind_context(a=1, b=1):
        with bind_context(b=2, c=3):
            assert current_context() == {"a": 1, "b": 2, "c": 3}
        assert current_context() == {"a": 1, "b": 1}
    assert current_context() == {}


def test_bind_context_restores_on_exception() -> None:
    with pytest.raises(RuntimeError, match="inner failure"), bind_context(doc="d1"):
        raise RuntimeError("inner failure")
    assert current_context() == {}


def test_threads_do_not_see_each_others_context(json_log_records: Any) -> None:
    barrier = threading.Barrier(4)
    after: dict[int, dict[str, Any]] = {}

    def worker(n: int) -> None:
        with bind_context(doc=f"doc-{n}"):
            barrier.wait(timeout=5)  # every thread is inside its binding at once
            for i in range(50):
                get_logger("worker").info("step", extra={"n": n, "i": i})
            barrier.wait(timeout=5)
        after[n] = current_context()

    with bind_context(doc="main"):
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert current_context() == {"doc": "main"}
    assert sorted(after) == [0, 1, 2, 3]  # every worker finished (a failed one would be missing)
    assert all(context.get("doc") != f"doc-{n}" for n, context in after.items())
    records = json_log_records.records
    assert len(records) == 200
    for record in records:
        assert record["doc"] == f"doc-{record['n']}"


def test_asyncio_tasks_are_isolated() -> None:
    seen: dict[str, list[dict[str, Any]]] = {}

    async def task(name: str) -> None:
        with bind_context(task=name):
            seen[name] = []
            for _ in range(5):
                await asyncio.sleep(0)  # let the other task run inside its own binding
                seen[name].append(current_context())

    async def main() -> None:
        await asyncio.gather(task("a"), task("b"))

    asyncio.run(main())
    assert seen["a"] == [{"task": "a"}] * 5
    assert seen["b"] == [{"task": "b"}] * 5


# --- redaction ------------------------------------------------------------------------------


def test_redacts_nested_payloads_in_context_and_extra() -> None:
    stream = configured()
    payload = {
        "headers": {"Authorization": f"Bearer {SECRET}", "X-Api-Key": SECRET, "Accept": "json"},
        "clients": [{"name": "c1", "client_secret": SECRET}],
        "Cookie": {"session": SECRET},
        "usage": {"input_tokens": 12, "max_output_tokens": 50},
        "tokenizer": "bpe",
    }
    assert SECRET in json.dumps(payload)  # positive control: the secret is really there
    with bind_context(auth={"accessToken": SECRET}, password=SECRET):
        get_logger("t").info("request", extra={"request": payload})
    (record,) = lines(stream)
    assert SECRET not in stream.getvalue()
    request = record["request"]
    assert request["headers"] == {"Authorization": REDACTED, "X-Api-Key": REDACTED, "Accept": "json"}
    assert request["clients"] == [{"name": "c1", "client_secret": REDACTED}]
    assert request["Cookie"] == REDACTED
    assert request["usage"] == {"input_tokens": 12, "max_output_tokens": 50}
    assert request["tokenizer"] == "bpe"
    assert record["auth"] == {"accessToken": REDACTED}
    assert record["password"] == REDACTED


def test_redacts_secrets_in_message_text_and_args() -> None:
    stream = configured()
    get_logger("t").info(
        "calling with Authorization: Bearer %s and key sk-%s; api_key=%s, input_tokens=7",
        SECRET,
        SECRET,
        SECRET,
    )
    (record,) = lines(stream)
    assert SECRET not in stream.getvalue()
    assert record["event"] == (
        f"calling with Authorization: Bearer {REDACTED} and key {REDACTED}; "
        f"api_key={REDACTED}, input_tokens=7"
    )


def test_redacts_exception_text_and_chained_causes() -> None:
    stream = configured()
    log = get_logger("t")
    try:
        try:
            raise KeyError(f"token={SECRET}")
        except KeyError as inner:
            raise RuntimeError(f"provider said: Bearer {SECRET}") from inner
    except RuntimeError:
        log.exception("call failed")
    (record,) = lines(stream)
    assert SECRET not in stream.getvalue()
    assert "RuntimeError: provider said: Bearer [REDACTED]" in record["exc"]
    assert "KeyError" in record["exc"]
    assert "Traceback" in record["exc"]


def test_redacts_stack_info() -> None:
    stream = configured()
    log = get_logger("t.stack")
    raw = io.StringIO()
    raw_handler = logging.StreamHandler(raw)  # on the child logger: runs before ours, unredacted
    raw_handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(raw_handler)
    try:
        log.info("with stack", stack_info=True)  # api_key=SECRET-VALUE-9f8e7d6c
    finally:
        log.removeHandler(raw_handler)
    assert SECRET in raw.getvalue()  # positive control: the stack really carries the secret
    (record,) = lines(stream)
    assert "test_redacts_stack_info" in record["stack"]
    assert SECRET not in stream.getvalue()


def test_other_handlers_see_the_redacted_record_after_ours() -> None:
    configured()
    later_stream = io.StringIO()
    later = logging.StreamHandler(later_stream)
    later.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(later)
    get_logger("t").info("password=%s", SECRET)
    assert later_stream.getvalue() == f"password={REDACTED}\n"


@pytest.mark.parametrize(
    ("key", "masked"),
    [
        ("api_key", True),
        ("API-KEY", True),
        ("apiKey", True),
        ("openai_api_key", True),
        ("access_token", True),
        ("refreshToken", True),
        ("Authorization", True),
        ("set-cookie", True),
        ("db.password", True),
        ("client_secret", True),
        ("input_tokens", False),
        ("cached_tokens", False),
        ("tokenizer", False),
        ("secretary", False),
        ("keyword", False),
        ("model", False),
    ],
)
def test_sensitive_key_matching(key: str, masked: bool) -> None:
    assert (redact({key: "v"})[key] == REDACTED) is masked


_SECRET_CHARS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-", min_size=8
)


@given(secret=_SECRET_CHARS.filter(lambda s: s not in f"Bearer {REDACTED}"), depth=st.integers(0, 5))
def test_property_secret_under_sensitive_key_never_survives(secret: str, depth: int) -> None:
    value: Any = {"api_key": secret, "plain": 1}
    for level in range(depth):
        value = {f"layer{level}": [value]}
    assert secret not in json.dumps(redact(value))


@given(secret=_SECRET_CHARS.filter(lambda s: s not in f"Bearer {REDACTED}"), prefix=st.text(max_size=20))
def test_property_bearer_and_sk_tokens_never_survive_in_text(secret: str, prefix: str) -> None:
    assume(secret not in prefix)
    for text in (f"{prefix} Bearer {secret}", f"{prefix} sk-{secret}", f"{prefix} password={secret}"):
        assert secret not in redact_text(text)


@given(st.text())
def test_property_redaction_is_idempotent(text: str) -> None:
    once = redact_text(text)
    assert redact_text(once) == once


@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(),
        lambda c: st.lists(c) | st.dictionaries(st.text(), c),
    )
)
def test_property_redact_output_is_json_serializable(value: Any) -> None:
    json.dumps(redact(value))
