"""Review policies: rule branches, first-match order, catalog lookup, validation, hash, sampling."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import pytest
import yaml
from desk_testkit import POLICY, policy, policy_dict, write_yaml

from mailroom_desk import OUTAGE_TOKENS, SPOT_CHECK, PolicyError, load_policy, parse_policy
from mailroom_desk.policy import Sample, is_outage, pattern_matches, unknown_classes
from mailroom_labels.profile import load_profile

REPO = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = REPO / "config" / "review" / "default.yaml"


def problems_of(raw: Any, path: Path | None = None) -> tuple[str, ...]:
    with pytest.raises(PolicyError) as info:
        parse_policy(raw, path)
    return info.value.problems


def load_problems(path: Path) -> tuple[str, ...]:
    with pytest.raises(PolicyError) as info:
        load_policy(path)
    return info.value.problems


# --- routing: every rule branch, first match wins -------------------------------------


def test_cause_in_branch_and_then_overrides_queue_and_role() -> None:
    r = policy().route(["flag_high"])
    assert (r.rule, r.queue, r.required_role) == ("high_to_senior", "senior", "senior")
    # The rule sets neither priority nor SLA: both come from the catalog.
    assert (r.priority, r.sla_hours) == ("high", 4.0)


def test_cause_in_matches_any_of_several_request_causes() -> None:
    assert policy().route(["flag_low", "flag_high"]).rule == "high_to_senior"


def test_all_conditions_of_a_rule_must_hold() -> None:
    p = policy()
    hit = p.route(["flag_class"], "kind_b", 0.49)
    assert (hit.rule, hit.priority, hit.sla_hours, hit.queue, hit.prefill_ref) == (
        "class_and_low_conf", "high", 1.0, "combo", "prefill-combo"
    )
    # Drop any one condition and the combined rule no longer matches: the next match wins.
    assert p.route(["flag_normal"], "kind_b", 0.49).rule == "class_only"  # cause_in fails
    assert p.route(["flag_class"], "kind_a", 0.49).rule == "class_only"  # class_in fails
    assert p.route(["flag_class"], "kind_b", 0.5).rule == "class_only"  # not strictly below


def test_class_in_branch() -> None:
    p = policy()
    assert p.route(["flag_normal"], "kind_a").rule == "class_only"
    assert p.route(["flag_normal"], "kind_a").queue == "by_class"
    assert p.route(["flag_normal"], "kind_c").rule == "default"
    assert p.route(["flag_normal"], None).rule == "default"  # no hint never matches


def test_confidence_below_branch_is_strict_and_needs_a_hint() -> None:
    p = policy()
    hit = p.route(["flag_normal"], None, 0.29)
    assert (hit.rule, hit.priority, hit.sla_hours) == ("low_conf", "normal", 12.0)
    assert p.route(["flag_normal"], None, 0.3).rule == "default"  # equal is not below
    assert p.route(["flag_normal"], None, None).rule == "default"
    assert p.route(["flag_normal"], None, 0.0).rule == "low_conf"


def test_pattern_in_cause_in_matches_a_segment_each() -> None:
    p = policy()
    assert p.route(["slot:x:y"]).rule == "slots"
    assert p.route(["slot:exact:one"]).rule == "slots"


def test_default_rule_takes_catalog_priority_and_default_queue() -> None:
    r = policy().route(["flag_normal", "flag_low"])
    assert r.rule == "default"
    # The most urgent priority and the shortest SLA among the causes.
    assert (r.priority, r.sla_hours, r.queue, r.required_role, r.prefill_ref) == ("normal", 24.0, "main", None, None)
    r = policy().route(["flag_low", "flag_conf"])
    assert (r.priority, r.sla_hours) == ("low", 48.0)


def test_first_match_wins_and_order_matters() -> None:
    both = ["flag_high", "slot:a:b"]
    assert policy().route(both).rule == "high_to_senior"
    raw = policy_dict()
    rules = raw["rules"]
    slots = next(r for r in rules if r["name"] == "slots")
    rules.remove(slots)
    rules.insert(0, slots)
    assert parse_policy(raw).route(both).rule == "slots"


def test_exact_catalog_entry_wins_over_a_pattern() -> None:
    p = policy()
    assert p.cause_spec("slot:exact:one").priority == "high"
    assert p.cause_spec("slot:exact:two").token == "slot:*:*"
    assert p.cause_spec("slot:exact") is None  # a pattern matches whole segments only
    assert p.cause_spec("nope") is None
    assert p.route(["slot:exact:one"]).sla_hours == 2.0


def test_route_refuses_unknown_or_no_causes() -> None:
    with pytest.raises(KeyError):
        policy().route(["not_listed"])
    with pytest.raises(ValueError):
        policy().route([])


def test_pattern_matches() -> None:
    assert pattern_matches("a:*:c", "a:b:c")
    assert not pattern_matches("a:*:c", "a:b:d")
    assert not pattern_matches("a:*", "a:b:c")
    assert pattern_matches("a", "a") and not pattern_matches("a", "b")


def test_shipped_default_policy_loads_and_routes() -> None:
    p = load_policy(DEFAULT_POLICY)
    assert p.name == "default" and p.rules[-1].when.is_default
    assert p.route(["boss_conflict"]).queue == "senior"
    assert p.route([SPOT_CHECK]).queue == "qa"
    r = p.route(["decision_point:slot_a:judge_miss"])
    assert (r.rule, r.priority, r.sla_hours, r.queue) == ("default", "low", 72.0, "main")
    assert 0 < p.sample.rate < 1


# --- validation ---------------------------------------------------------------------


def test_missing_default_rule_is_refused() -> None:
    raw = policy_dict()
    raw["rules"] = raw["rules"][:-1]
    assert any("final rule and must be the default" in p for p in problems_of(raw))


def test_default_rule_before_the_end_is_refused() -> None:
    raw = policy_dict()
    raw["rules"].insert(0, {"name": "early_default", "then": {}})
    assert any("no conditions but is not the final rule" in p for p in problems_of(raw))


def test_empty_when_counts_as_no_conditions() -> None:
    raw = policy_dict()
    raw["rules"].insert(0, {"name": "empty_when", "when": {}, "then": {}})
    assert any("no conditions but is not the final rule" in p for p in problems_of(raw))


def test_duplicate_keys_are_refused(tmp_path: Path) -> None:
    text = DEFAULT_POLICY.read_text(encoding="utf-8")
    top = tmp_path / "default.yaml"
    top.write_text(text + "\nname: default\n", encoding="utf-8")
    assert any("duplicate key 'name'" in p for p in load_problems(top))
    nested = tmp_path / "nested.yaml"
    nested.write_text(
        "name: nested\ncauses:\n  - {token: a, token: b, description: x, priority: low, sla_hours: 1}\n"
        "rules:\n  - name: default\n",
        encoding="utf-8",
    )
    assert any("duplicate key 'token'" in p for p in load_problems(nested))


def test_unhashable_key_and_invalid_yaml_and_unreadable_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("? [a, b]\n: 1\n", encoding="utf-8")
    assert any("unhashable key" in p for p in load_problems(bad))
    bad.write_text("name: [unclosed\n", encoding="utf-8")
    assert any("not valid YAML" in p for p in load_problems(bad))
    assert any("cannot read" in p for p in load_problems(tmp_path / "missing.yaml"))
    bad.write_bytes(b"name: \xff\xfe\n")
    assert any("cannot read" in p for p in load_problems(bad))
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    assert load_problems(bad) == ("a policy must be a mapping",)


def test_python_tags_are_not_constructed(tmp_path: Path) -> None:
    bad = tmp_path / "evil.yaml"
    bad.write_text("name: !!python/object/apply:os.getcwd []\n", encoding="utf-8")
    assert any("not valid YAML" in p for p in load_problems(bad))


@pytest.mark.parametrize(
    "where, mutate, expect",
    [
        ("top", lambda r: r.update(extra=1), "the policy has unknown key 'extra'"),
        ("cause", lambda r: r["causes"][0].update(colour="red"), "causes[0] has unknown key 'colour'"),
        ("rule", lambda r: r["rules"][0].update(note="x"), "rules[0] has unknown key 'note'"),
        ("when", lambda r: r["rules"][0]["when"].update(doc_type="x"), "has unknown key 'doc_type'"),
        ("then", lambda r: r["rules"][0]["then"].update(colour="x"), "has unknown key 'colour'"),
        ("sample", lambda r: r["sample"].update(every=3), "sample has unknown key 'every'"),
    ],
)
def test_unknown_keys_are_refused_at_every_level(where: str, mutate: Any, expect: str) -> None:
    raw = policy_dict()
    mutate(raw)
    assert any(expect in p for p in problems_of(raw)), where


@pytest.mark.parametrize("token", ["Flag", "flag-x", "flag:", ":flag", "flag::x", "", "flag x", "flag\n", 7, None])
def test_bad_cause_tokens_are_refused(token: Any) -> None:
    raw = policy_dict()
    raw["causes"][0]["token"] = token
    assert any("causes[0].token is not a token or pattern" in p for p in problems_of(raw))


@pytest.mark.parametrize(
    "path, value, expect",
    [
        (("causes", 0, "sla_hours"), "4", "sla_hours must be a number of hours"),
        (("causes", 0, "sla_hours"), True, "sla_hours must be a number of hours"),
        (("causes", 0, "sla_hours"), 0, "sla_hours must be a number of hours"),
        (("causes", 0, "sla_hours"), -1, "sla_hours must be a number of hours"),
        (("causes", 0, "sla_hours"), math.inf, "sla_hours must be a number of hours"),
        (("causes", 0, "sla_hours"), math.nan, "sla_hours must be a number of hours"),
        (("causes", 0, "sla_hours"), 24 * 366 + 1, "sla_hours must be a number of hours"),
        (("causes", 0, "priority"), "urgent", "priority must be one of high, normal, low"),
        (("causes", 0, "description"), "  ", "description must be non-empty text"),
        (("rules", 1, "when", "confidence_below"), 0, "confidence_below must be a number in (0, 1]"),
        (("rules", 1, "when", "confidence_below"), 1.5, "confidence_below must be a number in (0, 1]"),
        (("rules", 1, "when", "confidence_below"), "0.5", "confidence_below must be a number in (0, 1]"),
        (("rules", 1, "when", "class_in"), [], "class_in must be a non-empty list"),
        (("rules", 1, "when", "class_in"), "kind_b", "class_in must be a non-empty list"),
        (("rules", 1, "when", "class_in"), ["kind_b", "kind_b"], "lists 'kind_b' twice"),
        (("rules", 1, "when", "class_in"), [" kind_b"], "not a name"),
        (("rules", 0, "when", "cause_in"), ["flag_high", 3], "not a token or pattern"),
        (("rules", 0, "when", "cause_in"), ["never_listed"], "matches no cause in the catalog"),
        (("rules", 0, "then", "queue"), "Senior", "queue is not a token"),
        (("rules", 0, "then", "required_role"), 1, "required_role is not a token"),
        (("rules", 1, "then", "prefill_ref"), "", "prefill_ref must be a name"),
        (("rules", 0, "then", "priority"), "high", "must set priority and sla_hours together"),
        (("rules", 0, "name"), "Bad Name", "name is not a token"),
        (("rules", 1, "name"), "high_to_senior", "is used twice"),
        (("rules", 0, "when"), ["flag_high"], "when must be a mapping"),
        (("rules", 0, "then"), "senior", "then must be a mapping"),
        (("sample", "rate"), 1.5, "sample.rate must be a number in [0, 1]"),
        (("sample", "rate"), "0.1", "sample.rate must be a number in [0, 1]"),
        (("sample", "seed"), 1.5, "sample.seed must be text or an integer"),
        (("sample", "seed"), True, "sample.seed must be text or an integer"),
        (("sample", "seed"), "", "sample.seed must be set when sample.rate > 0"),
        (("default_queue",), "Main", "default_queue is not a token"),
        (("description",), "", "description must be non-empty text"),
        (("name",), "Synthetic", "name is not a token"),
    ],
)
def test_bad_values_and_types_are_refused(path: tuple, value: Any, expect: str) -> None:
    raw = policy_dict()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert any(expect in p for p in problems_of(raw)), problems_of(raw)


def test_missing_keys_and_non_list_sections() -> None:
    assert set(problems_of({})) == {"missing key 'causes'", "missing key 'name'", "missing key 'rules'"}
    raw = policy_dict(causes=[], rules=[])
    found = problems_of(raw)
    assert "causes must be a non-empty list" in found
    assert "rules must be a non-empty list ending in a default rule" in found
    raw = policy_dict()
    del raw["causes"][0]["sla_hours"]
    raw["causes"][1] = "flag_normal"
    raw["rules"][0] = "high_to_senior"
    raw["sample"] = [0.1]
    found = problems_of(raw)
    assert "causes[0] is missing 'sla_hours'" in found
    assert "causes[1] must be a mapping" in found
    assert "rules[0] must be a mapping" in found
    assert "sample must be a mapping {rate, seed}" in found


def test_duplicate_cause_and_overlapping_patterns_are_refused() -> None:
    raw = policy_dict()
    raw["causes"].append(copy.deepcopy(raw["causes"][0]))
    assert any("'flag_high' is listed twice" in p for p in problems_of(raw))
    raw = policy_dict()
    raw["causes"].append({"token": "slot:a:*", "description": "x", "priority": "low", "sla_hours": 1})
    assert any("can match the same token" in p for p in problems_of(raw))
    # Patterns of different lengths never overlap.
    raw = policy_dict()
    raw["causes"].append({"token": "slot:*", "description": "x", "priority": "low", "sla_hours": 1})
    parse_policy(raw)


def test_sampling_needs_a_spot_check_cause() -> None:
    raw = policy_dict()
    raw["causes"] = [c for c in raw["causes"] if c["token"] != "spot_check"]
    raw["rules"] = [r for r in raw["rules"] if r["name"] != "sampled"]
    assert any("needs a 'spot_check' cause" in p for p in problems_of(raw))
    raw["sample"] = {"rate": 0, "seed": ""}
    parse_policy(raw)  # no sampling: no spot_check needed


@pytest.mark.parametrize("outage", sorted(OUTAGE_TOKENS))
def test_outage_tokens_are_refused_in_the_catalog_and_rules(outage: str) -> None:
    raw = policy_dict()
    raw["causes"].append({"token": outage, "description": "x", "priority": "high", "sla_hours": 1})
    assert any("names a provider outage" in p for p in problems_of(raw))
    raw = policy_dict()
    raw["causes"].append({"token": f"slot_b:{outage}", "description": "x", "priority": "high", "sla_hours": 1})
    assert any("names a provider outage" in p for p in problems_of(raw))
    raw = policy_dict()
    raw["rules"][0]["when"]["cause_in"] = [f"slot:x:{outage}"]
    assert any("a provider outage; outages are never reviewed" in p for p in problems_of(raw))
    assert is_outage(outage) and is_outage(f"a:{outage}:b") and not is_outage(f"{outage}_not")


def test_every_problem_is_listed_at_once() -> None:
    raw = policy_dict(extra=1, default_queue="Bad")
    raw["causes"][0]["priority"] = "urgent"
    raw["rules"] = raw["rules"][:-1]
    found = problems_of(raw)
    assert len(found) >= 3
    assert str(PolicyError(None, found)).startswith("invalid review policy: ")


def test_file_name_must_match_policy_name(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "other.yaml", POLICY)
    assert any("differs from the file name 'other'" in p for p in load_problems(path))
    good = write_yaml(tmp_path / "synthetic.yaml", POLICY)
    assert load_policy(good).hash == policy().hash


# --- the policy hash ----------------------------------------------------------------


def _reordered(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _reordered(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reordered(v) for v in value]
    return value


def test_hash_ignores_key_order_comments_and_number_format(tmp_path: Path) -> None:
    base = load_policy(DEFAULT_POLICY)
    raw = yaml.safe_load(DEFAULT_POLICY.read_text(encoding="utf-8"))
    for cause in raw["causes"]:
        cause["sla_hours"] = float(cause["sla_hours"])  # 4 -> 4.0
    path = tmp_path / "default.yaml"
    path.write_text("# no comments survive this\n" + yaml.safe_dump(_reordered(raw), sort_keys=False), encoding="utf-8")
    assert path.read_text() != DEFAULT_POLICY.read_text()
    assert load_policy(path).hash == base.hash
    assert parse_policy(_reordered(policy_dict())).hash == policy().hash


def _mutations() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    raw = policy_dict()
    for i, rule in enumerate(raw["rules"]):
        out.append((f"rules[{i}].name", lambda r, i=i: r["rules"][i].update(name=r["rules"][i]["name"] + "_x")))
        out.append((f"rules[{i}].then.queue", lambda r, i=i: r["rules"][i].setdefault("then", {}).update(queue="other_q")))
        out.append(
            (f"rules[{i}].then.required_role", lambda r, i=i: r["rules"][i].setdefault("then", {}).update(required_role="role_x"))
        )
        if rule.get("when"):
            out.append(
                (f"rules[{i}].when.class_in", lambda r, i=i: r["rules"][i]["when"].update(class_in=["kind_z"]))
            )
            out.append(
                (f"rules[{i}].when.confidence_below", lambda r, i=i: r["rules"][i]["when"].update(confidence_below=0.77))
            )
    for i in range(len(raw["causes"])):
        out.append((f"causes[{i}].sla_hours", lambda r, i=i: r["causes"][i].update(sla_hours=r["causes"][i]["sla_hours"] + 1)))
        out.append((f"causes[{i}].priority", lambda r, i=i: r["causes"][i].update(
            priority="high" if r["causes"][i]["priority"] != "high" else "low")))
        out.append((f"causes[{i}].description", lambda r, i=i: r["causes"][i].update(description="Changed.")))
    out += [
        ("rules swapped", lambda r: r["rules"].insert(0, r["rules"].pop(1))),
        ("rules[0].when.cause_in", lambda r: r["rules"][0]["when"].update(cause_in=["flag_normal"])),
        ("rules[1].then.priority+sla", lambda r: r["rules"][1]["then"].update(priority="low", sla_hours=2)),
        ("rules[1].then.prefill_ref", lambda r: r["rules"][1]["then"].update(prefill_ref="other")),
        ("sample.rate", lambda r: r["sample"].update(rate=0.2)),
        ("sample.seed", lambda r: r["sample"].update(seed="other-seed")),
        ("default_queue", lambda r: r.update(default_queue="other_main")),
        ("description", lambda r: r.update(description="Other.")),
        ("name", lambda r: r.update(name="synthetic_b")),
        ("causes reordered", lambda r: r["causes"].insert(0, r["causes"].pop(2))),
    ]
    return out


@pytest.mark.parametrize("label, mutate", _mutations(), ids=[m[0] for m in _mutations()])
def test_any_change_of_meaning_changes_the_hash(label: str, mutate: Any) -> None:
    raw = policy_dict()
    mutate(raw)
    assert raw != policy_dict(), label
    assert parse_policy(raw).hash != policy().hash


def test_hash_is_stable_across_loads() -> None:
    assert load_policy(DEFAULT_POLICY).hash == load_policy(DEFAULT_POLICY).hash
    assert len(policy().hash) == 64


# --- sampling -----------------------------------------------------------------------


def test_sampling_is_deterministic_and_seeded() -> None:
    a = Sample(rate=0.3, seed="s1")
    ids = [f"doc-{i}" for i in range(2000)]
    first = [a.selects(d) for d in ids]
    assert first == [Sample(rate=0.3, seed="s1").selects(d) for d in ids]
    other = [Sample(rate=0.3, seed="s2").selects(d) for d in ids]
    assert first != other  # the seed changes which documents are drawn


@pytest.mark.parametrize("rate", [0.02, 0.1, 0.5])
def test_sampling_rate_over_ten_thousand_ids(rate: float) -> None:
    # Binomial(10,000, rate): a five-sigma band. The ids and seed are fixed, so the count is
    # deterministic; the band only says the hash behaves like a fair draw.
    n = 10_000
    hits = sum(Sample(rate=rate, seed="synthetic-seed").selects(f"{i:064x}") for i in range(n))
    sigma = math.sqrt(n * rate * (1 - rate))
    assert abs(hits - n * rate) <= 5 * sigma, (hits, n * rate)


def test_sampling_boundaries_and_monotonicity() -> None:
    ids = [f"d{i}" for i in range(3000)]
    assert not any(Sample(rate=0.0, seed="s").selects(d) for d in ids)
    assert all(Sample(rate=1.0, seed="s").selects(d) for d in ids)
    low = {d for d in ids if Sample(rate=0.1, seed="s").selects(d)}
    high = {d for d in ids if Sample(rate=0.4, seed="s").selects(d)}
    assert low < high  # a higher rate keeps every document a lower one drew
    s = Sample(rate=0.1, seed="s")
    some = ids[0]
    exact = Sample(rate=s.fraction(some), seed="s")
    assert not exact.selects(some)  # strictly below the rate


def test_integer_seed_is_its_text() -> None:
    a = policy(sample={"rate": 0.2, "seed": 17})
    b = policy(sample={"rate": 0.2, "seed": "17"})
    assert a.hash == b.hash and a.sample == b.sample


# --- classes against a client profile ------------------------------------------------


def test_unknown_classes_lists_rule_classes_the_profile_lacks() -> None:
    profile = load_profile(REPO / "packages" / "labels" / "tests" / "fixtures" / "synthetic-profile.yaml")
    known = sorted(profile.classes)[0]
    raw = policy_dict()
    raw["rules"][2]["when"]["class_in"] = [known, "kind_missing"]
    assert unknown_classes(parse_policy(raw), profile) == ["kind_b", "kind_missing"]
