from __future__ import annotations

import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailroom_common.errors import RegistryError
from mailroom_common.registry import Registry


def test_register_direct_and_as_decorator() -> None:
    metrics: Registry[Callable[[int], int]] = Registry("metric")

    @metrics.register("double")
    def double(x: int) -> int:
        return 2 * x

    def square(x: int) -> int:
        return x * x

    assert metrics.register("square", square) is square
    assert double(2) == 4  # the decorator returns the function unchanged
    assert metrics.get("double") is double
    assert metrics.names() == ["double", "square"]
    assert list(metrics) == ["double", "square"]
    assert metrics.items() == [("double", double), ("square", square)]
    assert "double" in metrics and "cube" not in metrics
    assert len(metrics) == 2


def test_duplicate_name_names_kind_and_both_objects() -> None:
    reg: Registry[str] = Registry("backend")
    reg.register("local", "first-object")
    with pytest.raises(RegistryError) as info:
        reg.register("local", "second-object")
    assert info.value.message == "duplicate backend name 'local'"
    assert info.value.context == {
        "kind": "backend",
        "name": "local",
        "existing": "'first-object'",
        "new": "'second-object'",
    }
    assert reg.get("local") == "first-object"


def test_duplicate_via_decorator_is_refused_too() -> None:
    reg: Registry[Callable[[], None]] = Registry("node")
    reg.register("sorter", lambda: None)
    with pytest.raises(RegistryError, match="duplicate node name 'sorter'"):

        @reg.register("sorter")
        def sorter() -> None: ...


def test_replace_overwrites() -> None:
    reg: Registry[int] = Registry("schema")
    reg.register("a", 1)
    reg.register("a", 2, replace=True)
    assert reg.get("a") == 2


def test_missing_name_lists_available_and_suggests() -> None:
    reg: Registry[int] = Registry("document class")
    for name in ("invoice", "insurance_claim", "lease"):
        reg.register(name, 0)
    with pytest.raises(RegistryError) as info:
        reg.get("invoce")
    assert info.value.message == "unknown document class 'invoce'"
    assert info.value.context == {
        "kind": "document class",
        "name": "invoce",
        "available": ["insurance_claim", "invoice", "lease"],
        "did_you_mean": ["invoice"],
    }


def test_registering_none_stores_none_instead_of_returning_a_decorator() -> None:
    reg: Registry[int | None] = Registry("metric")
    assert reg.register("f1", None) is None
    assert "f1" in reg
    assert reg.get("f1") is None
    with pytest.raises(RegistryError, match="duplicate metric name 'f1'"):
        reg.register("f1", None)


@pytest.mark.parametrize("bad", [None, 1, b"f1"])
def test_lookup_with_a_non_string_name_is_a_registry_error(bad: object) -> None:
    reg: Registry[int] = Registry("metric")
    reg.register("f1", 1)
    with pytest.raises(RegistryError, match="metric name must be a string") as info:
        reg.get(bad)  # type: ignore[arg-type]
    assert info.value.context == {"kind": "metric", "name": bad}


def test_missing_name_in_empty_registry() -> None:
    with pytest.raises(RegistryError) as info:
        Registry[int]("metric").get("f1")
    assert info.value.context["available"] == []
    assert info.value.context["did_you_mean"] == []


@pytest.mark.parametrize("bad", ["", "Upper", "_lead", "-lead", ".lead", "has space", "slash/name", "é"])
def test_invalid_names_are_refused(bad: str) -> None:
    reg: Registry[int] = Registry("metric")
    with pytest.raises(RegistryError, match="invalid metric name") as info:
        reg.register(bad, 1)
    assert info.value.context == {"kind": "metric", "name": bad}
    assert len(reg) == 0


@given(st.from_regex(r"[a-z0-9][a-z0-9_.-]{0,30}", fullmatch=True))
def test_property_every_slug_round_trips(name: str) -> None:
    reg: Registry[str] = Registry("metric")
    reg.register(name, name)
    assert reg.get(name) == name
    assert reg.names() == [name]


def test_freeze_refuses_later_registration_but_allows_lookup() -> None:
    reg: Registry[int] = Registry("metric")
    reg.register("f1", 1)
    reg.freeze()
    reg.freeze()  # idempotent
    assert reg.frozen
    with pytest.raises(RegistryError, match="metric registry is frozen") as info:
        reg.register("f2", 2)
    assert info.value.context == {"kind": "metric", "name": "f2"}
    with pytest.raises(RegistryError, match="frozen"):
        reg.register("f1", 3, replace=True)
    assert reg.get("f1") == 1
    assert "frozen" in repr(reg)


def test_registries_are_independent() -> None:
    first: Registry[int] = Registry("metric")
    second: Registry[int] = Registry("metric")
    first.register("f1", 1)
    first.freeze()
    second.register("f1", 2)
    assert (first.get("f1"), second.get("f1")) == (1, 2)


def test_concurrent_registration_of_one_name_admits_exactly_one() -> None:
    reg: Registry[int] = Registry("node")
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        barrier.wait(timeout=5)
        try:
            reg.register("shared", n)
            result = "ok"
        except RegistryError:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(outcomes) == ["ok"] + ["refused"] * 7


# --- entry points ---------------------------------------------------------------------------


def make_distribution(
    root: Path, dist: str, group: str, entries: dict[str, str], modules: dict[str, str]
) -> None:
    """Create a real installed-looking distribution in ``root`` (a .dist-info plus modules)."""
    info = root / f"{dist}-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1\n", encoding="utf-8")
    lines = "".join(f"{name} = {target}\n" for name, target in entries.items())
    (info / "entry_points.txt").write_text(f"[{group}]\n{lines}", encoding="utf-8")
    for module, source in modules.items():
        (root / f"{module}.py").write_text(source, encoding="utf-8")


@pytest.fixture
def plugin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, str, str]]:
    """A unique entry-point group and module prefix, with tmp_path on sys.path."""
    tag = uuid.uuid4().hex[:8]
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path, f"mailroom_test_{tag}.metrics", f"plug_{tag}"
    for name in [m for m in sys.modules if m.startswith(f"plug_{tag}")]:
        del sys.modules[name]


def test_load_entry_points_registers_in_name_order(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root,
        f"{prefix}_dist",
        group,
        {"recall": f"{prefix}_mod:recall", "precision": f"{prefix}_mod:precision"},
        {f"{prefix}_mod": "def recall():\n    return 'r'\n\ndef precision():\n    return 'p'\n"},
    )
    reg: Registry[Callable[[], str]] = Registry("metric")
    assert reg.load_entry_points(group) == ["precision", "recall"]
    assert reg.get("recall")() == "r"
    assert reg.get("precision")() == "p"


def test_load_entry_points_with_no_plugins_is_empty(plugin_env: tuple[Path, str, str]) -> None:
    _, group, _ = plugin_env
    reg: Registry[object] = Registry("metric")
    assert reg.load_entry_points(group) == []
    assert len(reg) == 0


def test_plugin_import_failure_is_reported_with_cause(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root,
        f"{prefix}_dist",
        group,
        {"good": f"{prefix}_good:metric", "broken": f"{prefix}_broken:metric"},
        {
            f"{prefix}_good": "def metric():\n    return 1\n",
            f"{prefix}_broken": "raise RuntimeError('plug-in exploded on import')\n",
        },
    )
    reg: Registry[object] = Registry("metric")
    with pytest.raises(RegistryError) as info:
        reg.load_entry_points(group)
    assert (
        info.value.message
        == "metric plug-in 'broken' failed to load: RuntimeError: plug-in exploded on import"
    )
    assert info.value.context == {
        "kind": "metric",
        "group": group,
        "entry_point": f"broken = {prefix}_broken:metric",
    }
    assert isinstance(info.value.__cause__, RuntimeError)


def test_plugin_missing_attribute_is_reported(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root, f"{prefix}_dist", group, {"ghost": f"{prefix}_mod:nope"}, {f"{prefix}_mod": "x = 1\n"}
    )
    with pytest.raises(RegistryError, match="'ghost' failed to load: AttributeError") as info:
        Registry[object]("metric").load_entry_points(group)
    assert isinstance(info.value.__cause__, AttributeError)


def test_plugin_with_invalid_or_duplicate_name_is_refused(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root, f"{prefix}_dist", group, {"Bad_Name": f"{prefix}_mod:x"}, {f"{prefix}_mod": "x = 1\n"}
    )
    with pytest.raises(RegistryError, match="invalid metric name") as info:
        Registry[object]("metric").load_entry_points(group)
    assert info.value.context == {
        "kind": "metric",
        "name": "Bad_Name",
        "group": group,
        "entry_point": f"Bad_Name = {prefix}_mod:x",
    }


def test_a_failing_plugin_leaves_nothing_registered(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root,
        f"{prefix}_dist",
        group,
        {"alpha": f"{prefix}_good:metric", "zeta": f"{prefix}_broken:metric"},
        {
            f"{prefix}_good": "def metric():\n    return 1\n",
            f"{prefix}_broken": "raise ImportError('missing optional dependency')\n",
        },
    )
    reg: Registry[object] = Registry("metric")
    reg.register("builtin", 0)
    with pytest.raises(RegistryError, match="'zeta' failed to load"):
        reg.load_entry_points(group)
    assert reg.names() == ["builtin"]


def test_a_plugin_name_clash_leaves_nothing_registered(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root,
        f"{prefix}_dist",
        group,
        {"alpha": f"{prefix}_mod:x", "zeta": f"{prefix}_mod:x"},
        {f"{prefix}_mod": "x = 1\n"},
    )
    reg: Registry[object] = Registry("metric")
    reg.register("zeta", "builtin")
    with pytest.raises(RegistryError, match="duplicate metric name 'zeta'") as info:
        reg.load_entry_points(group)
    assert info.value.context["group"] == group
    assert reg.names() == ["zeta"]


def test_system_exit_from_a_plugin_import_is_wrapped(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root,
        f"{prefix}_dist",
        group,
        {"quits": f"{prefix}_mod:x"},
        {f"{prefix}_mod": "raise SystemExit(2)\n"},
    )
    reg: Registry[object] = Registry("metric")
    with pytest.raises(RegistryError, match="'quits' failed to load: SystemExit: 2") as info:
        reg.load_entry_points(group)
    assert isinstance(info.value.__cause__, SystemExit)
    assert len(reg) == 0


def test_plugin_clashing_with_a_builtin_registration_is_refused(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(
        root, f"{prefix}_dist", group, {"f1": f"{prefix}_mod:x"}, {f"{prefix}_mod": "x = 'plugin'\n"}
    )
    reg: Registry[object] = Registry("metric")
    reg.register("f1", "builtin")
    with pytest.raises(RegistryError, match="duplicate metric name 'f1'") as info:
        reg.load_entry_points(group)
    assert info.value.context["existing"] == "'builtin'"
    assert info.value.context["new"] == "'plugin'"


def test_frozen_registry_refuses_plugins(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(root, f"{prefix}_dist", group, {"f1": f"{prefix}_mod:x"}, {f"{prefix}_mod": "x = 1\n"})
    reg: Registry[object] = Registry("metric")
    reg.freeze()
    with pytest.raises(RegistryError, match="frozen") as info:
        reg.load_entry_points(group)
    assert info.value.context == {"kind": "metric", "group": group}
    assert f"{prefix}_mod" not in sys.modules  # the plug-in was never imported


def test_two_plugins_with_one_name_are_refused(plugin_env: tuple[Path, str, str]) -> None:
    root, group, prefix = plugin_env
    make_distribution(root, f"{prefix}_one", group, {"f1": f"{prefix}_a:x"}, {f"{prefix}_a": "x = 'a'\n"})
    make_distribution(root, f"{prefix}_two", group, {"f1": f"{prefix}_b:x"}, {f"{prefix}_b": "x = 'b'\n"})
    reg: Registry[object] = Registry("metric")
    with pytest.raises(RegistryError, match="duplicate metric name 'f1'") as info:
        reg.load_entry_points(group)
    assert (info.value.context["existing"], info.value.context["new"]) == ("'a'", "'b'")
    assert len(reg) == 0
