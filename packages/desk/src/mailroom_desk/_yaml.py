"""Safe YAML loading that refuses duplicate keys (shared by the policy and registry loaders)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

__all__ = ["load_yaml_file"]


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping holding the same key twice (YAML keeps the last
    one silently, which would hide a copy-paste error in a rule)."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:  # an unhashable key (a list or mapping used as a key)
            raise yaml.constructor.ConstructorError(
                None, None, f"unhashable key {key!r}", key_node.start_mark
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_yaml_file(path: Path) -> tuple[Any, list[str]]:
    """Read ``path`` as YAML. Returns ``(data, problems)``; ``problems`` is non-empty when
    the file cannot be read or parsed (then ``data`` is ``None``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"cannot read: {type(exc).__name__}: {exc}"]
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader), []  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        return None, [f"not valid YAML: {exc}"]
