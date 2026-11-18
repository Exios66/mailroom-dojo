"""The reviewer registry: strict and open modes, token identity, roles, validation, hash."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from desk_testkit import write_yaml

from mailroom_desk import (
    RegistryError,
    RoleNotHeld,
    TokenRejected,
    UnknownReviewer,
    load_registry,
    parse_registry,
)
from mailroom_desk.reviewers import token_sha256

REPO = Path(__file__).resolve().parents[3]
SHIPPED = REPO / "config" / "review" / "reviewers.yaml"

REGISTRY: dict[str, Any] = {
    "mode": "strict",
    "reviewers": [
        {"id": "rv-a", "initials": "RVA", "roles": ["reviewer", "senior"], "token_sha256": token_sha256("token-a")},
        {"id": "rv-b", "initials": "RVB", "roles": ["qa"]},
    ],
}


def registry(**overrides: Any):
    raw = copy.deepcopy(REGISTRY)
    raw.update(overrides)
    return parse_registry(raw)


def problems_of(raw: Any) -> tuple[str, ...]:
    with pytest.raises(RegistryError) as info:
        parse_registry(raw)
    return info.value.problems


def test_strict_known_reviewer_with_token_is_authenticated() -> None:
    ref = registry().identify("rv-a", token="token-a")
    assert (ref.id, ref.role, ref.identity) == ("rv-a", "reviewer", "authenticated")
    assert registry().identify("rv-a", role="senior", token="token-a").role == "senior"


def test_strict_known_reviewer_without_token_is_asserted() -> None:
    ref = registry().identify("rv-b")
    assert (ref.id, ref.role, ref.identity) == ("rv-b", "qa", "asserted")


def test_wrong_token_or_token_without_a_registered_hash_is_rejected() -> None:
    with pytest.raises(TokenRejected):
        registry().identify("rv-a", token="token-b")
    with pytest.raises(TokenRejected):
        registry().identify("rv-b", token="anything")


def test_role_not_held_is_refused() -> None:
    with pytest.raises(RoleNotHeld) as info:
        registry().identify("rv-b", role="senior")
    assert (info.value.reviewer, info.value.role) == ("rv-b", "senior")


def test_strict_refuses_an_unknown_reviewer() -> None:
    with pytest.raises(UnknownReviewer):
        registry().identify("rv-unknown")


def test_open_accepts_an_unknown_reviewer_as_asserted() -> None:
    reg = registry(mode="open")
    ref = reg.identify("rv-unknown")
    assert (ref.id, ref.role, ref.identity) == ("rv-unknown", "reviewer", "asserted")
    assert reg.identify("rv-unknown", role="qa").role == "qa"
    # Known reviewers are still checked in open mode.
    assert reg.identify("rv-a", token="token-a").identity == "authenticated"
    with pytest.raises(RoleNotHeld):
        reg.identify("rv-b", role="senior")


def test_open_refuses_a_token_for_an_unknown_id_a_bad_id_or_a_bad_role() -> None:
    reg = registry(mode="open")
    with pytest.raises(TokenRejected):
        reg.identify("rv-unknown", token="token-a")
    for bad in ("RV-Upper", "", "a" * 65, "rv a", 5):
        with pytest.raises(UnknownReviewer):
            reg.identify(bad)  # type: ignore[arg-type]
    with pytest.raises(RoleNotHeld):
        reg.identify("rv-unknown", role="Not A Token")


def test_authenticate_finds_the_holder_of_a_token() -> None:
    reg = registry()
    assert reg.authenticate("token-a").id == "rv-a"
    assert reg.authenticate("token-a", role="senior").role == "senior"
    with pytest.raises(UnknownReviewer):
        reg.authenticate("token-b")


def test_shipped_example_registry_loads() -> None:
    reg = load_registry(SHIPPED)
    assert reg.mode == "strict"
    assert reg.identify("rv-example-a", token="example-token-a").identity == "authenticated"


@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda r: r.update(mode="lenient"), "mode must be 'strict' or 'open'"),
        (lambda r: r.pop("mode"), "mode must be 'strict' or 'open'"),
        (lambda r: r.update(extra=1), "unknown key 'extra'"),
        (lambda r: r.update(reviewers="rv-a"), "reviewers must be a list"),
        (lambda r: r.update(reviewers=[]), "a strict registry must list at least one reviewer"),
        (lambda r: r["reviewers"].append("rv-c"), "reviewers[2] must be a mapping"),
        (lambda r: r["reviewers"][0].update(email="x"), "reviewers[0] has unknown key 'email'"),
        (lambda r: r["reviewers"][0].pop("initials"), "reviewers[0] is missing 'initials'"),
        (lambda r: r["reviewers"][0].update(id="RV-A"), "reviewers[0].id must match"),
        (lambda r: r["reviewers"][0].update(id=7), "reviewers[0].id must match"),
        (lambda r: r["reviewers"][0].update(initials="r"), "initials must be 2-6 capital letters"),
        (lambda r: r["reviewers"][0].update(roles=[]), "roles must be a non-empty list"),
        (lambda r: r["reviewers"][0].update(roles="reviewer"), "roles must be a non-empty list"),
        (lambda r: r["reviewers"][0].update(roles=["Reviewer"]), "not a token"),
        (lambda r: r["reviewers"][0].update(roles=["qa", "qa"]), "lists a role twice"),
        (lambda r: r["reviewers"][0].update(token_sha256="ABC"), "token_sha256 must be 64 lowercase hex digits"),
        (lambda r: r["reviewers"][1].update(id="rv-a"), "reviewers[1].id is used by another reviewer"),
        (lambda r: r["reviewers"][1].update(initials="RVA"), "reviewers[1].initials is used by another reviewer"),
        (
            lambda r: r["reviewers"][1].update(token_sha256=r["reviewers"][0]["token_sha256"]),
            "reviewers[1].token_sha256 is used by another reviewer",
        ),
    ],
)
def test_bad_registries_are_refused(mutate: Any, expect: str) -> None:
    raw = copy.deepcopy(REGISTRY)
    mutate(raw)
    assert any(expect in p for p in problems_of(raw)), problems_of(raw)


def test_open_registry_may_be_empty_and_non_mapping_is_refused() -> None:
    assert parse_registry({"mode": "open", "reviewers": []}).reviewers == ()
    assert problems_of(["mode", "strict"]) == ("a reviewer registry must be a mapping",)


def test_registry_file_errors(tmp_path: Path) -> None:
    path = tmp_path / "reviewers.yaml"
    path.write_text("mode: strict\nmode: open\nreviewers: []\n", encoding="utf-8")
    with pytest.raises(RegistryError) as info:
        load_registry(path)
    assert any("duplicate key 'mode'" in p for p in info.value.problems)
    assert info.value.path == path
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "missing.yaml")


def test_registry_hash_ignores_order_of_keys_and_changes_with_meaning(tmp_path: Path) -> None:
    base = registry().hash
    flipped = {"reviewers": [dict(reversed(list(r.items()))) for r in REGISTRY["reviewers"]], "mode": "strict"}
    assert parse_registry(flipped).hash == base
    path = write_yaml(tmp_path / "reviewers.yaml", REGISTRY)
    path.write_text("# a comment\n" + path.read_text(), encoding="utf-8")
    assert load_registry(path).hash == base
    for mutate in (
        lambda r: r.update(mode="open"),
        lambda r: r["reviewers"][0].update(roles=["senior", "reviewer"]),
        lambda r: r["reviewers"][1].update(initials="RVC"),
        lambda r: r["reviewers"][1].update(token_sha256=token_sha256("token-b")),
        lambda r: r["reviewers"].reverse(),
    ):
        raw = yaml.safe_load(yaml.safe_dump(REGISTRY))
        mutate(raw)
        assert parse_registry(raw).hash != base


def test_hostile_sizes_are_registry_errors(tmp_path: Path) -> None:
    depth = 5000
    path = tmp_path / "reviewers.yaml"
    path.write_text("mode: strict\nreviewers: " + "[" * depth + "]" * depth + "\n", encoding="utf-8")
    with pytest.raises(RegistryError) as info:
        load_registry(path)
    assert any("nested too deeply" in p for p in info.value.problems)
    path.write_text("mode: strict\nreviewers:\n  - {id: rv-a, initials: RVA, roles: [" + "9" * 5000 + "]}\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        load_registry(path)
    deep: Any = []
    for _ in range(depth):
        deep = [deep]
    raw = copy.deepcopy(REGISTRY)
    raw["reviewers"][0]["roles"] = deep
    with pytest.raises(RegistryError) as info:
        parse_registry(raw)
    raw = copy.deepcopy(REGISTRY)
    raw["reviewers"][0]["roles"] = [10**5000]
    with pytest.raises(RegistryError) as info:
        parse_registry(raw)
    assert any("roles holds" in p and "not a token" in p for p in info.value.problems)
