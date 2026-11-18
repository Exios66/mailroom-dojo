"""Safe YAML loading that refuses duplicate keys (shared by the policy and registry loaders)."""

from __future__ import annotations

import reprlib
from pathlib import Path
from typing import Any

import yaml

__all__ = ["load_yaml_file", "show"]

_SHOW = reprlib.Repr()
_SHOW.maxlevel, _SHOW.maxstring, _SHOW.maxother, _SHOW.maxlong = 3, 80, 80, 40
_SHOW.maxlist = _SHOW.maxtuple = _SHOW.maxdict = _SHOW.maxset = 6


def show(value: Any) -> str:
    """A bounded ``repr`` for problem messages: a hostile value (a long string, a huge
    integer, a list nested thousands deep) is shortened, never echoed whole or crashing."""
    try:
        return _SHOW.repr(value)
    except (RecursionError, ValueError):
        return f"<{type(value).__name__}>"


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
    the file cannot be read or parsed (then ``data`` is ``None``), including a document
    nested too deeply for the parser or holding an integer too long to convert."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"cannot read: {type(exc).__name__}: {exc}"]
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader), []  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        return None, [f"not valid YAML: {exc}"]
    except RecursionError:
        return None, ["not readable YAML: nested too deeply"]
    except (ValueError, OverflowError) as exc:  # e.g. an integer literal too long to convert
        return None, [f"not readable YAML: {type(exc).__name__}: {exc}"]
