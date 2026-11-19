"""Label vocabularies: each fold method, tie order, closure of results and the mailroom vocabularies."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dojo_testkit import minimal_profile_mapping
from mailroom_dojo.profile import CatalogEntry, LabelVocabulary, load_scoring_profile

label_surfaces = st.one_of(
    st.text(max_size=40),
    st.sampled_from(["", " ", "Other", "OTHER", "n/a", "none", "-", "Notice of Claim", "amendment #2"]),
    st.none(),
    st.integers(),
    st.floats(allow_nan=True),
)


def _all_vocabularies(profile):
    """Every label vocabulary and subclass vocabulary the profile holds, with where it lives."""
    for spec in profile.classes:
        if spec.subclasses is not None:
            yield f"{spec.key}.subclasses", spec.subclasses
        for field_spec in spec.fields + tuple(f for variant in spec.variants for f in variant.added_fields):
            if field_spec.vocabulary is not None:
                yield f"{spec.key}.{field_spec.name}", field_spec.vocabulary


def test_exact_fold_matches_compact_form_only():
    """Exact folding ignores case, spaces and punctuation, and nothing else."""
    vocabulary = LabelVocabulary(tokens=("auto", "home_owner"))
    assert vocabulary.fold(" AUTO ") == "auto"
    assert vocabulary.fold("Home-Owner") == "home_owner"
    assert vocabulary.fold("homeowners") == ""
    assert vocabulary.fold(None) == "" and vocabulary.fold("") == ""
    assert vocabulary.normalize("boat") == "other"


def test_alias_fold_uses_the_alias_table_by_compact_form():
    """Aliases match on the compact form; an alias never matches as a substring."""
    vocabulary = LabelVocabulary(
        tokens=("approved", "denied"),
        fold_method="aliases",
        aliases={"paid in full": "approved", "rejected": "denied"},
    )
    assert vocabulary.fold("PAID-IN-FULL") == "approved"
    assert vocabulary.fold("rejected") == "denied"
    assert vocabulary.fold("claim rejected") == ""


def test_inventory_fold_order_prefix_alias_then_exact_then_alias_then_tokens():
    """Prefix aliases win, then exact tokens and aliases, long aliases inside, then token prefixes and
    substrings."""
    vocabulary = LabelVocabulary(
        tokens=("notice", "amend_merger", "termination", "other", "10k"),
        fold_method="inventory",
        aliases={"cease and desist": "notice", "ending": "termination"},
        prefix_aliases={"amend": "amend_merger"},
    )
    assert vocabulary.fold("Amendment No. 2") == "amend_merger"  # prefix alias first
    assert vocabulary.fold("Notice") == "notice"  # exact
    assert vocabulary.fold("ending") == "termination"  # whole alias
    assert vocabulary.fold("formal cease-and-desist letter") == "notice"  # alias of 8+ chars inside
    assert vocabulary.fold("my ending note") == ""  # alias under 8 chars is not searched inside
    assert vocabulary.fold("noticeofdefault") == "notice"  # token prefix
    assert vocabulary.fold("earlytermination") == "termination"  # token of 4+ chars inside
    assert vocabulary.fold("form 10k filing") == "10k"  # short token led by a digit inside
    assert vocabulary.fold("other") == "other"  # the unknown token only matches whole
    assert vocabulary.fold("others") == ""
    assert vocabulary.normalize("unrelated") == "other"


def test_inventory_alias_to_a_token_of_another_vocabulary_folds_to_nothing():
    """An alias target outside this vocabulary's tokens is not returned."""
    vocabulary = LabelVocabulary(
        tokens=("notice",), fold_method="inventory", aliases={"fnol": "claim_filing"}
    )
    assert vocabulary.fold("FNOL") == ""


def test_inventory_ties_go_to_the_longest_then_the_earliest():
    """Of two matching tokens the longer compact form wins; of equal length the earlier listed one."""
    vocabulary = LabelVocabulary(tokens=("lease", "release", "other"), fold_method="inventory")
    assert vocabulary.fold("xreleasex") == "release"
    tied = LabelVocabulary(tokens=("abcd", "wxyz", "other"), fold_method="inventory")
    assert tied.fold("_wxyz_abcd_") == "wxyz"  # a token prefix is tried before any substring
    assert tied.fold("_q_wxyz_abcd_") == "abcd"
    reversed_ties = LabelVocabulary(tokens=("wxyz", "abcd", "other"), fold_method="inventory")
    assert reversed_ties.fold("_q_wxyz_abcd_") == "wxyz"


def test_catalog_normalize_by_alias_label_and_label_prefix():
    """Catalog normalization: alias, whole label, first eight label characters; None and blanks are
    unknown."""
    vocabulary = LabelVocabulary(
        tokens=("license", "supply", "other"),
        fold_method="catalog",
        aliases={"ip license": "license"},
        catalog=(CatalogEntry("license", "License Agreement"), CatalogEntry("supply", "Supply Agreement")),
    )
    assert vocabulary.normalize("IP-License") == "license"
    assert vocabulary.normalize("License Agreement") == "license"
    assert vocabulary.normalize("Supply Agreements (amended)") == "supply"
    assert vocabulary.normalize("Supply") == "supply"  # the catalog key itself
    assert vocabulary.normalize("Suppl") == "other"  # shorter than the eight-character label prefix
    assert vocabulary.normalize(None) == "other" and vocabulary.normalize("  ") == "other"
    assert vocabulary.fold("Supply Agreement") == "supply" and vocabulary.fold("whatever") == ""
    assert vocabulary.fold("other") == "other"  # exact token match comes before the catalog


def test_catalog_normalize_recognises_a_token_written_with_case_or_punctuation():
    """A catalog key with capitals or punctuation is a valid token. When neither its aliases nor its label
    prefix match, normalize still finds it by its compact form, as fold does, instead of the unknown token."""
    vocabulary = LabelVocabulary(
        tokens=("Co-Op", "supply", "other"),
        fold_method="catalog",
        catalog=(CatalogEntry("Co-Op", "Cooperative Agreement"), CatalogEntry("supply", "Supply Agreement")),
    )
    for surface in ("Co-Op", "co op", "COOP"):
        assert vocabulary.fold(surface) == "Co-Op"
        assert vocabulary.normalize(surface) == "Co-Op", surface
    assert vocabulary.normalize("Cooperative Agreement") == "Co-Op"  # the label step, as before
    assert vocabulary.normalize("Supply Agreement") == "supply"
    assert vocabulary.normalize("coop agreement") == "other"  # not a token's compact form, no label prefix
    assert vocabulary.normalize("barter") == "other"


def test_catalog_normalize_tries_the_compact_form_only_after_the_label_step():
    """The compact-form lookup is the normalizer's last step: a value whose compact form is one token's, but
    which another entry's label names, goes to that entry, as the old Dojo's label step did. Here ``Co-Op``
    folds to the token ``Co-Op`` but normalizes to ``coopx``, whose label is "Coop"."""
    mapping = minimal_profile_mapping()
    mapping["classes"][0]["fields"]["coverage_tier"] = {
        "type": "label",
        "vocabulary": {
            "tokens": ["coopx", "Co-Op", "other"],
            "fold": "catalog",
            "catalog": [
                {"key": "coopx", "label": "Coop"},
                {"key": "Co-Op", "label": "Cooperative Agreement"},
            ],
        },
    }
    vocabulary = load_scoring_profile(mapping).label_vocabulary("auto_policy_declaration", "coverage_tier")
    assert vocabulary is not None
    assert vocabulary.fold("Co-Op") == "Co-Op"
    assert vocabulary.normalize("Co-Op") == "coopx"


@given(value=label_surfaces)
def test_catalog_normalize_agrees_with_fold_when_no_label_names_another_token(value):
    """On this vocabulary, where no catalog label starts like another entry's token, normalize returns
    whatever token the catalog fold finds. Not a general law: a label can claim a token's compact form first
    (see ``test_catalog_normalize_tries_the_compact_form_only_after_the_label_step``)."""
    vocabulary = LabelVocabulary(
        tokens=("Co-Op", "IP", "other"),
        fold_method="catalog",
        catalog=(CatalogEntry("Co-Op", "Cooperative Agreement"), CatalogEntry("IP", "Intellectual Property")),
    )
    for surface in (value, "Co-Op", "ip", "I.P."):
        folded = vocabulary.fold(surface)
        if folded and folded != vocabulary.unknown_token:
            assert vocabulary.normalize(surface) == folded, surface


def test_allowed_set_prefers_the_raw_spelling_then_the_compact_form():
    """Allowed-set normalization: the stripped raw value, else the first token with the same compact form."""
    vocabulary = LabelVocabulary(tokens=("e_mail", "letter", "other"), fold_method="allowed_set")
    assert vocabulary.normalize(" letter ") == "letter"
    assert vocabulary.normalize("E-Mail") == "e_mail"
    assert vocabulary.normalize("fax") == "other"
    assert vocabulary.normalize(None) == "other"
    assert vocabulary.fold("fax") == ""


@pytest.mark.parametrize("method", ["exact", "aliases", "inventory", "catalog", "allowed_set"])
@given(value=label_surfaces)
def test_fold_and_normalize_stay_inside_the_vocabulary(method, value):
    """fold gives a token or ""; normalize gives a token or the unknown token; neither raises."""
    catalog = (CatalogEntry("license", "License Agreement"),) if method == "catalog" else ()
    aliases = {"ip license": "license"} if method in ("aliases", "inventory", "catalog") else {}
    vocabulary = LabelVocabulary(
        tokens=("license", "notice", "other"), fold_method=method, aliases=aliases, catalog=catalog
    )
    assert vocabulary.fold(value) in {"", *vocabulary.tokens}
    assert vocabulary.normalize(value) in set(vocabulary.tokens) | {vocabulary.unknown_token}


def test_every_mailroom_vocabulary_is_closed_and_has_fixed_points(mailroom_profile):
    """In the mailroom profile, every token folds and normalizes to itself."""
    checked = 0
    for where, vocabulary in _all_vocabularies(mailroom_profile):
        for token in vocabulary.tokens:
            assert vocabulary.fold(token) == token, (where, token)
            assert vocabulary.normalize(token) == token, (where, token)
            checked += 1
    assert checked == 148


@given(value=label_surfaces)
def test_mailroom_folds_stay_inside_their_vocabularies(value, mailroom_profile):
    """For every mailroom vocabulary, any surface folds into the vocabulary or to nothing."""
    for where, vocabulary in _all_vocabularies(mailroom_profile):
        assert vocabulary.fold(value) in {"", *vocabulary.tokens}, where


def test_mailroom_intent_aliases_and_merger_prefix(mailroom_profile):
    """Examples from the old intent inventory survive the move to YAML."""
    merger = mailroom_profile.label_vocabulary("merger_agreement", "intent")
    assert merger.fold("Amendment to the Merger Agreement") == "amend_merger"
    assert mailroom_profile.canonical_label("insurance_claim", "claim_type", " Auto ") == "auto"
    assert mailroom_profile.canonical_label("insurance_claim", "claim_type", "spaceship") == ""
    assert mailroom_profile.canonical_label("insurance_claim", "not_a_field", "auto") == ""
    assert mailroom_profile.normalize_subclass("contract", "Supply Agreement") == "supply"


def test_vocabularies_are_values():
    """Equal inputs make equal, hashable vocabularies; a different order is a different vocabulary."""
    first = LabelVocabulary(tokens=["a", "b"], aliases={"x": "a"})
    assert first == LabelVocabulary(tokens=("a", "b"), aliases={"x": "a"})
    assert hash(first) == hash(LabelVocabulary(tokens=("a", "b"), aliases={"x": "a"}))
    assert first != LabelVocabulary(tokens=("b", "a"), aliases={"x": "a"})


def test_vocabularies_with_aliases_in_another_order_differ_when_the_order_decides():
    """Alias order decides ties (here two prefixes compacting to "ab"), so it is part of equality and hash."""
    tokens = ("alpha", "beta", "other")
    first = LabelVocabulary(
        tokens=tokens, fold_method="inventory", prefix_aliases={"a-b": "alpha", "ab": "beta"}
    )
    second = LabelVocabulary(
        tokens=tokens, fold_method="inventory", prefix_aliases={"ab": "beta", "a-b": "alpha"}
    )
    assert (first.fold("ab memo"), second.fold("ab memo")) == ("alpha", "beta")
    assert first != second
    reordered_aliases = LabelVocabulary(tokens=tokens, aliases={"y": "beta", "x": "alpha"})
    assert reordered_aliases != LabelVocabulary(tokens=tokens, aliases={"x": "alpha", "y": "beta"})
    same = LabelVocabulary(
        tokens=tokens, fold_method="inventory", prefix_aliases={"a-b": "alpha", "ab": "beta"}
    )
    assert first == same and hash(first) == hash(same)
