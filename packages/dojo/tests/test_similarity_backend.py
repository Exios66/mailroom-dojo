"""Embedding rescue: when a backend is asked, how its failures are reported, isolation and no network."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import threading
import types
from decimal import Decimal
from fractions import Fraction

import numpy
import pytest

from dojo_testkit import FakeSimilarity, decode, minimal_profile_mapping, scalar_corpus
from mailroom_dojo.field_scoring import scalar_scorers
from mailroom_dojo.field_scoring.scorer_registry import BUILTIN_SCORERS
from mailroom_dojo.field_scoring.sentence_transformer_backend import SentenceTransformerBackend
from mailroom_dojo.field_scoring.similarity_backend import (
    EmbeddingRescue,
    SimilarityBackend,
    SimilarityFallback,
    with_embedding_rescue,
)
from mailroom_dojo.profile import load_scoring_profile

#: A pair whose string name score is well below 0.7.
FAR_PREDICTED, FAR_EXPECTED = "Acme Holdings", "Zebra Partners"


def _constant(value: object) -> FakeSimilarity:
    """A backend that always answers ``value``."""
    return FakeSimilarity({"kind": "value", "value": value})


def test_far_pair_has_a_low_string_score():
    """The pair the tests rely on is below every threshold used here."""
    assert scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED) < 0.3


def test_backend_lifts_a_low_score_and_receives_text():
    """Below the threshold the backend's higher answer wins; it is called with ``str`` of the inputs."""
    backend = _constant(0.91)
    rescue = EmbeddingRescue(backend, 0.7)
    assert scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED, rescue) == 0.91
    assert scalar_scorers.score_free_text_field(FAR_PREDICTED, FAR_EXPECTED, rescue) == 0.91
    assert with_embedding_rescue(0.1, 12, 13.5, rescue) == 0.91
    assert backend.calls[-1] == ("12", "13.5")
    assert rescue.fallbacks == ()


def test_backend_never_lowers_a_score_and_is_not_asked_at_or_above_the_threshold():
    """A lower answer keeps the string score; a score at the threshold is not offered at all."""
    backend = _constant(0.0)
    rescue = EmbeddingRescue(backend, 0.7)
    string_score = scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED)
    assert scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED, rescue) == string_score
    asked = len(backend.calls)
    assert rescue.rescue(0.7, "a", "b") == 0.7
    assert rescue.rescue(0.95, "a", "b") == 0.95
    assert len(backend.calls) == asked


@pytest.mark.parametrize(("predicted", "expected"), [("", "Zebra"), ("Acme", "   "), ("\t", "\n")])
def test_blank_sides_are_never_offered(predicted, expected):
    """A blank prediction or expectation is not rescued."""
    backend = _constant(1.0)
    rescue = EmbeddingRescue(backend, 0.7)
    assert scalar_scorers.score_name_field(predicted, expected, rescue) == scalar_scorers.score_name_field(
        predicted, expected
    )
    assert rescue.rescue(0.0, predicted, expected) == 0.0
    assert backend.calls == []


def test_none_reaches_a_scalar_scorer_as_the_text_none():
    """Kept from the old Dojo: scalar scorers read None as the word "None" (callers drop empties first)."""
    backend = _constant(1.0)
    assert scalar_scorers.score_name_field(None, "None") == 1.0
    assert scalar_scorers.score_name_field(None, "Zebra", EmbeddingRescue(backend, 0.7)) == 1.0
    assert backend.calls == [("None", "Zebra")]


def test_only_name_like_scorers_consult_the_backend():
    """id, money, label, containment and CMS identifiers never ask the backend."""
    backend = _constant(1.0)
    rescue = EmbeddingRescue(backend, 0.7)
    for scorer, predicted, expected in [
        (scalar_scorers.score_id_field, "A-1", "B-2"),
        (scalar_scorers.score_money_field, "$5", "$900"),
        (scalar_scorers.score_label_field, "auto", "home"),
        (scalar_scorers.score_containment_field, "Delaware", "New York"),
        (BUILTIN_SCORERS["id"], "x", "y"),
    ]:
        assert scorer(predicted, expected, rescue) == scorer(predicted, expected)
    from mailroom_dojo.field_scoring.plugins.cms_identifiers import score_cms_doc_ref

    assert score_cms_doc_ref("NPI 1234567890", "NPI 0987654321", rescue) == 0.0
    assert backend.calls == []


@pytest.mark.parametrize(
    ("backend", "reason", "detail"),
    [
        (
            FakeSimilarity({"kind": "raises"}),
            "backend_raised",
            "RuntimeError: fake similarity backend failure",
        ),
        (FakeSimilarity({"kind": "nan"}), "not_a_number", "nan"),
        (_constant("0.9"), "not_a_number", "'0.9'"),
        (_constant(True), "not_a_number", "True"),
        (_constant([0.9]), "not_a_number", "[0.9]"),
        (_constant(1.5), "out_of_range", "1.5"),
        (_constant(-0.01), "out_of_range", "-0.01"),
        (_constant(float("inf")), "out_of_range", "inf"),
        (_constant(float("-inf")), "out_of_range", "-inf"),
        (_constant(Decimal("0.9")), "not_a_number", "Decimal('0.9')"),
        (_constant(10**400), "out_of_range", repr(10**400)[:80]),
        (_constant(10**5000), "out_of_range", "<int too large to show>"),
    ],
    ids=[
        "raises",
        "nan",
        "text",
        "bool",
        "list",
        "above-one",
        "below-zero",
        "inf",
        "minus-inf",
        "decimal",
        "huge-int",
        "int-over-digit-limit",
    ],
)
def test_a_failing_backend_keeps_the_string_score_and_is_reported(backend, reason, detail, caplog):
    """Every failure keeps the string score and records exactly one fallback with its reason and detail."""
    rescue = EmbeddingRescue(backend, 0.7)
    string_score = scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED)
    with caplog.at_level(logging.WARNING, logger="mailroom_dojo.field_scoring.similarity_backend"):
        assert scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED, rescue) == string_score
    assert rescue.fallbacks == (SimilarityFallback(reason, detail, string_score),)
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert reason in caplog.records[0].getMessage()


def test_declining_with_none_is_not_a_failure(caplog):
    """None means "no opinion": string score kept, nothing reported."""
    rescue = EmbeddingRescue(FakeSimilarity({"kind": "none"}), 0.7)
    with caplog.at_level(logging.WARNING):
        assert rescue.rescue(0.2, "a b", "c d") == 0.2
    assert rescue.fallbacks == () and caplog.records == []


@pytest.mark.parametrize("value", [numpy.float32(0.8), numpy.float64(0.8), Fraction(4, 5), 1, 0])
def test_numeric_answers_of_any_real_type_are_used(value):
    """numpy floats, fractions and integers in range are accepted as plain floats."""
    rescue = EmbeddingRescue(_constant(value), 0.7)
    assert rescue.rescue(0.1, "a", "b") == max(0.1, float(value))
    assert rescue.fallbacks == ()


def test_fallbacks_accumulate_in_order_and_are_a_snapshot():
    """Each failure is appended; the exposed tuple does not change after it was read."""
    backend = _constant(2.0)
    rescue = EmbeddingRescue(backend, 0.7)
    rescue.rescue(0.1, "a", "b")
    first = rescue.fallbacks
    backend.spec = {"kind": "raises"}
    rescue.rescue(0.2, "c", "d")
    assert [fallback.reason for fallback in rescue.fallbacks] == ["out_of_range", "backend_raised"]
    assert [fallback.string_score for fallback in rescue.fallbacks] == [0.1, 0.2]
    assert len(first) == 1


@pytest.mark.parametrize(
    ("backend", "threshold", "error"),
    [
        (object(), 0.7, TypeError),
        (lambda first, second: 1.0, 0.7, TypeError),
        (_constant(1.0), "0.7", TypeError),
        (_constant(1.0), True, TypeError),
        (_constant(1.0), 1.01, ValueError),
        (_constant(1.0), -0.1, ValueError),
        (_constant(1.0), float("nan"), ValueError),
    ],
)
def test_bad_construction_is_refused(backend, threshold, error):
    """A backend without ``similarity`` or a threshold outside [0, 1] is refused at construction."""
    with pytest.raises(error):
        EmbeddingRescue(backend, threshold)


def test_fake_and_real_backends_satisfy_the_protocol():
    """The runtime-checkable protocol accepts any object with ``similarity``."""
    assert isinstance(_constant(1.0), SimilarityBackend)
    assert isinstance(SentenceTransformerBackend(model=_UnitModel()), SimilarityBackend)


def test_from_profile_uses_the_profile_threshold_and_refuses_a_disagreement(mailroom_profile):
    """No backend → None; backend with embedding disabled → ValueError; enabled → profile threshold."""
    assert EmbeddingRescue.from_profile(mailroom_profile, None) is None
    with pytest.raises(ValueError, match="disables embedding rescue"):
        EmbeddingRescue.from_profile(mailroom_profile, _constant(1.0))
    enabled = load_scoring_profile(minimal_profile_mapping(embedding={"enabled": True, "rescue_below": 0.4}))
    rescue = EmbeddingRescue.from_profile(enabled, _constant(1.0))
    assert rescue.rescue_below == 0.4
    assert rescue.rescue(0.5, "a", "b") == 0.5  # above this profile's threshold
    assert rescue.rescue(0.3, "a", "b") == 1.0


def test_default_scoring_is_string_only():
    """With no rescue argument every scorer gives the same result as with ``rescue=None``."""
    for name, scorer in BUILTIN_SCORERS.items():
        assert scorer(FAR_PREDICTED, FAR_EXPECTED) == scorer(FAR_PREDICTED, FAR_EXPECTED, None), name


# ---------------------------------------------------------------------------
# Isolation: two profiles, threads, no network
# ---------------------------------------------------------------------------


def test_two_profiles_with_different_thresholds_in_threads_do_not_interfere():
    """Rescues built from two profiles, used concurrently, each apply only their own threshold."""
    low = load_scoring_profile(minimal_profile_mapping(embedding={"enabled": True, "rescue_below": 0.05}))
    high = load_scoring_profile(minimal_profile_mapping(embedding={"enabled": True, "rescue_below": 0.95}))
    string_score = scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED)
    assert 0.05 < string_score < 0.95
    results: dict[str, list[float]] = {"low": [], "high": []}
    rescues: dict[str, EmbeddingRescue] = {}
    barrier = threading.Barrier(8)

    def work(label: str, profile, index: int) -> None:
        """Score the far pair many times with this thread's own rescue."""
        rescue = EmbeddingRescue.from_profile(profile, _constant(0.99))
        rescues[f"{label}{index}"] = rescue
        barrier.wait()
        for _ in range(300):
            results[label].append(scalar_scorers.score_name_field(FAR_PREDICTED, FAR_EXPECTED, rescue))

    threads = [
        threading.Thread(target=work, args=(label, profile, index))
        for index in range(4)
        for label, profile in (("low", low), ("high", high))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(results["low"]) == {string_score} and len(results["low"]) == 1200
    assert set(results["high"]) == {0.99} and len(results["high"]) == 1200
    assert all(rescue.fallbacks == () for rescue in rescues.values())
    assert low.embedding.rescue_below == 0.05 and high.embedding.rescue_below == 0.95


def test_fallbacks_of_one_rescue_never_appear_on_another():
    """Each EmbeddingRescue keeps its own fallback list."""
    failing, healthy = EmbeddingRescue(_constant(9.0), 0.7), EmbeddingRescue(_constant(0.8), 0.7)
    failing.rescue(0.1, "a", "b")
    healthy.rescue(0.1, "a", "b")
    assert len(failing.fallbacks) == 1 and healthy.fallbacks == ()


def test_scoring_opens_no_socket(monkeypatch, mailroom_profile):
    """Every scorer, the profile loader and the whole differential corpus run with sockets forbidden."""

    def refuse(*args, **kwargs):
        """Fail the test: scoring must not touch the network."""
        raise AssertionError("scoring opened a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    from dojo_testkit import MAILROOM_PROFILE_PATH
    from test_differential_scalar_parity import ADAPTERS

    load_scoring_profile(MAILROOM_PROFILE_PATH)
    _, cases = scalar_corpus()
    for case in cases[::7]:
        try:
            ADAPTERS[case["function"]](decode(case["args"]), mailroom_profile)
        except AssertionError:
            raise
        except Exception:  # the corpus records which old calls raised; only network use matters here
            pass


# ---------------------------------------------------------------------------
# The optional sentence-transformers backend, with a fake model
# ---------------------------------------------------------------------------


class _UnitModel:
    """A fake embedding model: maps each text to a fixed unit vector, counting encodes."""

    VECTORS = {
        "cat": numpy.array([1.0, 0.0]),
        "kitten": numpy.array([0.8, 0.6]),
        "dog": numpy.array([0.0, 1.0]),
        "anti-cat": numpy.array([-1.0, 0.0]),
    }

    def __init__(self) -> None:
        """Start with no encodes."""
        self.encoded: list[str] = []

    def encode(self, texts, normalize_embeddings=False):
        """Return the vectors of ``texts``; insists on normalized embeddings."""
        assert normalize_embeddings is True
        self.encoded.extend(texts)
        return [self.VECTORS[text] for text in texts]


def test_sentence_backend_computes_clipped_cosine_and_caches():
    """Cosine of unit vectors, negatives clipped to 0, repeated texts encoded once."""
    model = _UnitModel()
    backend = SentenceTransformerBackend(model=model)
    assert backend.similarity("cat", "kitten") == pytest.approx(0.8)
    assert backend.similarity("cat", "dog") == 0.0
    assert backend.similarity("cat", "anti-cat") == 0.0
    assert backend.similarity("cat", "cat") == 1.0
    assert model.encoded.count("cat") == 1


def test_sentence_backend_cache_is_bounded():
    """With cache size 2, a third text evicts the least recently used one."""
    model = _UnitModel()
    backend = SentenceTransformerBackend(model=model, cache_size=2)
    backend.similarity("cat", "dog")
    backend.similarity("kitten", "dog")  # evicts cat
    backend.similarity("cat", "dog")
    assert model.encoded == ["cat", "dog", "kitten", "cat"]


def test_sentence_backend_rescues_end_to_end():
    """Through EmbeddingRescue, a semantic match lifts a low string score."""
    rescue = EmbeddingRescue(SentenceTransformerBackend(model=_UnitModel()), 0.7)
    assert scalar_scorers.score_name_field("cat", "kitten", rescue) == pytest.approx(0.8)


def test_sentence_backend_loads_local_files_only(monkeypatch):
    """A named model is loaded with ``local_files_only=True``, so building it never downloads."""
    calls = []

    def fake_sentence_transformer(name, **kwargs):
        """Record how the model was requested."""
        calls.append((name, kwargs))
        return _UnitModel()

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=fake_sentence_transformer),
    )
    backend = SentenceTransformerBackend("/models/minilm")
    assert calls == [("/models/minilm", {"local_files_only": True})]
    assert backend.similarity("cat", "kitten") == pytest.approx(0.8)


def test_sentence_backend_without_the_extra_names_it(monkeypatch):
    """Without sentence-transformers installed, building a named backend says which extra to install."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(ImportError, match=r"mailroom-dojo\[embeddings\]"):
        SentenceTransformerBackend("all-MiniLM-L6-v2")


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"model_name_or_path": "x", "model": _UnitModel()},
        {"model": _UnitModel(), "cache_size": 0},
        {"model": _UnitModel(), "cache_size": True},
    ],
)
def test_sentence_backend_bad_arguments(kwargs):
    """Neither or both of name and model, or a non-positive cache, is refused."""
    with pytest.raises(ValueError):
        SentenceTransformerBackend(**kwargs)


def test_importing_the_dojo_does_not_import_sentence_transformers():
    """The heavy optional dependency is imported only when a backend is built."""
    code = (
        "import sys, mailroom_dojo, mailroom_dojo.field_scoring.sentence_transformer_backend;"
        "print('sentence_transformers' in sys.modules, 'torch' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False False"
