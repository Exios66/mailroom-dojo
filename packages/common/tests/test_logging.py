from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, TypeVar

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
            return f"Thing(sk-{SECRET})"

    stream = configured()
    get_logger("t").info("x", extra={"thing": Thing()})
    (record,) = lines(stream)
    assert record["thing"] == f"Thing({REDACTED})"


def only_our_handler() -> io.StringIO:
    """Configure, then drop pytest's capture handlers, which would raise on a bad record first."""
    stream = configured()
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if getattr(h, "_mailroom_owned", False)]
    return stream


def test_a_bad_format_argument_never_raises_into_the_caller() -> None:
    stream = only_our_handler()
    get_logger("t").warning("count %d for token=%s", "x", SECRET)
    (record,) = lines(stream)
    assert record["level"] == "WARNING"
    assert record["event"] == (
        f"count %d for token={REDACTED} [unformattable: TypeError; argument types: str, str]"
    )
    assert SECRET not in stream.getvalue()


def test_an_extra_whose_str_raises_never_raises_into_the_caller() -> None:
    class Boom:
        def __str__(self) -> str:
            raise RuntimeError("str failed")

    stream = only_our_handler()
    get_logger("t").warning("obj %s", Boom(), extra={"thing": Boom()})
    (record,) = lines(stream)
    assert record["thing"] == "<unprintable Boom>"
    assert record["event"] == "obj %s [unformattable: RuntimeError; argument types: Boom]"


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
        "calling with key sk-%s; api_key=%s, input_tokens=7, Authorization: Bearer %s",
        SECRET,
        SECRET,
        SECRET,
    )
    (record,) = lines(stream)
    assert SECRET not in stream.getvalue()
    assert record["event"] == (
        f"calling with key {REDACTED}; api_key={REDACTED}, input_tokens=7, Authorization: {REDACTED}"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Basic dXNlcjpwYXNz", f"Authorization: {REDACTED}"),
        ("authorization=Token abc def\nnext line", f"authorization={REDACTED}\nnext line"),
        ("Proxy-Authorization: Digest user=x, nonce=y", f"Proxy-Authorization: {REDACTED}"),
        ('{"Authorization": "Basic abc def", "n": 1}', f'{{"Authorization": "{REDACTED}", "n": 1}}'),
        ("hf_AbCdEfGhIjKlMnOpQrStUvWxYz123456 loaded", f"{REDACTED} loaded"),
        ("pat ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890", f"pat {REDACTED}"),
        ("pat github_pat_11ABCDEFG0123456789_abcdefghijklmnop", f"pat {REDACTED}"),
        ("GET https://user:hunter2@host.example/x", f"GET https://{REDACTED}@host.example/x"),
        ("dsn postgres://:pw0rd@db:5432/x", f"dsn postgres://{REDACTED}@db:5432/x"),
        ("credentials=abc123 passphrase: open-sesame", f"credentials={REDACTED} passphrase: {REDACTED}"),
        ("private_key=-----BEGIN", f"private_key={REDACTED}"),
        ("mailgun key-3ax6xnjp29jd6fds4gc373sgvjxteol0", f"mailgun {REDACTED}"),
        ("SECRET_KEY=abc", f"SECRET_KEY={REDACTED}"),
        ("LANGFUSE_SECRET_KEY=x", f"LANGFUSE_SECRET_KEY={REDACTED}"),
        ("AWS_SECRET_ACCESS_KEY=x", f"AWS_SECRET_ACCESS_KEY={REDACTED}"),
        ("PASSWORD_HASH=x", f"PASSWORD_HASH={REDACTED}"),
        ("api_secret_value=x", f"api_secret_value={REDACTED}"),
        ("TOKEN_V2=x", f"TOKEN_V2={REDACTED}"),
        ("password2=x", f"password2={REDACTED}"),
        ("my.password.value: abc", f"my.password.value: {REDACTED}"),
        ("GET /x?access_token=abc&n=1", f"GET /x?access_token={REDACTED}&n=1"),
        ("Cookie: session=abc; theme=dark", f"Cookie: {REDACTED}"),
        ("set-cookie: id=abc; Path=/; HttpOnly\nnext", f"set-cookie: {REDACTED}\nnext"),
        ("https://user:p@ssSECRET@host.example/x", f"https://{REDACTED}@host.example/x"),
        ("u=ftp://a:b:c@d@e@host?q=1", f"u=ftp://{REDACTED}@host?q=1"),
    ],
)
def test_redacts_credential_shapes_in_text(text: str, expected: str) -> None:
    assert redact_text(text) == expected
    assert redact_text(expected) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('https://password2://x"u@h', f"https://{REDACTED}@h"),
        ('see https://token://a"b@host', f"see https://{REDACTED}@host"),
    ],
)
def test_masks_that_expose_a_new_shape_settle_in_one_call(text: str, expected: str) -> None:
    # Masking the key=value pair removes the "//" that ended the URL authority, which
    # exposes userinfo; one call must reach the settled form, or a second call changes it.
    assert redact_text(text) == expected
    assert redact_text(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "key-value store",
        "key-phrase-extraction",
        "doc_class=key-dates-schedule",
        "max_tokens=4096 input_tokens=10",
        "token_count: 5",
        "token_budget=5000 prompt_token_count=12",
        "see https://host.example/a:b@c",
        "mail https://user@host.example/x",
    ],
)
def test_ordinary_text_is_not_redacted(text: str) -> None:
    assert redact_text(text) == text


# Linearity is checked by growth, not by an absolute time: the CPU time of this process
# (``time.process_time``, so other processes on a loaded machine do not count) at size
# ``n`` and at ``_SCALE * n``. Linear work grows by about ``_SCALE`` (4), quadratic by up
# to ``_SCALE**2`` (16); ``_MAX_RATIO`` sits halfway between them on a log scale. Each size
# takes the minimum of ``_REPEATS`` runs (noise only ever adds time). A ratio above the
# bound is re-measured up to ``_ATTEMPTS`` times and the first one under it passes, so a
# one-off fluke does not fail the test, and a regression growing only a little faster
# than 8x can pass by luck. The sizes are chosen so the quadratic regressions we have
# tried grow about 11x or more. ``_CPU_CEILING`` fails any single run that is
# catastrophically slow, as soon as it happens; it is a backstop, never the primary check.
_SCALE = 4
_MAX_RATIO = 8.0
_REPEATS = 3
_ATTEMPTS = 3
_CPU_CEILING = 10.0

_T = TypeVar("_T")


def _min_cpu_seconds(run: Callable[[_T], object], arg: _T, *, ceiling: float = _CPU_CEILING) -> float:
    """Return the least CPU time of ``_REPEATS`` runs; fail at once if any run passes ``ceiling``."""
    best = float("inf")
    for _ in range(_REPEATS):
        gc.collect()
        gc.disable()
        try:
            started = time.process_time()
            run(arg)
            spent = time.process_time() - started
        finally:
            gc.enable()
        assert spent < ceiling, f"one run took {spent:.2f}s of CPU (ceiling {ceiling}s)"
        best = min(best, spent)
    return best


def test_cpu_ceiling_fails_on_the_first_slow_run() -> None:
    calls: list[int] = []

    def burn(_: None) -> None:
        calls.append(1)
        deadline = time.process_time() + 0.01
        while time.process_time() < deadline:
            pass

    with pytest.raises(AssertionError, match="ceiling"):
        _min_cpu_seconds(burn, None, ceiling=0.005)
    assert calls == [1]


def assert_linear(make: Callable[[int], _T], run: Callable[[_T], object], n: int) -> None:
    """Assert that ``run(make(_SCALE * n))`` costs about ``_SCALE`` times ``run(make(n))``."""
    small, large = make(n), make(_SCALE * n)
    ratios: list[float] = []
    for _ in range(_ATTEMPTS):
        small_cpu = _min_cpu_seconds(run, small)
        large_cpu = _min_cpu_seconds(run, large)
        ratios.append(large_cpu / max(small_cpu, 1e-6))
        if ratios[-1] < _MAX_RATIO:
            return
    pytest.fail(
        f"CPU time grew {', '.join(f'{r:.1f}x' for r in ratios)} for {_SCALE}x the input "
        f"(linear is about {_SCALE}x, the bound {_MAX_RATIO}x)"
    )


# Each case is a generator of its adversarial shape and a base repetition count ``n``;
# the larger run uses ``_SCALE * n``.
_ADVERSARIAL: dict[str, tuple[Callable[[int], str], int]] = {
    "dotted run": (lambda n: "a." * n, 125_000),
    "dashed run": (lambda n: "a-" * n, 125_000),
    "token. run": (lambda n: "token." * n, 42_500),
    "key-a run": (lambda n: "key-a" * n, 50_000),
    "base64url-like": (lambda n: "aB3-_x9Z." * n, 27_500),
    "long userinfo": (lambda n: "https://" + "u:p" * n + " x", 82_500),
    "many schemes": (lambda n: "a://" * n, 62_500),
    "long authorization line": (lambda n: "Authorization:" + " " * n + "'", 250_000),
    "repeated authorization": (lambda n: "authorization " * n, 18_750),
    "bearer spaces": (lambda n: "bearer" + " " * n + "!", 250_000),
    "key= spaces": (lambda n: "password" + " " * n + "!", 250_000),
    "pair chain": (lambda n: "a=" * n, 125_000),
    "sensitive pairs": (lambda n: "token=x " * n, 31_250),
    "at signs": (lambda n: "https://a:" + "@" * n, 250_000),
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_redaction_is_linear_on_adversarial_input(name: str) -> None:
    make, n = _ADVERSARIAL[name]
    assert_linear(make, redact_text, n)


def test_a_long_dashed_argument_logs_quickly() -> None:
    stream = configured()
    logger = get_logger("t")
    assert_linear(lambda n: "-".join(["ab"] * n), lambda arg: logger.info("doc id %s", arg), 12_500)
    assert lines(stream)[0]["event"].startswith("doc id ab-ab")


def test_header_pairs_with_a_sensitive_name_are_redacted_as_key_and_value() -> None:
    headers = [("Authorization", "Basic dXNlcjpwYXNz"), ("Accept", "json"), ["X-Api-Key", "abc"]]
    assert redact({"headers": headers}) == {
        "headers": [["Authorization", REDACTED], ["Accept", "json"], ["X-Api-Key", REDACTED]]
    }
    assert redact(("token", "abc")) == ["token", REDACTED]
    assert redact(("token", "abc", "def")) == ["token", "abc", "def"]


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
        ("credentials", True),
        ("gcpCredential", True),
        ("private_key", True),
        ("privateKey", True),
        ("passphrase", True),
        ("token_budget", False),
        ("prompt_token_count", False),
        ("cookie_jar_size", False),
        ("api_key_limit", False),
        ("secret_total", False),
        ("token_ratio", False),
        ("max_tokens", False),
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


_FRAGMENTS = st.sampled_from(
    [
        "Authorization", "Proxy-Authorization", "authorization", ":", "=", " ", "  ", "'", '"', "\n",
        "Basic", "Bearer", "abc123", "[REDACTED]", "https://", "u", "@", "/", "key-", "sk-",
        "hf_", "ghp_", "github_pat_", "AbCdEfGhIjKlMnOpQrStUvWxYz123456", "password", "token_count",
        "Cookie", "set-cookie", "SECRET_KEY", "password2", ".", "-", "://", "?", "#", ";",
    ]
)  # fmt: skip


@given(st.lists(_FRAGMENTS, max_size=14).map("".join))
def test_property_redaction_of_credential_shapes_is_idempotent(text: str) -> None:
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


@pytest.mark.parametrize("text", ["dbpassword=hunter2", "clientsecret=zzz", "adminPasswd: abc123"])
def test_run_together_credential_keys_are_masked(text: str) -> None:
    out = redact_text(text)
    assert REDACTED in out
    assert not any(secret in out for secret in ("hunter2", "zzz", "abc123"))


def test_secretary_is_not_a_credential_key() -> None:
    assert redact_text("secretary=alice") == "secretary=alice"


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "my secret phrase", "x": 1}',
        "password='two secret words' next=1",
        'api_key="has spaces inside"',
    ],
)
def test_quoted_secret_values_with_spaces_are_masked_whole(text: str) -> None:
    out = redact_text(text)
    assert REDACTED in out
    assert not any(word in out for word in ("secret", "phrase", "words", "spaces", "inside"))
    assert redact_text(out) == out


def test_unclosed_quote_does_not_make_redaction_quadratic() -> None:
    # Twice the other cases' scale: at 25_000 a slice-per-match regression grows only
    # about 9.5x here, too close to the 8x bound.
    assert_linear(lambda n: 'password="' * n, redact_text, 50_000)


def test_self_referential_values_are_truncated_not_a_recursion_error() -> None:
    loop: list[object] = []
    loop.append(loop)
    out = redact({"data": loop})
    node = out["data"]
    depth = 0
    while isinstance(node, list):
        node = node[0]
        depth += 1
    assert node == "[TRUNCATED]" and depth < 40
