"""Loading and validating a scoring profile.

:func:`load_scoring_profile` reads ``scoring.yaml`` (or an already parsed mapping),
checks every key and value, and returns an immutable :class:`ScoringProfile`. Any
problem raises :class:`ProfileError` naming the YAML path, for example
``classes[6].fields.claim_type.type: unregistered field type 'lable'``. Defaults
equal the scoring engine's defaults; a profile only states what it changes.

The content hash is the SHA-256 of the profile's canonical JSON form (sorted keys,
no whitespace). Mapping key order never changes it; list order does, because list
order is meaningful (classes, catalog entries, alias tables that break ties by
order).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from ..normalization.text import compact_label
from .field_types import BUILTIN_SCALAR_TYPES, ENTITY_LIST_PREFIX, element_type, is_entity_list
from .frozen import FrozenMapping
from .label_folding import FOLD_METHODS, CatalogEntry, LabelVocabulary
from .models import (
    AuditSettings,
    ClassSpec,
    EmbeddingSettings,
    FieldNameDefaults,
    FieldSpec,
    ListMatchingSettings,
    PredictedValueTypeOverride,
    PresenceSettings,
    ProfileError,
    RowVariant,
    ScoringProfile,
    Thresholds,
    TraceCaptureOptions,
    TypeBand,
    VariantGuard,
    VariantMarker,
)

__all__ = ["PROFILE_FORMAT", "load_scoring_profile", "profile_canonical_form"]

#: The ``profile_format`` this loader reads.
PROFILE_FORMAT = 1

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_UNMAPPED_FIELD_POLICIES = ("heuristic", "skip", "error")
_MISSING_CONFIDENCE_POLICIES = ("absent", "assume_1", "assume_0")


# ---------------------------------------------------------------------------
# YAML reading
# ---------------------------------------------------------------------------


class _StrictSafeLoader(yaml.SafeLoader):
    """A safe YAML loader that refuses a mapping with the same key twice."""


def _construct_unique_mapping(loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    """Build a mapping, raising on a duplicate key instead of keeping the last one."""
    loader.flatten_mapping(node)
    seen: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            mark = key_node.start_mark
            raise ProfileError("", f"duplicate key {key!r} at line {mark.line + 1}, column {mark.column + 1}")
        seen[key] = loader.construct_object(value_node, deep=deep)
    return seen


_StrictSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _read_yaml(path: Path) -> Any:
    """Parse a YAML file with :class:`_StrictSafeLoader`; a syntax error is a :class:`ProfileError`."""
    text = path.read_text(encoding="utf-8")
    try:
        return yaml.load(text, Loader=_StrictSafeLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as error:
        raise ProfileError("", f"{path.name} is not valid YAML: {error}") from None


# ---------------------------------------------------------------------------
# Value readers (each names the YAML path in its error)
# ---------------------------------------------------------------------------


def _join(path: str, key: str) -> str:
    """Return the YAML path of ``key`` inside ``path``."""
    return f"{path}.{key}" if path else key


class _Section:
    """A mapping being read: each key may be read once, and unread keys are errors at :meth:`finish`."""

    def __init__(self, data: Any, path: str) -> None:
        """Wrap ``data``, which must be a mapping with string keys."""
        if not isinstance(data, Mapping):
            raise ProfileError(path, f"must be a mapping, got {type(data).__name__}")
        for key in data:
            if not isinstance(key, str):
                raise ProfileError(path, f"keys must be strings, got {key!r}")
        self.data = data
        self.path = path
        self._read: set[str] = set()

    def has(self, key: str) -> bool:
        """True when ``key`` is present."""
        return key in self.data

    def take(
        self, key: str, reader: Callable[[Any, str], Any], default: Any = None, *, required: bool = False
    ) -> Any:
        """Read ``key`` with ``reader``, or return ``default`` when it is absent (an error when
        ``required``)."""
        self._read.add(key)
        path = _join(self.path, key)
        if key not in self.data:
            if required:
                raise ProfileError(path, "is required")
            return default
        return reader(self.data[key], path)

    def finish(self, allowed: tuple[str, ...]) -> None:
        """Raise for the first key that is not in ``allowed``."""
        for key in self.data:
            if key not in allowed:
                raise ProfileError(_join(self.path, key), f"unknown key; allowed keys: {', '.join(allowed)}")


def _text(value: Any, path: str) -> str:
    """A non-empty string with no surrounding whitespace."""
    if not isinstance(value, str):
        raise ProfileError(path, f"must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ProfileError(path, "must not be blank")
    if value != value.strip():
        raise ProfileError(path, f"must not have surrounding whitespace: {value!r}")
    return value


def _identifier(value: Any, path: str) -> str:
    """A lowercase snake_case name (``^[a-z][a-z0-9_]*$``)."""
    text = _text(value, path)
    if not _IDENTIFIER.match(text):
        raise ProfileError(path, f"must be a lowercase snake_case name, got {text!r}")
    return text


def _lowercase_token(value: Any, path: str) -> str:
    """A non-empty string that is already lowercase (compared case-blind elsewhere)."""
    text = _text(value, path)
    if text != text.lower():
        raise ProfileError(path, f"must be lowercase, got {text!r}")
    return text


def _boolean(value: Any, path: str) -> bool:
    """A YAML boolean."""
    if not isinstance(value, bool):
        raise ProfileError(path, f"must be true or false, got {value!r}")
    return value


def _number(value: Any, path: str, *, low: float = 0.0, high: float = 1.0) -> float:
    """A finite number in ``[low, high]``, returned as a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(path, f"must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ProfileError(path, f"must be finite, got {value!r}")
    if not low <= number <= high:
        raise ProfileError(path, f"must be between {low} and {high}, got {value!r}")
    return number


def _unit_interval(value: Any, path: str) -> float:
    """A number in ``[0, 1]``."""
    return _number(value, path)


def _band(value: Any, path: str) -> tuple[float, float]:
    """A ``[low, high]`` pair of numbers in ``[0, 1]`` with ``low <= high``."""
    if not isinstance(value, list) or len(value) != 2:
        raise ProfileError(path, f"must be a list of two numbers [low, high], got {value!r}")
    low = _number(value[0], f"{path}[0]")
    high = _number(value[1], f"{path}[1]")
    if low > high:
        raise ProfileError(path, f"low {low} is above high {high}")
    return low, high


def _choice(choices: tuple[str, ...]) -> Callable[[Any, str], str]:
    """A reader for one of ``choices``."""

    def read(value: Any, path: str) -> str:
        """Return ``value`` when it is one of the choices."""
        if value not in choices:
            raise ProfileError(path, f"must be one of {', '.join(choices)}, got {value!r}")
        return value

    return read


def _list_of(
    item_reader: Callable[[Any, str], Any], *, unique: bool = True, non_empty: bool = False
) -> Callable[[Any, str], tuple]:
    """A reader for a list whose items each pass ``item_reader``."""

    def read(value: Any, path: str) -> tuple:
        """Return the items as a tuple."""
        if not isinstance(value, list):
            raise ProfileError(path, f"must be a list, got {type(value).__name__}")
        if non_empty and not value:
            raise ProfileError(path, "must not be empty")
        items = tuple(item_reader(item, f"{path}[{index}]") for index, item in enumerate(value))
        if unique:
            seen: set = set()
            for index, item in enumerate(items):
                if item in seen:
                    raise ProfileError(f"{path}[{index}]", f"duplicate entry {item!r}")
                seen.add(item)
        return items

    return read


def _string_map(
    key_reader: Callable[[Any, str], str], value_reader: Callable[[Any, str], Any]
) -> Callable[[Any, str], dict]:
    """A reader for a mapping whose keys and values pass the given readers (order kept)."""

    def read(value: Any, path: str) -> dict:
        """Return the mapping as a dict in YAML order."""
        section = _Section(value, path)
        return {
            key_reader(key, _join(path, key)): value_reader(item, _join(path, key))
            for key, item in section.data.items()
        }

    return read


# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------


def _field_type_reader(scalar_types: tuple[str, ...]) -> Callable[[Any, str], str]:
    """A reader for a field type registered among ``scalar_types`` (or an entity list of one)."""

    def read(value: Any, path: str) -> str:
        """Return the field type when it is registered."""
        text = _text(value, path)
        if text in scalar_types:
            return text
        if is_entity_list(text):
            element = element_type(text)
            if element in scalar_types:
                return text
            raise ProfileError(
                path,
                f"unregistered element type {element!r} in {text!r}; registered: {', '.join(scalar_types)}",
            )
        raise ProfileError(
            path,
            f"unregistered field type {text!r}; registered: {', '.join(scalar_types)}, "
            f"{ENTITY_LIST_PREFIX} or {ENTITY_LIST_PREFIX}:<type>",
        )

    return read


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


def _alias_key(value: Any, path: str) -> str:
    """An alias written in lowercase with at least one letter or digit."""
    text = _lowercase_token(value, path)
    if not compact_label(text):
        raise ProfileError(path, f"alias {text!r} has no letters or digits")
    return text


def _vocabulary(value: Any, path: str, unknown_label: str) -> LabelVocabulary:
    """Read and validate one label vocabulary (alias targets of inventory folds are checked later)."""
    section = _Section(value, path)
    tokens = section.take("tokens", _list_of(_text, non_empty=True), required=True)
    fold_method = section.take("fold", _choice(FOLD_METHODS), "exact")
    aliases = section.take("aliases", _string_map(_alias_key, _text), {})
    prefix_aliases = section.take("prefix_aliases", _string_map(_alias_key, _text), {})
    catalog_rows = section.take("catalog", _list_of(_catalog_entry, unique=False), ())
    section.finish(("tokens", "fold", "aliases", "prefix_aliases", "catalog"))

    compact_seen: dict[str, str] = {}
    for index, token in enumerate(tokens):
        key = compact_label(token)
        if not key:
            raise ProfileError(f"{path}.tokens[{index}]", f"token {token!r} has no letters or digits")
        if key in compact_seen:
            raise ProfileError(
                f"{path}.tokens[{index}]",
                f"token {token!r} folds to the same compact form as {compact_seen[key]!r}",
            )
        compact_seen[key] = token

    if aliases and fold_method not in ("aliases", "inventory", "catalog"):
        raise ProfileError(
            _join(path, "aliases"), f"aliases need fold aliases, inventory or catalog, not {fold_method}"
        )
    if prefix_aliases and fold_method != "inventory":
        raise ProfileError(
            _join(path, "prefix_aliases"), f"prefix aliases need fold inventory, not {fold_method}"
        )
    if catalog_rows and fold_method != "catalog":
        raise ProfileError(_join(path, "catalog"), f"a catalog needs fold catalog, not {fold_method}")
    if fold_method == "catalog" and not catalog_rows:
        raise ProfileError(_join(path, "catalog"), "fold catalog needs a non-empty catalog")

    alias_targets: dict[str, str] = {}
    for alias, target in aliases.items():
        key = compact_label(alias)
        if key in alias_targets and alias_targets[key] != target:
            raise ProfileError(
                _join(_join(path, "aliases"), alias),
                "folds to the same compact form as another alias with target "
                f"{alias_targets[key]!r}, not {target!r}",
            )
        alias_targets[key] = target
        if fold_method != "inventory" and target not in tokens:
            raise ProfileError(_join(_join(path, "aliases"), alias), f"target {target!r} is not a token")
    for prefix, target in prefix_aliases.items():
        if target not in tokens:
            raise ProfileError(
                _join(_join(path, "prefix_aliases"), prefix), f"target {target!r} is not a token"
            )
    catalog_keys: set[str] = set()
    for index, entry in enumerate(catalog_rows):
        entry_path = f"{path}.catalog[{index}]"
        if entry.key not in tokens:
            raise ProfileError(_join(entry_path, "key"), f"{entry.key!r} is not a token")
        if entry.key in catalog_keys:
            raise ProfileError(_join(entry_path, "key"), f"duplicate catalog key {entry.key!r}")
        catalog_keys.add(entry.key)
    return LabelVocabulary(
        tokens=tokens,
        fold_method=fold_method,
        aliases=FrozenMapping(aliases),
        prefix_aliases=FrozenMapping(prefix_aliases),
        catalog=tuple(catalog_rows),
        unknown_token=unknown_label,
    )


def _catalog_entry(value: Any, path: str) -> CatalogEntry:
    """One ``{key, label}`` catalog row; the label needs a letter or digit."""
    section = _Section(value, path)
    key = section.take("key", _text, required=True)
    label = section.take("label", _text, required=True)
    section.finish(("key", "label"))
    if not compact_label(label):
        raise ProfileError(_join(path, "label"), f"label {label!r} has no letters or digits")
    return CatalogEntry(key=key, label=label)


# ---------------------------------------------------------------------------
# Fields, variants, classes
# ---------------------------------------------------------------------------


class _ProfileContext:
    """What every class reader needs: registered types, name defaults and the unknown label."""

    def __init__(
        self, scalar_types: tuple[str, ...], name_defaults: FieldNameDefaults, unknown_label: str
    ) -> None:
        """Hold the shared reading context."""
        self.scalar_types = scalar_types
        self.field_type = _field_type_reader(scalar_types)
        self.name_defaults = name_defaults
        self.unknown_label = unknown_label


def _field(name: str, value: Any, path: str, context: _ProfileContext) -> FieldSpec:
    """Read one field: a type string, or a mapping with ``type`` and optional scoring rules."""
    if isinstance(value, str):
        value = {"type": value}
    section = _Section(value, path)
    field_type = section.take("type", context.field_type, required=True)
    default_compare = "containment" if name in context.name_defaults.containment else "default"
    default_coverage = "recall" if name in context.name_defaults.recall_coverage else "f1"
    compare = section.take("compare", _choice(("default", "containment")), default_compare)
    coverage = section.take("coverage", _choice(("f1", "recall")), default_coverage)
    vocabulary = section.take(
        "vocabulary", lambda raw, raw_path: _vocabulary(raw, raw_path, context.unknown_label)
    )
    override = section.take(
        "predicted_value_type_override", lambda raw, raw_path: _type_override(raw, raw_path, context)
    )
    subclasses = section.take("subclasses", _list_of(_text), ())
    section.finish(
        ("type", "compare", "coverage", "vocabulary", "predicted_value_type_override", "subclasses")
    )
    if field_type == "label" and vocabulary is None:
        raise ProfileError(path, "a label field needs a vocabulary")
    if vocabulary is not None and field_type != "label":
        raise ProfileError(
            _join(path, "vocabulary"), f"only label fields fold labels; this field is {field_type!r}"
        )
    if coverage == "recall" and not is_entity_list(field_type) and section.has("coverage"):
        raise ProfileError(
            _join(path, "coverage"), f"recall coverage applies to entity lists, not {field_type!r}"
        )
    if (
        compare == "containment"
        and field_type not in ("name", "free_text", "containment")
        and section.has("compare")
    ):
        raise ProfileError(
            _join(path, "compare"),
            f"containment applies to name, free_text and containment fields, not {field_type!r}",
        )
    return FieldSpec(
        name=name,
        field_type=field_type,
        compare=compare,
        coverage_scoring=coverage,
        vocabulary=vocabulary,
        predicted_value_type_override=override,
        subclasses=subclasses,
    )


def _type_override(value: Any, path: str, context: _ProfileContext) -> PredictedValueTypeOverride:
    """Read ``{tokens, type}`` for a predicted-value type override."""
    section = _Section(value, path)
    tokens = section.take("tokens", _list_of(_lowercase_token, non_empty=True), required=True)
    field_type = section.take("type", context.field_type, required=True)
    section.finish(("tokens", "type"))
    return PredictedValueTypeOverride(tokens=frozenset(tokens), field_type=field_type)


def _fields(value: Any, path: str, context: _ProfileContext) -> tuple[FieldSpec, ...]:
    """Read a ``{field name: field}`` mapping."""
    section = _Section(value, path)
    specs = []
    for name, raw in section.data.items():
        field_path = _join(path, name)
        _identifier(name, field_path)
        specs.append(_field(name, raw, field_path, context))
    return tuple(specs)


def _variant(
    value: Any, path: str, context: _ProfileContext, class_fields: dict[str, FieldSpec]
) -> RowVariant:
    """Read one row variant and check that every field it names exists."""
    section = _Section(value, path)
    name = section.take("name", _identifier, required=True)
    added_fields = section.take("added_fields", lambda raw, raw_path: _fields(raw, raw_path, context), ())
    for spec in added_fields:
        if spec.name in class_fields:
            raise ProfileError(
                _join(_join(path, "added_fields"), spec.name), "is already a field of the class"
            )
    known = {**class_fields, **{spec.name: spec for spec in added_fields}}

    def known_field(raw: Any, raw_path: str) -> str:
        """A field of the class or of this variant."""
        field_name = _identifier(raw, raw_path)
        if field_name not in known:
            raise ProfileError(raw_path, f"{field_name!r} is not a field of the class or of this variant")
        return field_name

    marker = section.take(
        "marker", lambda raw, raw_path: _marker(raw, raw_path, known_field, known), required=True
    )
    guard = section.take("guard", lambda raw, raw_path: _guard(raw, raw_path, known_field))
    documented_absent = section.take("documented_absent_fields", _list_of(known_field), ())
    overrides = section.take("field_type_overrides", _string_map(known_field, context.field_type), {})
    section.finish(
        ("name", "marker", "guard", "documented_absent_fields", "field_type_overrides", "added_fields")
    )
    return RowVariant(
        name=name,
        marker=marker,
        guard=guard,
        documented_absent_fields=documented_absent,
        field_type_overrides=FrozenMapping(overrides),
        added_fields=added_fields,
    )


def _marker(
    value: Any, path: str, known_field: Callable[[Any, str], str], known: dict[str, FieldSpec]
) -> VariantMarker:
    """Read a variant marker; its values must be tokens of the marker field's vocabulary when it has one."""
    section = _Section(value, path)
    field_name = section.take("field", known_field, required=True)
    values = section.take("values", _list_of(_lowercase_token, non_empty=True), required=True)
    fallback_fields = section.take("fallback_fields", _list_of(known_field), ())
    skip_when_unstated = section.take("skip_when_unstated", _boolean, False)
    derive_from = section.take("derive_from", _choice(("subclass",)))
    section.finish(("field", "values", "fallback_fields", "skip_when_unstated", "derive_from"))
    vocabulary = known[field_name].vocabulary
    if vocabulary is not None:
        for index, marker_value in enumerate(values):
            if marker_value not in vocabulary.tokens:
                raise ProfileError(
                    f"{path}.values[{index}]", f"{marker_value!r} is not a token of field {field_name!r}"
                )
    return VariantMarker(
        field=field_name,
        values=values,
        fallback_fields=fallback_fields,
        skip_when_unstated=skip_when_unstated,
        derive_from=derive_from,
    )


def _guard(value: Any, path: str, known_field: Callable[[Any, str], str]) -> VariantGuard:
    """Read a variant guard."""
    section = _Section(value, path)
    field_name = section.take("field", known_field, required=True)
    allowed = section.take("allowed", _list_of(_lowercase_token, non_empty=True), required=True)
    allow_empty = section.take("allow_empty", _boolean, True)
    section.finish(("field", "allowed", "allow_empty"))
    return VariantGuard(field=field_name, allowed=allowed, allow_empty=allow_empty)


def _synonym(value: Any, path: str) -> str:
    """A regular expression that compiles."""
    pattern = _text(value, path)
    try:
        re.compile(pattern)
    except re.error as error:
        raise ProfileError(path, f"is not a valid regular expression: {error}") from None
    return pattern


def _class(value: Any, path: str, context: _ProfileContext) -> ClassSpec:
    """Read one class with its fields, subclasses and variants."""
    section = _Section(value, path)
    key = section.take("key", _identifier, required=True)
    status = section.take("status", _choice(("live", "retired")), required=True)
    sorter_label = section.take("sorter_label", _text, key)
    synonyms = section.take("synonyms", _list_of(_synonym), ())
    subclasses = section.take(
        "subclasses", lambda raw, raw_path: _vocabulary(raw, raw_path, context.unknown_label)
    )
    accepted_unscored = section.take("accepted_unscored_fields", _list_of(_identifier), ())
    fields = section.take("fields", lambda raw, raw_path: _fields(raw, raw_path, context), required=True)
    class_fields = {spec.name: spec for spec in fields}
    variants = section.take(
        "variants",
        _list_of(lambda raw, raw_path: _variant(raw, raw_path, context, class_fields), unique=False),
        (),
    )
    section.finish(
        (
            "key",
            "status",
            "sorter_label",
            "synonyms",
            "subclasses",
            "accepted_unscored_fields",
            "fields",
            "variants",
        )
    )
    if subclasses is not None and subclasses.fold_method not in ("catalog", "allowed_set"):
        raise ProfileError(
            _join(path, "subclasses.fold"),
            f"subclasses fold with catalog or allowed_set, not {subclasses.fold_method}",
        )
    subclass_tokens = subclasses.tokens if subclasses is not None else ()
    for spec in fields:
        for index, subclass in enumerate(spec.subclasses):
            if subclass not in subclass_tokens:
                raise ProfileError(
                    f"{path}.fields.{spec.name}.subclasses[{index}]",
                    f"{subclass!r} is not a subclass of {key!r}",
                )
    for name in accepted_unscored:
        if name in class_fields:
            raise ProfileError(
                _join(path, "accepted_unscored_fields"), f"{name!r} is a scored field of the class"
            )
    variant_names: set[str] = set()
    for index, variant in enumerate(variants):
        if variant.name in variant_names:
            raise ProfileError(f"{path}.variants[{index}].name", f"duplicate variant {variant.name!r}")
        variant_names.add(variant.name)
        if variant.marker.derive_from == "subclass":
            for value_index, marker_value in enumerate(variant.marker.values):
                if marker_value not in subclass_tokens:
                    raise ProfileError(
                        f"{path}.variants[{index}].marker.values[{value_index}]",
                        f"{marker_value!r} is not a subclass of {key!r}",
                    )
    return ClassSpec(
        key=key,
        status=status,
        fields=fields,
        sorter_label=sorter_label,
        synonyms=synonyms,
        subclasses=subclasses,
        accepted_unscored_fields=frozenset(accepted_unscored),
        variants=variants,
    )


# ---------------------------------------------------------------------------
# Settings sections
# ---------------------------------------------------------------------------


def _type_band(value: Any, path: str) -> TypeBand:
    """``always``, ``never`` or a ``[low, high]`` band."""
    if value in ("always", "never"):
        return TypeBand(value)
    low, high = _band(value, path)
    return TypeBand("band", low, high)


def _thresholds(value: Any, path: str, context: _ProfileContext) -> Thresholds:
    """Read the ``thresholds`` section."""
    section = _Section(value, path)

    def type_band_key(raw: Any, raw_path: str) -> str:
        """A registered field type, or the ``entity_list`` namespace."""
        return context.field_type(raw, raw_path)

    defaults = Thresholds()
    thresholds = Thresholds(
        ambiguous_band=section.take("ambiguous_band", _band, defaults.ambiguous_band),
        type_bands=FrozenMapping(section.take("type_bands", _string_map(type_band_key, _type_band), {})),
        list_match_threshold=section.take(
            "list_match_threshold", _unit_interval, defaults.list_match_threshold
        ),
    )
    section.finish(("ambiguous_band", "type_bands", "list_match_threshold"))
    return thresholds


def _simple_section(
    dataclass_type: type, readers: dict[str, Callable[[Any, str], Any]]
) -> Callable[[Any, str], Any]:
    """A reader for a flat settings section whose keys map to ``dataclass_type`` fields."""

    def read(value: Any, path: str) -> Any:
        """Read the keys that are present; the dataclass supplies the rest."""
        section = _Section(value, path)
        values = {key: section.take(key, reader) for key, reader in readers.items() if section.has(key)}
        section.finish(tuple(readers))
        return dataclass_type(**values)

    return read


def _optional(reader: Callable[[Any, str], Any]) -> Callable[[Any, str], Any]:
    """A reader that also accepts null."""

    def read(value: Any, path: str) -> Any:
        """Return None for null, else the inner reader's result."""
        return None if value is None else reader(value, path)

    return read


_AUDIT = _simple_section(AuditSettings, {"enabled": _boolean, "token_coverage": _unit_interval})
_PRESENCE = _simple_section(
    PresenceSettings, {"route_by_reasoning": _boolean, "embedding_threshold": _unit_interval}
)
_EMBEDDING = _simple_section(EmbeddingSettings, {"enabled": _boolean, "rescue_below": _unit_interval})
_LIST_MATCHING = _simple_section(ListMatchingSettings, {"role_words": _list_of(_lowercase_token)})
_TRACE_CAPTURE = _simple_section(
    TraceCaptureOptions,
    {
        "capture_confidence": _boolean,
        "capture_reasoning": _boolean,
        "confidence_min": _optional(_unit_interval),
        "confidence_band": _optional(_band),
        "compute_calibration_error": _boolean,
        "missing_confidence": _choice(_MISSING_CONFIDENCE_POLICIES),
    },
)
_FIELD_NAME_DEFAULTS = _simple_section(
    FieldNameDefaults, {"containment": _list_of(_identifier), "recall_coverage": _list_of(_identifier)}
)


def _plugin_name(value: Any, path: str) -> str:
    """A plug-in field type name that does not shadow a built-in type."""
    name = _identifier(value, path)
    if name in BUILTIN_SCALAR_TYPES or name == ENTITY_LIST_PREFIX:
        raise ProfileError(path, f"plug-in {name!r} would replace a built-in field type")
    return name


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

_TOP_LEVEL_KEYS = (
    "profile_format",
    "client",
    "version",
    "unknown_label",
    "unmapped_fields",
    "never_scored_fields",
    "retired_fields",
    "scorer_plugins",
    "field_name_defaults",
    "thresholds",
    "audit",
    "presence",
    "embedding",
    "list_matching",
    "trace_capture",
    "classification",
    "class_aliases",
    "classes",
)


def load_scoring_profile(source: str | os.PathLike[str] | Mapping[str, Any]) -> ScoringProfile:
    """Load, validate and freeze a scoring profile.

    ``source`` is a path to a YAML file or an already parsed mapping. Raises
    :class:`ProfileError` (naming the YAML path) for a duplicate YAML key, an unknown
    key, a missing required key, an unregistered field type, a label field without
    a vocabulary, a duplicate class, a variant naming a field the class lacks, a
    threshold that is not a finite number in ``[0, 1]`` and every other malformed
    value. A missing file raises ``FileNotFoundError``; a source that is neither a
    path nor a mapping raises ``TypeError``.
    """
    if isinstance(source, Mapping):
        data: Any = source
    elif isinstance(source, (str, os.PathLike)):
        data = _read_yaml(Path(source))
    else:
        raise TypeError(f"a scoring profile source is a path or a mapping, not {type(source).__name__}")
    root = _Section(data, "")
    profile_format = root.take("profile_format", lambda raw, path: raw, required=True)
    if type(profile_format) is not int or profile_format != PROFILE_FORMAT:
        raise ProfileError(
            "profile_format", f"this loader reads format {PROFILE_FORMAT}, got {profile_format!r}"
        )
    client = root.take("client", _text, required=True)
    version = root.take("version", _text, required=True)
    unknown_label = root.take("unknown_label", _text, "other")
    unmapped_fields = root.take("unmapped_fields", _choice(_UNMAPPED_FIELD_POLICIES), "heuristic")
    never_scored = root.take("never_scored_fields", _list_of(_text), ())
    retired = root.take("retired_fields", _list_of(_text), ())
    plugins = root.take("scorer_plugins", _list_of(_plugin_name), ())
    name_defaults = root.take("field_name_defaults", _FIELD_NAME_DEFAULTS, FieldNameDefaults())
    context = _ProfileContext(BUILTIN_SCALAR_TYPES + plugins, name_defaults, unknown_label)
    thresholds = root.take("thresholds", lambda raw, path: _thresholds(raw, path, context), Thresholds())
    audit = root.take("audit", _AUDIT, AuditSettings())
    presence = root.take("presence", _PRESENCE, PresenceSettings())
    embedding = root.take("embedding", _EMBEDDING, EmbeddingSettings())
    list_matching = root.take("list_matching", _LIST_MATCHING, ListMatchingSettings())
    trace_capture = root.take("trace_capture", _TRACE_CAPTURE, TraceCaptureOptions())
    classes = root.take(
        "classes",
        _list_of(lambda raw, path: _class(raw, path, context), unique=False, non_empty=True),
        required=True,
    )
    class_keys = [spec.key for spec in classes]
    _check_unique_classes(classes)
    match_order = root.take("classification", lambda raw, path: _classification(raw, path, class_keys), ())
    aliases = root.take("class_aliases", lambda raw, path: _class_aliases(raw, path, class_keys), {})
    root.finish(_TOP_LEVEL_KEYS)
    _check_inventory_alias_targets(classes)

    parts = dict(
        client=client,
        version=version,
        classes=classes,
        classification_match_order=tuple(match_order)
        + tuple(key for key in class_keys if key not in match_order),
        unknown_label=unknown_label,
        unmapped_fields=unmapped_fields,
        never_scored_fields=frozenset(never_scored),
        retired_fields=frozenset(retired),
        scorer_plugins=plugins,
        class_aliases=FrozenMapping(aliases),
        field_name_defaults=name_defaults,
        thresholds=thresholds,
        audit=audit,
        presence=presence,
        embedding=embedding,
        list_matching=list_matching,
        trace_capture=trace_capture,
    )
    content_hash = hashlib.sha256(_canonical_json(_canonical_parts(parts))).hexdigest()
    return ScoringProfile(content_hash=content_hash, **parts)


def _check_unique_classes(classes: tuple[ClassSpec, ...]) -> None:
    """Raise for a repeated class key or sorter label."""
    keys: dict[str, int] = {}
    labels: dict[str, int] = {}
    for index, spec in enumerate(classes):
        if spec.key in keys:
            raise ProfileError(
                f"classes[{index}].key", f"duplicate class {spec.key!r} (also classes[{keys[spec.key]}])"
            )
        if spec.sorter_label in labels:
            raise ProfileError(
                f"classes[{index}].sorter_label",
                f"sorter label {spec.sorter_label!r} is already used by classes[{labels[spec.sorter_label]}]",
            )
        keys[spec.key] = index
        labels[spec.sorter_label] = index


def _classification(value: Any, path: str, class_keys: list[str]) -> tuple[str, ...]:
    """Read ``classification.match_order``: known classes, each once."""
    section = _Section(value, path)

    def known_class(raw: Any, raw_path: str) -> str:
        """A class key of this profile."""
        key = _identifier(raw, raw_path)
        if key not in class_keys:
            raise ProfileError(raw_path, f"{key!r} is not a class; known: {', '.join(class_keys)}")
        return key

    order = section.take("match_order", _list_of(known_class), ())
    section.finish(("match_order",))
    return order


def _class_aliases(value: Any, path: str, class_keys: list[str]) -> dict[str, str]:
    """Read ``class_aliases``: new names that point at existing classes."""
    aliases = _string_map(_identifier, _identifier)(value, path)
    for alias, target in aliases.items():
        if alias in class_keys:
            raise ProfileError(_join(path, alias), "an alias must not be a class key")
        if target not in class_keys:
            raise ProfileError(_join(path, alias), f"target {target!r} is not a class")
    return aliases


def _check_inventory_alias_targets(classes: tuple[ClassSpec, ...]) -> None:
    """Raise when an inventory alias points at a token no inventory vocabulary has (a typo).

    An inventory alias may point at another class's token: such a value is foreign
    to this vocabulary and folds to nothing.
    """
    located: list[tuple[str, LabelVocabulary]] = []
    for class_index, spec in enumerate(classes):
        for field_spec in spec.fields:
            if field_spec.vocabulary is not None:
                located.append(
                    (f"classes[{class_index}].fields.{field_spec.name}.vocabulary", field_spec.vocabulary)
                )
        for variant_index, variant in enumerate(spec.variants):
            for field_spec in variant.added_fields:
                if field_spec.vocabulary is not None:
                    located.append(
                        (
                            f"classes[{class_index}].variants[{variant_index}].added_fields.{field_spec.name}.vocabulary",
                            field_spec.vocabulary,
                        )
                    )
    inventory_tokens = {
        token
        for _, vocabulary in located
        if vocabulary.fold_method == "inventory"
        for token in vocabulary.tokens
    }
    for path, vocabulary in located:
        if vocabulary.fold_method != "inventory":
            continue
        for alias, target in vocabulary.aliases.items():
            if target not in inventory_tokens:
                raise ProfileError(
                    f"{path}.aliases.{alias}", f"target {target!r} is not a token of any inventory vocabulary"
                )


# ---------------------------------------------------------------------------
# Canonical form and hash
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> bytes:
    """Encode as canonical JSON: sorted keys, no whitespace, UTF-8, no NaN."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _canonical_vocabulary(vocabulary: LabelVocabulary | None) -> Any:
    """The canonical form of a vocabulary (ordered tables stay ordered)."""
    if vocabulary is None:
        return None
    return {
        "tokens": list(vocabulary.tokens),
        "fold": vocabulary.fold_method,
        "aliases": [[alias, target] for alias, target in vocabulary.aliases.items()],
        "prefix_aliases": [[prefix, target] for prefix, target in vocabulary.prefix_aliases.items()],
        "catalog": [[entry.key, entry.label] for entry in vocabulary.catalog],
        "unknown_token": vocabulary.unknown_token,
    }


def _canonical_field(spec: FieldSpec) -> dict[str, Any]:
    """The canonical form of a field."""
    override = spec.predicted_value_type_override
    return {
        "type": spec.field_type,
        "compare": spec.compare,
        "coverage": spec.coverage_scoring,
        "vocabulary": _canonical_vocabulary(spec.vocabulary),
        "predicted_value_type_override": (
            None if override is None else {"tokens": sorted(override.tokens), "type": override.field_type}
        ),
        "subclasses": sorted(spec.subclasses),
    }


def _canonical_variant(variant: RowVariant) -> dict[str, Any]:
    """The canonical form of a row variant."""
    marker, guard = variant.marker, variant.guard
    return {
        "name": variant.name,
        "marker": {
            "field": marker.field,
            "values": sorted(marker.values),
            "fallback_fields": list(marker.fallback_fields),
            "skip_when_unstated": marker.skip_when_unstated,
            "derive_from": marker.derive_from,
        },
        "guard": None
        if guard is None
        else {
            "field": guard.field,
            "allowed": sorted(guard.allowed),
            "allow_empty": guard.allow_empty,
        },
        "documented_absent_fields": sorted(variant.documented_absent_fields),
        "field_type_overrides": dict(variant.field_type_overrides),
        "added_fields": {spec.name: _canonical_field(spec) for spec in variant.added_fields},
    }


def _canonical_class(spec: ClassSpec) -> dict[str, Any]:
    """The canonical form of a class."""
    return {
        "key": spec.key,
        "status": spec.status,
        "sorter_label": spec.sorter_label,
        "synonyms": list(spec.synonyms),
        "subclasses": _canonical_vocabulary(spec.subclasses),
        "accepted_unscored_fields": sorted(spec.accepted_unscored_fields),
        "fields": {field_spec.name: _canonical_field(field_spec) for field_spec in spec.fields},
        "variants": [_canonical_variant(variant) for variant in spec.variants],
    }


def _canonical_parts(parts: dict[str, Any]) -> dict[str, Any]:
    """The canonical form of a profile's parts (everything but its hash)."""
    thresholds: Thresholds = parts["thresholds"]
    trace: TraceCaptureOptions = parts["trace_capture"]
    return {
        "profile_format": PROFILE_FORMAT,
        "client": parts["client"],
        "version": parts["version"],
        "unknown_label": parts["unknown_label"],
        "unmapped_fields": parts["unmapped_fields"],
        "never_scored_fields": sorted(parts["never_scored_fields"]),
        "retired_fields": sorted(parts["retired_fields"]),
        "scorer_plugins": sorted(parts["scorer_plugins"]),
        "class_aliases": dict(parts["class_aliases"]),
        "field_name_defaults": {
            "containment": sorted(parts["field_name_defaults"].containment),
            "recall_coverage": sorted(parts["field_name_defaults"].recall_coverage),
        },
        "thresholds": {
            "ambiguous_band": list(thresholds.ambiguous_band),
            "type_bands": {
                name: [band.mode, band.low, band.high] for name, band in thresholds.type_bands.items()
            },
            "list_match_threshold": thresholds.list_match_threshold,
        },
        "audit": {"enabled": parts["audit"].enabled, "token_coverage": parts["audit"].token_coverage},
        "presence": {
            "route_by_reasoning": parts["presence"].route_by_reasoning,
            "embedding_threshold": parts["presence"].embedding_threshold,
        },
        "embedding": {"enabled": parts["embedding"].enabled, "rescue_below": parts["embedding"].rescue_below},
        "list_matching": {"role_words": sorted(parts["list_matching"].role_words)},
        "trace_capture": {
            "capture_confidence": trace.capture_confidence,
            "capture_reasoning": trace.capture_reasoning,
            "confidence_min": trace.confidence_min,
            "confidence_band": None if trace.confidence_band is None else list(trace.confidence_band),
            "compute_calibration_error": trace.compute_calibration_error,
            "missing_confidence": trace.missing_confidence,
        },
        "classification_match_order": list(parts["classification_match_order"]),
        "classes": [_canonical_class(spec) for spec in parts["classes"]],
    }


def profile_canonical_form(profile: ScoringProfile) -> dict[str, Any]:
    """Return the JSON-ready canonical form whose SHA-256 is ``profile.content_hash``."""
    parts = {
        name: getattr(profile, name)
        for name in (
            "client",
            "version",
            "classes",
            "classification_match_order",
            "unknown_label",
            "unmapped_fields",
            "never_scored_fields",
            "retired_fields",
            "scorer_plugins",
            "class_aliases",
            "field_name_defaults",
            "thresholds",
            "audit",
            "presence",
            "embedding",
            "list_matching",
            "trace_capture",
        )
    }
    return _canonical_parts(parts)
