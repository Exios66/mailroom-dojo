"""Client profiles: the classes, fields, field types, vocabularies and label precedence.

A profile lives at ``config/clients/<client>/profile.yaml``. Its keys (plan decision 7)::

    client: <name>
    dataset: {repository: ..., revision: ...}          # free mapping, recorded as given
    precedence: [review:gold, interim:correction, hub, ...]   # ranks, highest first
    scorable_statuses: [hub, interim, near_gold, gold]
    vocabularies: {<vocabulary>: [<value>, ...]}
    classes:
      <class>:
        specialist: <agent name>
        subclasses: [<subclass>, ...]
        fields:
          <field>: {type: <field type>, vocabulary: <vocabulary>?, tier: <tier>?}

Classes, subclasses and fields are open strings: a new document type is a profile entry,
never a code change (R4). Field types use the Dojo's vocabulary (``FIELD_TYPES``). A rank
left out of ``precedence`` is never truth; ``interim:disputed`` may never be listed.
``load_profile`` collects every problem before raising, so one run of the loader shows
the whole list.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, JsonValue

from mailroom_labels.errors import ProfileError
from mailroom_labels.models import RANKS, STATUS_WORDS, UNTRUSTED_RANKS

__all__ = [
    "FIELD_TYPES",
    "ClientProfile",
    "ClassSpec",
    "FieldSpec",
    "is_field_type",
    "load_profile",
    "parse_profile",
]

#: Scalar field types of the Dojo's field scorer.
SCALAR_TYPES: tuple[str, ...] = (
    "name",
    "label",
    "date",
    "money",
    "id",
    "free_text",
    "containment",
    "cms_doc_ref",
)
#: Item types an ``entity_list:<item>`` may name.
ENTITY_ITEM_TYPES: tuple[str, ...] = ("name", "free_text", "id", "cms_doc_ref")
FIELD_TYPES: tuple[str, ...] = (
    *SCALAR_TYPES,
    "entity_list",
    *(f"entity_list:{item}" for item in ENTITY_ITEM_TYPES),
)

_TOP_KEYS = {"client", "dataset", "precedence", "scorable_statuses", "vocabularies", "classes"}
_CLASS_KEYS = {"specialist", "subclasses", "fields"}
_FIELD_KEYS = {"type", "vocabulary", "tier"}

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def is_field_type(text: object) -> bool:
    """True when ``text`` is a field type the Dojo scores."""
    return isinstance(text, str) and text in FIELD_TYPES


class FieldSpec(BaseModel):
    """One extraction field of a class."""

    model_config = _FROZEN

    type: str
    vocabulary: str | None = None
    tier: str | None = None


class ClassSpec(BaseModel):
    """One document class: its specialist, subclasses and fields."""

    model_config = _FROZEN

    specialist: str | None = None
    subclasses: tuple[str, ...] = ()
    fields: Mapping[str, FieldSpec]


class ClientProfile(BaseModel):
    """A validated client profile. Build it with ``load_profile`` or ``parse_profile``."""

    model_config = _FROZEN

    client: str
    dataset: Mapping[str, JsonValue]
    precedence: tuple[str, ...]
    scorable_statuses: frozenset[str]
    vocabularies: Mapping[str, tuple[str, ...]]
    classes: Mapping[str, ClassSpec]

    def rank_index(self, rank: str) -> int | None:
        """Position of ``rank`` in the precedence (0 is highest); None if never truth."""
        try:
            return self.precedence.index(rank)
        except ValueError:
            return None

    def field_spec(self, doc_class: str | None, field: str) -> FieldSpec | None:
        """The spec of ``field`` in ``doc_class``, or None when either is unknown."""
        spec = self.classes.get(doc_class) if doc_class is not None else None
        return spec.fields.get(field) if spec is not None else None


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping holding the same key twice (YAML keeps the last
    one silently, which would hide a copy-paste error in a profile)."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None, None, f"unhashable mapping key {key!r}", key_node.start_mark
            ) from None
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _is_name(value: object) -> bool:
    return isinstance(value, str) and value.strip() == value and value != ""


def _str_list(raw: object, where: str, problems: list[str]) -> list[str]:
    if not isinstance(raw, list):
        problems.append(f"{where} must be a list")
        return []
    out: list[str] = []
    for item in raw:
        if not _is_name(item):
            problems.append(f"{where} holds a non-name entry {item!r}")
        elif item in out:
            problems.append(f"{where} lists {item!r} twice")
        else:
            out.append(item)
    return out


def _check_top(raw: Mapping[str, Any], problems: list[str]) -> None:
    for key in sorted(set(raw) - _TOP_KEYS):
        problems.append(f"unknown key {key!r}")
    for key in sorted(_TOP_KEYS - set(raw)):
        problems.append(f"missing key {key!r}")
    if "client" in raw and not _is_name(raw["client"]):
        problems.append("client must be a non-empty name")
    if "dataset" in raw and not isinstance(raw["dataset"], dict):
        problems.append("dataset must be a mapping")


def _check_precedence(raw: object, problems: list[str]) -> list[str]:
    ranks = _str_list(raw, "precedence", problems)
    for rank in ranks:
        if rank in UNTRUSTED_RANKS:
            problems.append(f"precedence names {rank!r}, which is never truth")
        elif rank not in RANKS:
            problems.append(f"precedence names unknown rank {rank!r} (known: {', '.join(RANKS)})")
    if isinstance(raw, list) and not ranks:
        problems.append("precedence is empty")
    return ranks


def _check_vocabularies(raw: object, problems: list[str]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        problems.append("vocabularies must be a mapping")
        return {}
    out: dict[str, list[str]] = {}
    for name, values in raw.items():
        if not _is_name(name):
            problems.append(f"vocabulary name {name!r} is not a name")
            continue
        out[name] = _str_list(values, f"vocabularies.{name}", problems)
        if isinstance(values, list) and not values:
            problems.append(f"vocabularies.{name} is empty")
    return out


def _check_field(where: str, raw: object, vocabularies: Mapping[str, Any], problems: list[str]) -> None:
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a mapping")
        return
    for key in sorted(set(raw) - _FIELD_KEYS):
        problems.append(f"{where} has unknown key {key!r}")
    ftype = raw.get("type")
    if not is_field_type(ftype):
        problems.append(f"{where}.type {ftype!r} is not a Dojo field type")
    vocab = raw.get("vocabulary")
    if vocab is not None and not _is_name(vocab):
        problems.append(f"{where}.vocabulary must be a name, not {type(vocab).__name__}")
    elif vocab is not None:
        if vocab not in vocabularies:
            problems.append(f"{where}.vocabulary {vocab!r} is not defined under vocabularies")
        if ftype != "label":
            problems.append(f"{where} has a vocabulary but type {ftype!r}; only a label is closed")
    tier = raw.get("tier")
    if tier is not None and not _is_name(tier):
        problems.append(f"{where}.tier must be a name")


def _check_classes(raw: object, vocabularies: Mapping[str, Any], problems: list[str]) -> None:
    if not isinstance(raw, dict) or not raw:
        problems.append("classes must be a non-empty mapping")
        return
    for name, spec in raw.items():
        where = f"classes.{name}"
        if not _is_name(name):
            problems.append(f"class name {name!r} is not a name")
            continue
        if not isinstance(spec, dict):
            problems.append(f"{where} must be a mapping")
            continue
        for key in sorted(set(spec) - _CLASS_KEYS):
            problems.append(f"{where} has unknown key {key!r}")
        if spec.get("specialist") is not None and not _is_name(spec["specialist"]):
            problems.append(f"{where}.specialist must be a name")
        _str_list(spec.get("subclasses", []), f"{where}.subclasses", problems)
        fields = spec.get("fields")
        if not isinstance(fields, dict) or not fields:
            problems.append(f"{where}.fields must be a non-empty mapping")
            continue
        for fname, fspec in fields.items():
            if not _is_name(fname):
                problems.append(f"{where} has field name {fname!r}, not a name")
                continue
            _check_field(f"{where}.fields.{fname}", fspec, vocabularies, problems)


def parse_profile(raw: object, path: Path | None = None) -> ClientProfile:
    """Validate a profile already parsed from YAML; raise ``ProfileError`` listing every problem."""
    if not isinstance(raw, dict):
        raise ProfileError(path, ["a profile must be a mapping"])
    problems: list[str] = []
    _check_top(raw, problems)
    _check_precedence(raw.get("precedence", []), problems)
    statuses = _str_list(raw.get("scorable_statuses", []), "scorable_statuses", problems)
    for status in statuses:
        if status not in STATUS_WORDS:
            problems.append(f"scorable_statuses names unknown status {status!r}")
    vocabularies = _check_vocabularies(raw.get("vocabularies", {}), problems)
    _check_classes(raw.get("classes"), vocabularies, problems)
    if problems:
        raise ProfileError(path, problems)
    return ClientProfile(
        client=raw["client"],
        dataset=raw["dataset"],
        precedence=tuple(raw["precedence"]),
        scorable_statuses=frozenset(raw["scorable_statuses"]),
        vocabularies={k: tuple(v) for k, v in raw["vocabularies"].items()},
        classes={
            name: ClassSpec(
                specialist=spec.get("specialist"),
                subclasses=tuple(spec.get("subclasses", [])),
                fields={f: FieldSpec(**fs) for f, fs in spec["fields"].items()},
            )
            for name, spec in raw["classes"].items()
        },
    )


def load_profile(path: Path | str) -> ClientProfile:
    """Read and validate ``profile.yaml``; raise ``ProfileError`` on any problem."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(path, [f"cannot read: {exc}"]) from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ProfileError(path, [f"not valid YAML: {exc}"]) from exc
    return parse_profile(raw, path)
