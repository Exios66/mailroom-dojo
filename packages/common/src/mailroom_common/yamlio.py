"""Strict YAML loading: the one way this workspace reads a YAML file.

``yaml.safe_load`` has three habits that corrupt configuration silently, and this loader
removes them:

* a duplicated mapping key keeps the last value without a word, so it raises here;
* YAML 1.1 reads the bare words ``yes``, ``no``, ``on``, ``off`` (and ``y``/``n``) as
  booleans, so a vocabulary containing ``no`` or ``on`` turns into ``False``/``True``;
  only ``true`` and ``false`` (any case) are booleans here;
* a mapping key that is not a string (a bare ``1`` or ``null``) is refused, because JSON
  and canonical hashing cannot represent it.

Numbers follow the YAML 1.2 core schema: ``010`` is the int 10 (not octal 8), ``0o17``
and ``0x1F`` are ints, ``1e3`` is the float 1000.0, and YAML 1.1 forms such as ``1:30``,
``1_000``, ``0b101`` and the bare ``=`` stay strings. ``!!binary`` and ``!!set`` are
refused, because JSON cannot hold bytes or sets.

Only safe constructors are used: no Python objects, no code execution.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from mailroom_common.errors import ConfigError

__all__ = ["load_yaml", "loads_yaml"]


class _StrictLoader(yaml.SafeLoader):
    pass


# Booleans, ints and floats resolve as in the YAML 1.2 core schema; YAML 1.1's extra
# forms (yes/no, 0-prefixed octal, 1_000, 1:30 sexagesimal, 0b101) and the ``=`` value
# tag stay strings.
_REPLACED_TAGS = {
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:value",
}
_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT = re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$")
_FLOAT = re.compile(
    r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
)
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in _REPLACED_TAGS]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL, list("tTfF"))
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:int", _INT, list("-+0123456789"))
# Resolvers are tried in order: an int-shaped scalar matched the int resolver first.
_StrictLoader.add_implicit_resolver("tag:yaml.org,2002:float", _FLOAT, list("-+0123456789."))


def _construct_int(loader: _StrictLoader, node: yaml.ScalarNode) -> int:
    text = loader.construct_scalar(node)
    if not isinstance(text, str) or not _INT.match(text):
        raise yaml.constructor.ConstructorError(
            None, None, f"not a YAML 1.2 integer: {text!r}", node.start_mark
        )
    if text.startswith(("0o", "0x")):
        return int(text, 0)
    return int(text, 10)


def _refuse(tag: str) -> Any:
    def construct(loader: _StrictLoader, node: yaml.Node) -> Any:
        raise yaml.constructor.ConstructorError(
            None, None, f"!!{tag} is not allowed (JSON cannot represent it)", node.start_mark
        )

    return construct


_StrictLoader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_StrictLoader.add_constructor("tag:yaml.org,2002:binary", _refuse("binary"))
_StrictLoader.add_constructor("tag:yaml.org,2002:set", _refuse("set"))


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    loader.flatten_mapping(node)
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"mapping key must be a string, got {type(key).__name__} ({key!r})",
                key_node.start_mark,
            )
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate mapping key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def loads_yaml(text: str, *, source: str = "<string>") -> Any:
    """Parse ``text``; raise :class:`ConfigError` (with ``source`` and line) if it is invalid."""
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506  (safe subclass)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"invalid YAML: {problem}", source=source, line=line) from exc


def load_yaml(path: str | Path) -> Any:
    """Read and parse a UTF-8 YAML file; a missing or unreadable file is a :class:`ConfigError`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read config file: {exc}", source=str(path)) from exc
    return loads_yaml(text, source=str(path))
