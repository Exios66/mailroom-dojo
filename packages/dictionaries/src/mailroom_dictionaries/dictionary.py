"""Controlled vocabularies: one versioned YAML file per dictionary, validated when it is loaded.

A dictionary file holds ``name`` (equal to the file stem), ``version`` (a positive int, bumped
on any content change), ``description``, ``ordered`` and a non-empty list of ``entries``, each
with ``key``, ``label``, ``description`` and optionally ``aliases``, ``deprecated``,
``replaced_by``, ``rank`` (ordered dictionaries only) and ``attributes`` (JSON values).

Values are compared after folding (the Dojo's ``label`` comparison key, made Unicode-aware):
NFKC-normalized, case-folded, then every character outside ``a-z`` and ``0-9`` dropped. So
``Time Sensitive``, ``time_sensitive``, ``TIME-SENSITIVE`` and ``Time–Sensitive`` all name the
key ``time-sensitive``, and full-width letters fold to their ASCII form. An entry is named by its
key, its label and its aliases; because of that, no two of those surfaces belonging to different
entries may fold to the same text. A key or alias that folds to nothing is refused; a label that
folds to nothing (one written only in a non-Latin script, say) is simply not a lookup surface.

Every malformed file raises :class:`~mailroom_common.errors.ConfigError` naming the file;
a value that is not in a dictionary raises :class:`VocabularyError`.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mailroom_common.errors import ConfigError
from mailroom_common.hashing import hash_obj
from mailroom_common.yamlio import load_yaml

__all__ = [
    "DATA_DIR",
    "Dictionary",
    "DictionarySet",
    "Entry",
    "VocabularyError",
    "fold",
    "list_dictionaries",
    "load_dictionary",
    "load_dictionary_dir",
    "load_dictionary_file",
    "shipped_dictionaries",
]

#: Where the shipped dictionaries live (package data).
DATA_DIR = Path(__file__).parent / "data"

#: The dictionary no override may replace.
_FIXED = "label_statuses"

#: Files in ``DATA_DIR`` that are not dictionaries.
_NOT_DICTIONARIES = frozenset({"glossary"})

KEY_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")

#: Echoed values in error messages are cut to this many characters.
_ECHO_LIMIT = 80
#: Values longer than this (after folding) get no "did you mean" search.
_SUGGEST_LIMIT = 200


class VocabularyError(ConfigError):
    """A value, dictionary or field type is not in the controlled vocabulary."""

    code = "vocabulary_error"


def fold(text: str) -> str:
    """The comparison form of ``text``: NFKC-normalized, case-folded, only ``a-z0-9`` kept."""
    return _NOT_ALNUM.sub("", unicodedata.normalize("NFKC", text).casefold())


def _clip(value: str) -> str:
    """``value`` cut to :data:`_ECHO_LIMIT` characters, so a huge input never fills a message or log."""
    if len(value) <= _ECHO_LIMIT:
        return value
    return f"{value[: _ECHO_LIMIT - 3]}... ({len(value)} characters)"


def _surfaces(key: str, label: str, aliases: Iterable[str]) -> list[tuple[str, str]]:
    """``(surface, kind)`` pairs an entry is named by: key, label and aliases."""
    return [(key, "key"), (label, "label"), *((alias, "alias") for alias in aliases)]


class _EntrySpec(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    key: str
    label: str
    description: str
    aliases: list[str] = Field(default_factory=list)
    deprecated: bool = False
    replaced_by: str | None = None
    rank: int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class _DictionarySpec(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str
    version: int
    description: str
    ordered: bool = False
    entries: list[_EntrySpec]


@dataclass(frozen=True)
class Entry:
    """One entry of a dictionary. ``attributes`` is a read-only mapping."""

    key: str
    label: str
    description: str
    aliases: tuple[str, ...]
    deprecated: bool
    replaced_by: str | None
    rank: int | None
    attributes: Mapping[str, Any]


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _check_json(value: Any, where: str, source: str) -> None:
    """Refuse attribute values that canonical JSON cannot hash (dates, NaN, non-string keys)."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"{where}: attribute value must be finite", source=source)
        return
    if isinstance(value, list):
        for item in value:
            _check_json(item, where, source)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _check_json(item, f"{where}.{key}", source)
        return
    raise ConfigError(
        f"{where}: attribute values must be JSON (string, number, bool, null, list, mapping), "
        f"got {type(value).__name__}; quote dates",
        source=source,
    )


class Dictionary:
    """A validated, read-only controlled vocabulary. Build it with the ``load_*`` functions."""

    def __init__(self, spec: _DictionarySpec, *, source: str) -> None:
        self.name = spec.name
        self.version = spec.version
        self.description = spec.description
        self.ordered = spec.ordered
        self.source = source
        # The hash covers the content with every default filled in, so the YAML key order or an
        # omitted default never changes it. The version is left out: it describes the content.
        self._hash = hash_obj(spec.model_dump(mode="python", exclude={"version"}))
        self.entries: tuple[Entry, ...] = tuple(
            Entry(
                key=e.key,
                label=e.label,
                description=e.description,
                aliases=tuple(e.aliases),
                deprecated=e.deprecated,
                replaced_by=e.replaced_by,
                rank=e.rank,
                attributes=_freeze(e.attributes),
            )
            for e in spec.entries
        )
        self._by_key = {entry.key: entry for entry in self.entries}
        self._active = tuple(entry.key for entry in self.entries if not entry.deprecated)
        lookup: dict[str, str] = {}
        for entry in self.entries:
            for surface, _kind in _surfaces(entry.key, entry.label, entry.aliases):
                folded = fold(surface)
                if folded:
                    lookup[folded] = entry.key
        self._lookup = lookup

    @property
    def origin(self) -> str:
        """``"shipped"`` when loaded from this package's data directory, otherwise ``"override"``."""
        return "shipped" if Path(self.source).resolve().parent == DATA_DIR.resolve() else "override"

    def __repr__(self) -> str:
        return f"Dictionary(name={self.name!r}, version={self.version}, entries={len(self.entries)})"

    def keys(self) -> tuple[str, ...]:
        """The canonical (not deprecated) keys, in file order."""
        return self._active

    def __iter__(self) -> Iterator[str]:
        """Iterate over the canonical keys, in file order."""
        return iter(self._active)

    def __len__(self) -> int:
        return len(self._active)

    def __contains__(self, value: object) -> bool:
        """True when ``value`` is exactly a canonical key (no folding, no aliases)."""
        return isinstance(value, str) and value in self._active

    def get(self, key: str) -> Entry | None:
        """The entry listed under exactly ``key`` (deprecated entries included), or ``None``."""
        return self._by_key.get(key)

    def _resolve(self, value: object) -> tuple[str | None, str | None]:
        """``(canonical key, None)`` or ``(None, reason it does not resolve)``."""
        if not isinstance(value, str):
            return None, f"expected a string, got {type(value).__name__}"
        listed = self._lookup.get(fold(value))
        if listed is None:
            return None, None
        entry = self._by_key[listed]
        if not entry.deprecated:
            return entry.key, None
        if entry.replaced_by is None:
            return None, f"{entry.key!r} is deprecated and has no replacement"
        return entry.replaced_by, None

    def normalize(self, value: object) -> str | None:
        """The key ``value`` names (folded; labels, aliases, deprecated keys resolve), or ``None``."""
        return self._resolve(value)[0]

    def require(self, value: object) -> str:
        """Like :meth:`normalize`, but a value that does not resolve raises :class:`VocabularyError`."""
        key, reason = self._resolve(value)
        if key is not None:
            return key
        suggestions: list[str] = []
        if reason is None:
            folded = fold(value) if isinstance(value, str) else ""
            close = (
                difflib.get_close_matches(folded, list(self._lookup), n=3, cutoff=0.6)
                if len(folded) <= _SUGGEST_LIMIT
                else []
            )
            for match in close:
                target = self.normalize(self._lookup[match])
                if target is not None and target not in suggestions:
                    suggestions.append(target)
            assert isinstance(value, str)  # only a string reaches here without a reason
            reason = f"{_clip(value)!r} is not in dictionary {self.name!r} version {self.version}"
            if suggestions:
                reason += f"; did you mean {', '.join(repr(s) for s in suggestions)}?"
        else:
            reason = f"{reason} (dictionary {self.name!r} version {self.version})"
        raise VocabularyError(
            reason,
            dictionary=self.name,
            version=self.version,
            value=_clip(value if isinstance(value, str) else repr(value)),
            suggestions=suggestions,
        )

    def rank(self, value: object) -> int:
        """The rank of the key ``value`` resolves to; only ordered dictionaries have ranks."""
        if not self.ordered:
            raise VocabularyError(
                f"dictionary {self.name!r} is not ordered, so its keys have no rank",
                dictionary=self.name,
                version=self.version,
            )
        rank = self._by_key[self.require(value)].rank
        assert rank is not None  # validation gives every active entry of an ordered dictionary a rank
        return rank

    def content_hash(self) -> str:
        """SHA-256 of the canonical content (version excluded); stable across YAML key order."""
        return self._hash


def _fail(message: str, source: str, **context: Any) -> ConfigError:
    return ConfigError(message, source=source, **context)


def _validate_spec(spec: _DictionarySpec, source: str) -> None:
    name = spec.name
    if not KEY_PATTERN.fullmatch(name):
        raise _fail(f"dictionary name {name!r} must match {KEY_PATTERN.pattern}", source)
    if spec.version < 1:
        raise _fail(f"version must be a positive integer, got {spec.version}", source, dictionary=name)
    if not spec.description.strip():
        raise _fail("dictionary description is empty", source, dictionary=name)
    if not spec.entries:
        raise _fail("dictionary has no entries", source, dictionary=name)

    seen: dict[str, tuple[str, str]] = {}  # folded surface -> (entry key, what claimed it)
    by_key: dict[str, _EntrySpec] = {}
    for entry in spec.entries:
        where = f"entry {entry.key!r}"
        if not KEY_PATTERN.fullmatch(entry.key):
            raise _fail(f"{where}: key must match {KEY_PATTERN.pattern}", source, dictionary=name)
        if not entry.label.strip():
            raise _fail(f"{where}: label is empty", source, dictionary=name)
        if not entry.description.strip():
            raise _fail(f"{where}: description is empty", source, dictionary=name)
        for surface, kind in _surfaces(entry.key, entry.label, entry.aliases):
            folded = fold(surface)
            if not folded:
                if kind == "label":
                    continue  # a label in a script that folds to nothing is not a lookup surface
                raise _fail(f"{where}: {kind} {surface!r} is empty after folding", source, dictionary=name)
            owner = seen.get(folded)
            if owner is not None:
                if kind == "label" and owner[0] == entry.key:
                    continue  # a label may restate its own key ("Other" for `other`)
                raise _fail(
                    f"{where}: {kind} {surface!r} collides with {owner[1]} after folding to {folded!r}",
                    source,
                    dictionary=name,
                )
            seen[folded] = (entry.key, f"{kind} {surface!r} of entry {entry.key!r}")
        _check_json(entry.attributes, f"{where} attributes", source)
        by_key[entry.key] = entry

    for entry in spec.entries:
        where = f"entry {entry.key!r}"
        if entry.replaced_by is not None:
            if not entry.deprecated:
                raise _fail(
                    f"{where}: replaced_by is set but the entry is not deprecated", source, dictionary=name
                )
            target = by_key.get(entry.replaced_by)
            if target is None:
                raise _fail(
                    f"{where}: replaced_by {entry.replaced_by!r} is not a key", source, dictionary=name
                )
            if target.deprecated:
                raise _fail(
                    f"{where}: replaced_by {entry.replaced_by!r} is itself deprecated (no chains)",
                    source,
                    dictionary=name,
                )

    ranks: dict[int, str] = {}
    previous: int | None = None
    for entry in spec.entries:
        where = f"entry {entry.key!r}"
        if not spec.ordered or entry.deprecated:
            if entry.rank is not None:
                why = (
                    "deprecated entries take their replacement's rank"
                    if spec.ordered
                    else "dictionary is not ordered"
                )
                raise _fail(f"{where}: rank is set but the {why}", source, dictionary=name)
            continue
        if entry.rank is None:
            raise _fail(
                f"{where}: an ordered dictionary needs a rank on every entry", source, dictionary=name
            )
        if entry.rank in ranks:
            raise _fail(
                f"{where}: duplicate rank {entry.rank} (also on {ranks[entry.rank]!r})",
                source,
                dictionary=name,
            )
        if previous is not None and entry.rank < previous:
            raise _fail(f"{where}: entries must be listed in rank order", source, dictionary=name)
        ranks[entry.rank] = entry.key
        previous = entry.rank


def load_dictionary_file(path: str | Path) -> Dictionary:
    """Load and validate one dictionary file (a client profile's own vocabulary, for example).

    Checks everything the file can show alone, including the per-dictionary rules for
    ``doc_classes`` and ``field_types``. References to other dictionaries are checked when the
    dictionary joins a :class:`DictionarySet`.
    """
    path = Path(path)
    source = str(path)
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise _fail(f"a dictionary file must be a mapping, got {type(raw).__name__}", source)
    try:
        spec = _DictionarySpec.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        raise _fail(f"invalid dictionary file: {problems}", source) from exc
    _validate_spec(spec, source)
    if spec.name in _NOT_DICTIONARIES:
        raise _fail(f"dictionary name {spec.name!r} is reserved", source)
    if spec.name != path.stem:
        raise _fail(f"dictionary name {spec.name!r} must equal the file name {path.stem!r}", source)
    dictionary = Dictionary(spec, source=source)
    check = _FILE_CHECKS.get(dictionary.name)
    if check is not None:
        check(dictionary)
    return dictionary


class DictionarySet(Mapping[str, Dictionary]):
    """Dictionaries that reference each other, checked together; ``manifest()`` pins them for a run."""

    def __init__(self, dictionaries: Iterable[Dictionary]) -> None:
        self._members = dict(sorted(DictionarySet._unique(dictionaries).items()))
        for dictionary in self._members.values():
            check = _SET_CHECKS.get(dictionary.name)
            if check is not None:
                check(dictionary, self._members)

    def __getitem__(self, name: str) -> Dictionary:
        return self._members[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def with_overrides(self, dictionaries: Iterable[Dictionary]) -> DictionarySet:
        """A new set where ``dictionaries`` are added, replacing members of the same name.

        Refused with :class:`ConfigError`: overriding ``label_statuses`` (label precedence does
        not change), and a ``doc_classes`` override that drops a class of this set.
        """
        overrides = DictionarySet._unique(dictionaries)
        if _FIXED in overrides:
            raise ConfigError(
                f"dictionary {_FIXED!r} cannot be overridden: label precedence does not change",
                source=overrides[_FIXED].source,
            )
        classes = overrides.get("doc_classes")
        if classes is not None and "doc_classes" in self._members:
            missing = [key for key in self._members["doc_classes"] if key not in classes]
            if missing:
                raise ConfigError(
                    f"a doc_classes override must keep every shipped class; missing {missing}",
                    source=classes.source,
                    missing=missing,
                )
        merged = dict(self._members)
        merged.update(overrides)
        return DictionarySet(merged.values())

    @staticmethod
    def _unique(dictionaries: Iterable[Dictionary]) -> dict[str, Dictionary]:
        out: dict[str, Dictionary] = {}
        for dictionary in dictionaries:
            if dictionary.name in out:
                raise ConfigError(
                    f"dictionary {dictionary.name!r} is given twice",
                    sources=[out[dictionary.name].source, dictionary.source],
                )
            out[dictionary.name] = dictionary
        return out

    def manifest(self) -> dict[str, dict[str, Any]]:
        """``{name: {"version": int, "hash": str, "origin": "shipped" | "override"}}``, sorted by name."""
        return {
            name: {
                "version": dictionary.version,
                "hash": dictionary.content_hash(),
                "origin": dictionary.origin,
            }
            for name, dictionary in self._members.items()
        }


def load_dictionary_dir(directory: str | Path) -> DictionarySet:
    """Load every ``*.yaml`` dictionary in ``directory`` (``glossary.yaml`` excepted) as one set."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ConfigError("dictionary directory does not exist", source=str(directory))
    paths = sorted(p for p in directory.glob("*.yaml") if p.stem not in _NOT_DICTIONARIES)
    return DictionarySet(load_dictionary_file(path) for path in paths)


@cache
def shipped_dictionaries() -> DictionarySet:
    """The dictionaries shipped with this package, loaded and cross-checked once per process."""
    return load_dictionary_dir(DATA_DIR)


def list_dictionaries() -> list[str]:
    """Names of the shipped dictionaries, sorted."""
    return list(shipped_dictionaries())


def load_dictionary(name: str) -> Dictionary:
    """One shipped dictionary by name; an unknown name raises :class:`VocabularyError`."""
    shipped = shipped_dictionaries()
    if name not in shipped:
        close = difflib.get_close_matches(name, list(shipped), n=3)
        hint = f"; did you mean {', '.join(repr(c) for c in close)}?" if close else ""
        raise VocabularyError(f"no shipped dictionary named {name!r}{hint}", known=list(shipped))
    return shipped[name]


# --- per-dictionary rules -------------------------------------------------------------------

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*")


def _check_attribute_keys(
    dictionary: Dictionary, entry: Entry, allowed: set[str], required: set[str]
) -> None:
    keys = set(entry.attributes)
    unknown = keys - allowed
    missing = required - keys
    if unknown or missing:
        raise ConfigError(
            f"entry {entry.key!r}: attributes must have {sorted(required)} and may have {sorted(allowed)}; "
            f"unknown {sorted(unknown)}, missing {sorted(missing)}",
            source=dictionary.source,
            dictionary=dictionary.name,
        )


def _check_doc_classes(dictionary: Dictionary) -> None:
    """Each class lists unique, well-formed subclasses and maps closed label fields to dictionary names."""
    required = {"subclasses", "closed_label_fields"}
    for entry in dictionary.entries:
        _check_attribute_keys(dictionary, entry, required, required)
        where = f"entry {entry.key!r}"
        subclasses = entry.attributes["subclasses"]
        if not isinstance(subclasses, tuple) or not all(isinstance(s, str) for s in subclasses):
            raise ConfigError(f"{where}: subclasses must be a list of strings", source=dictionary.source)
        folded = [fold(s) for s in subclasses]
        duplicates = sorted({s for s, f in zip(subclasses, folded, strict=True) if folded.count(f) > 1})
        if duplicates:
            raise ConfigError(f"{where}: duplicate subclasses {duplicates}", source=dictionary.source)
        bad = [s for s in subclasses if not KEY_PATTERN.fullmatch(s)]
        if bad:
            raise ConfigError(
                f"{where}: subclasses {bad} must match {KEY_PATTERN.pattern}", source=dictionary.source
            )
        fields = entry.attributes["closed_label_fields"]
        if not isinstance(fields, Mapping):
            raise ConfigError(f"{where}: closed_label_fields must be a mapping", source=dictionary.source)
        for field, target in fields.items():
            if (
                not _FIELD_NAME.fullmatch(field)
                or not isinstance(target, str)
                or not KEY_PATTERN.fullmatch(target)
            ):
                raise ConfigError(
                    f"{where}: closed_label_fields entry {field!r}: {target!r} must map a field name "
                    "to a dictionary name",
                    source=dictionary.source,
                )


def _check_doc_classes_refs(dictionary: Dictionary, members: Mapping[str, Dictionary]) -> None:
    for entry in dictionary.entries:
        for field, target in entry.attributes["closed_label_fields"].items():
            if target not in members:
                raise ConfigError(
                    f"entry {entry.key!r}: closed label field {field!r} names dictionary {target!r}, "
                    "which is not loaded",
                    source=dictionary.source,
                    known=sorted(members),
                )


def _check_field_types(dictionary: Dictionary) -> None:
    """``takes_element`` is a bool; only a type that takes an element may name a ``default_element``."""
    for entry in dictionary.entries:
        _check_attribute_keys(dictionary, entry, {"takes_element", "default_element"}, {"takes_element"})
        takes = entry.attributes["takes_element"]
        if not isinstance(takes, bool):
            raise ConfigError(
                f"entry {entry.key!r}: takes_element must be true or false", source=dictionary.source
            )
        default = entry.attributes.get("default_element")
        if default is not None and (not takes or not isinstance(default, str)):
            raise ConfigError(
                f"entry {entry.key!r}: default_element is only allowed (as a string) "
                "when takes_element is true",
                source=dictionary.source,
            )


def _check_field_types_refs(dictionary: Dictionary, members: Mapping[str, Dictionary]) -> None:
    elements = members.get("entity_element_types")
    if elements is None:
        raise ConfigError("field_types needs the entity_element_types dictionary", source=dictionary.source)
    for entry in dictionary.entries:
        default = entry.attributes.get("default_element")
        if default is not None and default not in elements:
            raise ConfigError(
                f"entry {entry.key!r}: default_element {default!r} is not an entity element type",
                source=dictionary.source,
            )
    for key in elements:
        base = dictionary.get(key)
        if base is None or base.deprecated or base.attributes["takes_element"]:
            raise ConfigError(
                f"entity element type {key!r} must be a field type that takes no element",
                source=elements.source,
            )


_FILE_CHECKS: dict[str, Callable[[Dictionary], None]] = {
    "doc_classes": _check_doc_classes,
    "field_types": _check_field_types,
}
_SET_CHECKS: dict[str, Callable[[Dictionary, Mapping[str, Dictionary]], None]] = {
    "doc_classes": _check_doc_classes_refs,
    "field_types": _check_field_types_refs,
}
