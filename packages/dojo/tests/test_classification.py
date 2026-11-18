"""Classification behaviour: the label set a profile builds, label reading, metrics and their failure states."""

from __future__ import annotations

import threading

import pytest
from dojo_testkit import mailroom_profile_mapping, minimal_profile_mapping

from mailroom_dojo.classification import (
    ERROR_PREFIX,
    ClassLabelSet,
    accuracy,
    binary_metrics,
    class_distribution,
    class_label_pattern,
    confusion_accuracy,
    confusion_matrix,
    exact_match,
    failure,
    macro_accuracy,
    macro_prf,
    normalize_label,
    per_class_stats,
    require_equal_lengths,
    top_confusions,
)
from mailroom_dojo.profile import ProfileError, load_scoring_profile

#: The old Dojo's hard-coded class table (llm-dojo-scoring 0.30.1), retired class removed.
OLD_CLASS_TABLE = {
    "merger_agreement": r"\bmerger[_ ]?agreement\b",
    "corporate_record": r"\bcorporate[_ ]?record\b",
    "correspondence": r"\bcorrespondence\b",
    "insurance_claim": r"\binsurance[_ ]?claim\b",
    "contract": r"\bcontract\b",
}


@pytest.fixture(scope="module")
def labels(mailroom_profile):
    """The mailroom-dataset label set."""
    return ClassLabelSet.from_profile(mailroom_profile)


def _profile_with(**class_changes):
    """The mailroom profile with changes applied to named classes (``{key: {field: value}}``)."""
    mapping = mailroom_profile_mapping()
    for spec in mapping["classes"]:
        spec.update(class_changes.get(spec["key"], {}))
    return mapping


# ---------------------------------------------------------------------------
# The label set
# ---------------------------------------------------------------------------


def test_the_mailroom_label_set_is_the_old_class_table(labels):
    """Live classes in the old table order, each recognised by exactly the old pattern."""
    assert labels.class_keys == tuple(OLD_CLASS_TABLE)
    assert {key: [pattern.pattern for pattern in patterns] for key, patterns in labels.patterns} == {
        key: [pattern] for key, pattern in OLD_CLASS_TABLE.items()
    }
    assert dict(labels.exact) == {key: key for key in OLD_CLASS_TABLE}


def test_retired_classes_are_not_labels(labels):
    """Text naming a retired class is kept as written, never read as that class."""
    assert normalize_label("compliance filing", labels) == "compliance filing"
    assert normalize_label("compliance_filing", labels) == "compliance_filing"
    assert normalize_label("court opinion", labels) == "court opinion"
    assert "compliance_filing" not in labels.class_keys


def test_reviving_a_retired_class_makes_it_a_label():
    """The retired list comes from the profile: a live compliance_filing is recognised again."""
    profile = load_scoring_profile(_profile_with(compliance_filing={"status": "live"}))
    revived = ClassLabelSet.from_profile(profile)
    assert revived.class_keys[3] == "compliance_filing"
    assert normalize_label("Compliance Filing", revived) == "compliance_filing"


@pytest.mark.parametrize(
    ("label", "pattern"),
    [
        ("merger_agreement", r"\bmerger[_ ]?agreement\b"),
        ("contract", r"\bcontract\b"),
        ("Auto Policy - Declarations", r"\bauto[_ ]?policy[_ ]?declarations\b"),
        ("a.b+c", r"\ba[_ ]?b[_ ]?c\b"),
        ("日本", r"\b日本\b"),
        ("!!!", None),
        ("", None),
    ],
)
def test_class_label_pattern(label, pattern):
    """A label's words become an ordered, boundary-anchored pattern; a label without words has none."""
    assert class_label_pattern(label) == pattern


def test_match_order_comes_from_the_profile():
    """First match wins in the profile's match order: moving insurance_claim first changes the answer."""
    text = "correspondence about an insurance claim"
    default = ClassLabelSet.from_profile(load_scoring_profile(mailroom_profile_mapping()))
    assert normalize_label(text, default) == "correspondence"
    mapping = mailroom_profile_mapping()
    mapping["classification"]["match_order"] = ["insurance_claim", "correspondence"]
    reordered = ClassLabelSet.from_profile(load_scoring_profile(mapping))
    assert reordered.class_keys[:2] == ("insurance_claim", "correspondence")
    assert normalize_label(text, reordered) == "insurance_claim"
    assert normalize_label("merger agreement", reordered) == "merger_agreement"


def test_sorter_label_and_synonyms_classify_a_new_class():
    """A sorter label is matched exactly and as words; synonyms are regular expressions matched case-blind."""
    mapping = minimal_profile_mapping()
    mapping["classes"][0]["sorter_label"] = "Auto Policy Dec"
    mapping["classes"][0]["synonyms"] = [r"\bdec(?:laration)?s? page\b", r"\bPOLICY JACKET\b"]
    label_set = ClassLabelSet.from_profile(load_scoring_profile(mapping))
    key = "auto_policy_declaration"
    assert dict(label_set.exact) == {key: key, "auto policy dec": key}
    for text in ("AUTO POLICY DEC", "an auto_policy dec here", "Declarations Page", "dec page", "the policy jacket",
                 '{"doc_type": "Auto Policy Declaration"}', "auto policy declaration"):
        assert normalize_label(text, label_set) == key, text
    assert normalize_label("auto policy", label_set) == "auto policy"


def test_a_sorter_label_that_claims_another_class_is_refused():
    """Two live classes may not claim the same exact output; the error names the YAML path."""
    with pytest.raises(ProfileError) as refused:
        ClassLabelSet.from_profile(load_scoring_profile(_profile_with(correspondence={"sorter_label": "Contract"})))
    assert refused.value.path == "classes[3].sorter_label"
    assert "classes[0].key" in str(refused.value)
    with pytest.raises(ProfileError) as case_blind:
        ClassLabelSet.from_profile(load_scoring_profile(
            _profile_with(contract={"sorter_label": "Legal Agreement"}, correspondence={"sorter_label": "legal agreement"})
        ))
    assert case_blind.value.path == "classes[0].sorter_label"
    assert "classes[3].sorter_label" in str(case_blind.value)


def test_a_retired_class_may_reuse_a_live_label():
    """Retired classes are outside the label set, so their labels cannot collide."""
    label_set = ClassLabelSet.from_profile(load_scoring_profile(_profile_with(court_opinion={"sorter_label": "Contract"})))
    assert normalize_label("contract", label_set) == "contract"


def test_a_profile_without_live_classes_cannot_classify():
    """No live class is an error, not an empty label set that reads everything as unmatched."""
    mapping = minimal_profile_mapping()
    mapping["classes"][0]["status"] = "retired"
    with pytest.raises(ProfileError, match="no live class to classify into"):
        ClassLabelSet.from_profile(load_scoring_profile(mapping))


def test_the_label_set_is_immutable(labels):
    """A label set cannot be changed after it is built."""
    with pytest.raises(AttributeError):
        labels.class_keys = ()
    with pytest.raises(TypeError):
        labels.exact["x"] = "contract"


# ---------------------------------------------------------------------------
# Reading labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('{"doc_type": "Contract"}', "contract"),
        ('{"doc_type": "Corporate Record"}', "corporate_record"),
        ("This is a contract agreement", "contract"),
        ("Contract Amendment to a Merger Agreement", "merger_agreement"),
        ("insurance claim", "insurance_claim"),
        ("nonsense", "nonsense"),
        (None, ""),
        ("", ""),
        ('  {"doc_type": "merger agreement"}  ', "merger_agreement"),
        ('{"DOC_TYPE": "CONTRACT"}', "contract"),
        ('{"doc_type": ""}', '{"doc_type": ""}'),
        ('{"doc_type": null, "note": "an insurance claim"}', "insurance_claim"),
        ('{"doc_type": 7}', "7"),
        ('{"doc_type": "contract"', "contract"),
        ("```json\n{\"doc_type\": \"correspondence\"}\n```", "correspondence"),
        ('"contracts"', "contracts"),
        ("**Unknown**", "unknown"),
        ("`__x__`", "x"),
        ("subcontract", "subcontract"),
        ("contract_x", "contract_x"),
        ("merger-agreement", "merger-agreement"),
        ("mergeragreement", "merger_agreement"),
        ("ＣＯＮＴＲＡＣＴ", "ｃｏｎｔｒａｃｔ"),
        (7, "7"),
        (True, "true"),
        ({"doc_type": "contract"}, "contract"),
        ("ERROR: contract", "contract"),
    ],
)
def test_normalize_label(output, expected, labels):
    """Examples of every reading rule, including the old quirks kept for parity."""
    assert normalize_label(output, labels) == expected


def test_exact_match_and_failure(labels):
    """Labels match after normalization; only text starting with the exact error prefix is a failure."""
    assert exact_match("Contract", "contract", labels) == 1.0
    assert exact_match("License Agreement", "contract", labels) == 0.0
    assert exact_match(None, "", labels) == 1.0
    assert failure("ERROR: parse failed", "contract") == 1.0
    assert failure("contract", "contract") == 0.0
    assert failure(" ERROR: x", None) == 0.0
    assert failure("error: x", None) == 0.0
    assert ERROR_PREFIX == "ERROR: "


def test_deeply_nested_json_output_raises_as_the_old_dojo_did(labels):
    """A JSON output nested past the interpreter's recursion limit raises ``RecursionError`` (kept for parity)."""
    deep = '{"a":' * 100_000 + "1" + "}" * 100_000
    with pytest.raises(RecursionError):
        normalize_label(deep, labels)


def test_hostile_text_is_read_without_error(labels):
    """Huge, unicode, lone-surrogate and control-character outputs all give a string."""
    assert normalize_label("x" * 1_000_000 + " contract", labels) == "contract"
    assert normalize_label("\ud800 merger agreement", labels) == "merger_agreement"
    assert normalize_label("İnsurance claim", labels) == "i̇nsurance claim"
    assert normalize_label("\x00\x1b[31m contract", labels) == "contract"
    assert normalize_label("\x1b[31mcontract", labels) == "\x1b[31mcontract"
    assert normalize_label(float("nan"), labels) == "nan"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metric_examples_from_the_old_tests(labels):
    """The old direct tests' hand-computed values."""
    expected = ["contract", "contract", "license", "license", "license"]
    predicted = ["contract", "contract", "contract", "license", "license"]
    assert accuracy(expected, predicted, labels) == 0.8
    assert macro_accuracy(expected, predicted, labels) == 0.8334  # mean of rounded per-class accuracies
    assert accuracy(["a", "a", "b"], ["a", "b", "b"], labels) == 0.6667
    assert macro_accuracy(["a", "a", "b"], ["a", "b", "b"], labels) == 0.75
    assert confusion_matrix(["a", "a", "b"], ["a", "b", "b"], labels) == ([[1, 1], [0, 1]], ["a", "b"])
    assert macro_prf(["a", "a", "b"], ["a", "b", "b"], labels)["f1_macro"] == 0.6667
    assert accuracy([], [], labels) == 0.0
    assert class_distribution(["contract", "Contract", "license"], labels) == {"contract": 2, "license": 1}


def test_error_rows_are_dropped_where_documented(labels):
    """accuracy, per-class stats, confusion and macro P/R/F drop failed rows; binary metrics do not."""
    expected = ["contract", "contract", "license"]
    predicted = ["contract", "ERROR: fail", "license"]
    assert accuracy(expected, predicted, labels) == 1.0
    assert per_class_stats(expected, predicted, labels)["contract"]["n"] == 1
    assert confusion_matrix(expected, predicted, labels)[0] == [[1, 0], [0, 1]]
    assert macro_prf(expected, predicted, labels)["n"] == 2
    assert binary_metrics(expected, predicted, "contract", label_set=labels)["fn"] == 1
    assert accuracy(["a"], ["ERROR: x"], labels) == 0.0


def test_per_class_stats_reads_a_none_prediction_as_text(labels):
    """per_class_stats normalizes ``str(prediction)``, so None is "none"; the confusion matrix reads None as "".

    Kept from the old Dojo; listed in docs/golden-parity.md.
    """
    assert per_class_stats(["none"], [None], labels)["none"]["correct"] == 1
    assert confusion_matrix(["none"], [None], labels) == ([[0, 0], [1, 0]], ["", "none"])
    assert accuracy(["none"], [None], labels) == 0.0


def test_confusion_matrix_label_order(labels):
    """Explicit labels come first, then the other observed labels sorted; duplicates keep the last index."""
    matrix, order = confusion_matrix(["a", "a", "c"], ["a", "b", "c"], labels, labels=["b", "a"])
    assert order == ["b", "a", "c"]
    assert matrix[order.index("a")][order.index("b")] == 1
    matrix, order = confusion_matrix(["a"], ["a"], labels, labels=["a", "a"])
    assert order == ["a", "a"] and matrix == [[0, 0], [0, 1]]


def test_top_confusions(labels):
    """Off-diagonal cells, largest first, ties in row-major order, cut at k."""
    matrix = [[5, 2, 2], [3, 0, 0], [0, 3, 1]]
    assert top_confusions(matrix, ["a", "b", "c"], k=3) == [
        {"expected": "b", "predicted": "a", "count": 3},
        {"expected": "c", "predicted": "b", "count": 3},
        {"expected": "a", "predicted": "b", "count": 2},
    ]
    assert top_confusions(matrix, ["a", "b", "c"], k=0) == []
    assert len(top_confusions(matrix, ["a", "b", "c"], k=-1)) == 3
    with pytest.raises(IndexError):
        top_confusions([[0, 1], [1, 0]], ["a"])


def test_confusion_accuracy_faults():
    """Empty and all-zero matrices score 0.0; a ragged matrix raises."""
    assert confusion_accuracy([]) == 0.0
    assert confusion_accuracy([[0, 0], [0, 0]]) == 0.0
    assert confusion_accuracy([[3, 1], [0, 4]]) == 0.875
    with pytest.raises(IndexError):
        confusion_accuracy([[1], [2]])


def test_binary_metrics_flags_and_normalized(labels):
    """Raw flags read yes/y/true/1/1.0 case-blind; normalized labels compare to the positive class."""
    out = binary_metrics([True, True, False, False], [True, False, True, False], positive="true", label_set=labels)
    assert (out["tp"], out["fp"], out["fn"], out["tn"]) == (1, 1, 1, 1)
    assert out["precision"] == out["recall"] == out["f1"] == out["f2"] == 0.5
    flags = binary_metrics([" YES ", "y", "1.0", 1, "on", "", None, 0], [True] * 8, positive="", normalize=False)
    assert (flags["tp"], flags["fp"]) == (4, 4)
    assert flags["false_positive_rate"] == 1.0 and flags["false_negative_rate"] == 0.0
    empty = binary_metrics([], [], "x", normalize=False)
    assert empty["accuracy"] == 0.0 and empty["precision"] == 0.0


def test_macro_prf_without_normalization(labels):
    """Raw labels are compared as text; an empty expected label is not a class."""
    out = macro_prf(["yes", "no", "yes"], ["yes", "yes", "no"], normalize=False)
    assert out["n_classes"] == 2 and out["f1_macro"] == 0.25
    assert macro_prf(["", ""], ["a", "b"], normalize=False)["n_classes"] == 0
    assert macro_prf(["Yes"], ["yes"], normalize=False)["per_class"]["Yes"]["correct"] == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda labels: binary_metrics(["a"], ["a"], "a"),
        lambda labels: macro_prf(["a"], ["a"]),
    ],
)
def test_normalizing_without_a_label_set_is_refused(call, labels):
    """A metric asked to normalize names its missing label set instead of guessing one."""
    with pytest.raises(TypeError, match="needs a label_set"):
        call(labels)


PAIRED_METRICS = {
    "accuracy": lambda e, p, labels: accuracy(e, p, labels),
    "per_class_stats": lambda e, p, labels: per_class_stats(e, p, labels),
    "macro_accuracy": lambda e, p, labels: macro_accuracy(e, p, labels),
    "confusion_matrix": lambda e, p, labels: confusion_matrix(e, p, labels),
    "macro_prf": lambda e, p, labels: macro_prf(e, p, labels),
    "macro_prf_raw": lambda e, p, labels: macro_prf(e, p, normalize=False),
    "binary_metrics": lambda e, p, labels: binary_metrics(e, p, "contract", label_set=labels),
    "binary_metrics_raw": lambda e, p, labels: binary_metrics(e, p, "yes", normalize=False),
    "require_equal_lengths": lambda e, p, labels: require_equal_lengths(e, p),
}


@pytest.mark.parametrize("name", sorted(PAIRED_METRICS))
@pytest.mark.parametrize("sizes", [(2, 1), (1, 2), (0, 1), (1, 0), (3, 7)])
def test_every_paired_metric_refuses_unequal_lengths(name, sizes, labels):
    """The length check runs before error rows are dropped, with the exact message."""
    expected_size, predicted_size = sizes
    with pytest.raises(ValueError) as refused:
        PAIRED_METRICS[name](["contract"] * expected_size, ["ERROR: unavailable"] * predicted_size, labels)
    assert str(refused.value) == f"expected length {expected_size} != predicted length {predicted_size}"


def test_iterators_are_refused_not_truncated(labels):
    """A one-shot iterator has no length, so it is refused before any row is read."""
    with pytest.raises(TypeError, match="has no len"):
        accuracy(iter(["a"]), iter(["a"]), labels)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_two_profiles_in_threads_do_not_interfere():
    """Two label sets with different match orders give their own answers from eight threads at once."""
    first = ClassLabelSet.from_profile(load_scoring_profile(mailroom_profile_mapping()))
    mapping = mailroom_profile_mapping()
    mapping["classification"]["match_order"] = ["insurance_claim"]
    second = ClassLabelSet.from_profile(load_scoring_profile(mapping))
    text = "correspondence about an insurance claim"
    answers: dict[int, set[str]] = {}
    barrier = threading.Barrier(8)

    def work(index: int) -> None:
        """Classify the same text 500 times with one of the two label sets."""
        label_set = first if index % 2 else second
        barrier.wait()
        answers[index] = {normalize_label(text, label_set) for _ in range(500)}

    threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert answers == {index: {"correspondence" if index % 2 else "insurance_claim"} for index in range(8)}
