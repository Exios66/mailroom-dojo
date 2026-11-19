"""The scoring profile: one client's scoring rules as immutable data.

A :class:`ScoringProfile` is loaded from ``config/clients/<client>/scoring.yaml`` by
:func:`mailroom_dojo.profile.loading.load_scoring_profile` and passed to every
function that needs a rule. Nothing here reads a file, the environment or a global.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .field_types import is_entity_list
from .frozen import FrozenMapping
from .label_folding import LabelVocabulary

__all__ = [
    "AuditSettings",
    "ClassSpec",
    "EmbeddingSettings",
    "FieldNameDefaults",
    "FieldSpec",
    "ListMatchingSettings",
    "PredictedValueTypeOverride",
    "PresenceSettings",
    "ProfileError",
    "RowVariant",
    "ScoringProfile",
    "Thresholds",
    "TraceCaptureOptions",
    "TypeBand",
    "VariantGuard",
    "VariantMarker",
]

ClassStatus = Literal["live", "retired"]
CompareMethod = Literal["default", "containment"]
CoverageScoring = Literal["f1", "recall"]
UnmappedFieldPolicy = Literal["heuristic", "skip", "error"]
MissingConfidencePolicy = Literal["absent", "assume_1", "assume_0"]


class ProfileError(ValueError):
    """A scoring profile is malformed. The message names the YAML path of the problem."""

    def __init__(self, path: str, problem: str) -> None:
        """Record the YAML ``path`` (``classes[2].fields.claim_type.type``) and the ``problem``."""
        super().__init__(f"{path}: {problem}" if path else problem)
        self.path = path
        self.problem = problem


@dataclass(frozen=True)
class PredictedValueTypeOverride:
    """Score a field as ``field_type`` whenever its predicted value is one of ``tokens``.

    The predicted value is compared as ``str(value or "")``, stripped and lowercased.
    """

    tokens: frozenset[str]
    field_type: str

    def __post_init__(self) -> None:
        """Freeze the token set."""
        object.__setattr__(self, "tokens", frozenset(self.tokens))

    def matches(self, predicted_value: object) -> bool:
        """True when the predicted value, stripped and lowercased, is one of the tokens."""
        return str(predicted_value or "").strip().lower() in self.tokens


@dataclass(frozen=True)
class FieldSpec:
    """One extraction field of a class and how it is scored.

    ``compare="containment"`` scores the expected text's tokens found in the
    prediction (for fields typed ``name``, ``free_text`` or ``containment``).
    ``coverage_scoring="recall"`` scores an entity list by ground-truth coverage
    instead of F1, for labels that sample a document rather than list it fully.
    ``subclasses`` names the subclasses the field belongs to; empty means all.
    """

    name: str
    field_type: str
    compare: CompareMethod = "default"
    coverage_scoring: CoverageScoring = "f1"
    vocabulary: LabelVocabulary | None = None
    predicted_value_type_override: PredictedValueTypeOverride | None = None
    subclasses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Freeze the subclass list."""
        object.__setattr__(self, "subclasses", tuple(self.subclasses))

    @property
    def is_entity_list(self) -> bool:
        """True when the field holds a list of entities."""
        return is_entity_list(self.field_type)


@dataclass(frozen=True)
class VariantMarker:
    """The expected field whose value marks a row as a variant.

    ``values`` are the marker tokens (lowercase). ``fallback_fields`` are predicted
    fields a marker token may be read from when the prediction leaves the marker
    field blank. ``skip_when_unstated`` leaves the marker unscored when the
    prediction states no marker token anywhere. ``derive_from="subclass"`` means the
    labels side fills the marker from the document's subclass.
    """

    field: str
    values: tuple[str, ...]
    fallback_fields: tuple[str, ...] = ()
    skip_when_unstated: bool = False
    derive_from: Literal["subclass"] | None = None

    def __post_init__(self) -> None:
        """Freeze the value and fallback lists."""
        object.__setattr__(self, "values", tuple(self.values))
        object.__setattr__(self, "fallback_fields", tuple(self.fallback_fields))


@dataclass(frozen=True)
class VariantGuard:
    """An expected field that must hold one of ``allowed`` (or be empty when ``allow_empty``) for the variant
    to apply."""

    field: str
    allowed: tuple[str, ...]
    allow_empty: bool = True

    def __post_init__(self) -> None:
        """Freeze the allowed list."""
        object.__setattr__(self, "allowed", tuple(self.allowed))


@dataclass(frozen=True)
class RowVariant:
    """A kind of row within a class that is scored by extra rules.

    A row is the variant when its expected marker field holds a marker value and the
    guard (if any) holds. Then ``documented_absent_fields`` are scored as expected
    empty, ``field_type_overrides`` replace field types, and ``added_fields`` exist
    only on such rows. The rules are applied by the extraction scorer.
    """

    name: str
    marker: VariantMarker
    guard: VariantGuard | None = None
    documented_absent_fields: tuple[str, ...] = ()
    field_type_overrides: FrozenMapping[str, str] = field(default_factory=FrozenMapping)
    added_fields: tuple[FieldSpec, ...] = ()

    def __post_init__(self) -> None:
        """Freeze the lists and the override map."""
        object.__setattr__(self, "documented_absent_fields", tuple(self.documented_absent_fields))
        object.__setattr__(self, "field_type_overrides", FrozenMapping(self.field_type_overrides))
        object.__setattr__(self, "added_fields", tuple(self.added_fields))

    def added_field(self, name: str) -> FieldSpec | None:
        """Return the added field called ``name``, or None."""
        return next((spec for spec in self.added_fields if spec.name == name), None)


@dataclass(frozen=True)
class ClassSpec:
    """One document class: its status, classification hints, subclasses and fields.

    ``status="retired"`` keeps a class scoreable for old records but takes it out of
    the classifier's label set. ``sorter_label`` is the label the sorter emits for the
    class. ``synonyms`` are extra regular expressions that classify text as this
    class. ``accepted_unscored_fields`` may appear in a prediction and are never
    scored unless a caller's explicit field-type map names them.
    """

    key: str
    status: ClassStatus
    fields: tuple[FieldSpec, ...]
    sorter_label: str = ""
    synonyms: tuple[str, ...] = ()
    subclasses: LabelVocabulary | None = None
    accepted_unscored_fields: frozenset[str] = frozenset()
    variants: tuple[RowVariant, ...] = ()

    def __post_init__(self) -> None:
        """Freeze the lists and default the sorter label to the key."""
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "synonyms", tuple(self.synonyms))
        object.__setattr__(self, "accepted_unscored_fields", frozenset(self.accepted_unscored_fields))
        object.__setattr__(self, "variants", tuple(self.variants))
        if not self.sorter_label:
            object.__setattr__(self, "sorter_label", self.key)

    @property
    def is_live(self) -> bool:
        """True for a class the pipeline extracts today."""
        return self.status == "live"

    @property
    def field_names(self) -> tuple[str, ...]:
        """The field names, in profile order."""
        return tuple(spec.name for spec in self.fields)

    def field(self, name: str) -> FieldSpec | None:
        """Return the field called ``name``, or None."""
        return next((spec for spec in self.fields if spec.name == name), None)

    def field_types(self) -> dict[str, str]:
        """Return a fresh ``{field name: field type}`` dict in profile order."""
        return {spec.name: spec.field_type for spec in self.fields}

    def variant_added_field(self, name: str) -> FieldSpec | None:
        """Return the first variant-only field called ``name``, or None."""
        for variant in self.variants:
            spec = variant.added_field(name)
            if spec is not None:
                return spec
        return None


@dataclass(frozen=True)
class TypeBand:
    """The ambiguity rule for one field type: always ambiguous, never, or a half-open score band."""

    mode: Literal["always", "never", "band"]
    low: float | None = None
    high: float | None = None

    def is_ambiguous(self, score: float) -> bool:
        """Apply the rule to ``score`` (a band is half open: ``low <= score < high``)."""
        if self.mode == "always":
            return True
        if self.mode == "never":
            return False
        return self.low <= score < self.high


@dataclass(frozen=True)
class Thresholds:
    """Score thresholds: the judge-review band, its per-type overrides and the list match threshold."""

    ambiguous_band: tuple[float, float] = (0.5, 0.85)
    type_bands: FrozenMapping[str, TypeBand] = field(default_factory=FrozenMapping)
    list_match_threshold: float = 0.6

    def __post_init__(self) -> None:
        """Freeze the band and the type-band map."""
        object.__setattr__(self, "ambiguous_band", tuple(self.ambiguous_band))
        object.__setattr__(self, "type_bands", FrozenMapping(self.type_bands))

    def band_for(self, field_type: str) -> TypeBand:
        """Return the rule for ``field_type``: its own type band, its namespace's, or the global band."""
        band = self.type_bands.get(field_type) or self.type_bands.get(field_type.split(":", 1)[0])
        if band is not None:
            return band
        low, high = self.ambiguous_band
        return TypeBand("band", low, high)

    def is_ambiguous(self, field_type: str, score: float) -> bool:
        """True when a ``field_type`` field scoring ``score`` should go to the judge."""
        return self.band_for(field_type).is_ambiguous(score)


@dataclass(frozen=True)
class AuditSettings:
    """The factuality audit: whether predicted items are checked against the document text, and the token
    coverage that counts as grounded."""

    enabled: bool = True
    token_coverage: float = 0.7


@dataclass(frozen=True)
class PresenceSettings:
    """Category presence: whether reasoning entries may route clause spans, and the similarity a span
    needs."""

    route_by_reasoning: bool = True
    embedding_threshold: float = 0.7


@dataclass(frozen=True)
class EmbeddingSettings:
    """Embedding rescue: whether an injected similarity backend may be used, and the string score below which
    it is asked."""

    enabled: bool = False
    rescue_below: float = 0.7


@dataclass(frozen=True)
class ListMatchingSettings:
    """Entity-list matching: party-role words that recall-scored lists credit when the prediction names a
    party."""

    role_words: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Freeze the word set."""
        object.__setattr__(self, "role_words", frozenset(self.role_words))


@dataclass(frozen=True)
class TraceCaptureOptions:
    """What the scorer captures from model-emitted ``confidence`` and ``reasoning`` (never scored).

    ``confidence_min`` flags predictions below a floor and ``confidence_band``
    tags a half-open band (``None`` turns each off). ``missing_confidence`` decides
    what a missing confidence counts as for gating only: the calibration error
    needs a stated confidence and stays None without one, whatever the policy (the
    old Dojo's behaviour, though its comment said otherwise).
    """

    capture_confidence: bool = True
    capture_reasoning: bool = True
    confidence_min: float | None = None
    confidence_band: tuple[float, float] | None = None
    compute_calibration_error: bool = True
    missing_confidence: MissingConfidencePolicy = "absent"

    def __post_init__(self) -> None:
        """Freeze the band."""
        if self.confidence_band is not None:
            object.__setattr__(self, "confidence_band", tuple(self.confidence_band))


@dataclass(frozen=True)
class FieldNameDefaults:
    """Scoring rules by field name, for fields a class does not declare or does not set.

    A declared field without an explicit ``compare`` or ``coverage`` takes these;
    an undeclared field (an unmapped expected key, a caller's own field-type map, a
    class the profile does not know) always does.
    """

    containment: frozenset[str] = frozenset()
    recall_coverage: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Freeze the name sets."""
        object.__setattr__(self, "containment", frozenset(self.containment))
        object.__setattr__(self, "recall_coverage", frozenset(self.recall_coverage))


@dataclass(frozen=True, eq=False)
class ScoringProfile:
    """One client's scoring rules. Immutable; equal and hashed by :attr:`content_hash`.

    ``classes`` keep the profile order; ``classification_match_order`` is the order
    in which class patterns are tried when classifying free text (most specific
    first). ``unmapped_fields`` says how an expected key the class does not declare
    is typed: by its name (``heuristic``), left out (``skip``) or refused (``error``).
    """

    client: str
    version: str
    classes: tuple[ClassSpec, ...]
    classification_match_order: tuple[str, ...]
    unknown_label: str = "other"
    unmapped_fields: UnmappedFieldPolicy = "heuristic"
    never_scored_fields: frozenset[str] = frozenset()
    retired_fields: frozenset[str] = frozenset()
    scorer_plugins: tuple[str, ...] = ()
    class_aliases: FrozenMapping[str, str] = field(default_factory=FrozenMapping)
    field_name_defaults: FieldNameDefaults = field(default_factory=FieldNameDefaults)
    thresholds: Thresholds = field(default_factory=Thresholds)
    audit: AuditSettings = field(default_factory=AuditSettings)
    presence: PresenceSettings = field(default_factory=PresenceSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    list_matching: ListMatchingSettings = field(default_factory=ListMatchingSettings)
    trace_capture: TraceCaptureOptions = field(default_factory=TraceCaptureOptions)
    #: SHA-256 of the canonical form of every rule above. Derived on every construction
    #: (``dataclasses.replace`` included), never passed in, so it always names these rules.
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        """Freeze every collection and derive :attr:`content_hash` from the frozen rules."""
        object.__setattr__(self, "classes", tuple(self.classes))
        object.__setattr__(self, "classification_match_order", tuple(self.classification_match_order))
        object.__setattr__(self, "never_scored_fields", frozenset(self.never_scored_fields))
        object.__setattr__(self, "retired_fields", frozenset(self.retired_fields))
        object.__setattr__(self, "scorer_plugins", tuple(self.scorer_plugins))
        object.__setattr__(self, "class_aliases", FrozenMapping(self.class_aliases))
        from .loading import profile_content_hash  # loading imports this module

        object.__setattr__(self, "content_hash", profile_content_hash(self))

    def __eq__(self, other: object) -> bool:
        """Two profiles are equal when their content hashes are."""
        if not isinstance(other, ScoringProfile):
            return NotImplemented
        return self.content_hash == other.content_hash

    def __hash__(self) -> int:
        """Hash by content hash."""
        return hash(self.content_hash)

    @property
    def class_keys(self) -> tuple[str, ...]:
        """Every class key, live and retired, in profile order."""
        return tuple(spec.key for spec in self.classes)

    @property
    def live_class_keys(self) -> tuple[str, ...]:
        """The live class keys, in profile order."""
        return tuple(spec.key for spec in self.classes if spec.is_live)

    @property
    def retired_class_keys(self) -> tuple[str, ...]:
        """The retired class keys, in profile order."""
        return tuple(spec.key for spec in self.classes if not spec.is_live)

    def class_spec(self, class_key: str | None) -> ClassSpec | None:
        """Return the class named ``class_key`` (or by a class alias), or None."""
        if not class_key:
            return None
        key = self.class_aliases.get(class_key, class_key)
        return next((spec for spec in self.classes if spec.key == key), None)

    def require_class(self, class_key: str) -> ClassSpec:
        """Return the class named ``class_key``; raise ``KeyError`` naming the known classes."""
        spec = self.class_spec(class_key)
        if spec is None:
            raise KeyError(f"unknown document class {class_key!r}; known: {list(self.class_keys)}")
        return spec

    def field_types(self, class_key: str) -> dict[str, str]:
        """Return a fresh ``{field name: field type}`` dict for a class; raise ``KeyError`` for an unknown
        class."""
        return self.require_class(class_key).field_types()

    def label_vocabulary(self, class_key: str | None, field_name: str) -> LabelVocabulary | None:
        """Return the vocabulary of a class's label field (a declared field or a variant-only one), or
        None."""
        spec = self.class_spec(class_key)
        if spec is None:
            return None
        field_spec = spec.field(field_name) or spec.variant_added_field(field_name)
        return field_spec.vocabulary if field_spec is not None else None

    def canonical_label(self, class_key: str | None, field_name: str, value: object) -> str:
        """Return the token a label value folds onto, or ``""`` when it folds onto none.

        The class key is stripped and lowercased first. A field with no vocabulary in
        the profile folds nothing.
        """
        vocabulary = self.label_vocabulary(str(class_key or "").strip().lower(), field_name)
        if vocabulary is None:
            return ""
        return vocabulary.fold(value)

    def normalize_subclass(self, class_key: str | None, value: object) -> str:
        """Return the subclass token ``value`` stands for within a class, or :attr:`unknown_label`.

        An unknown class, a class without subclasses and a value that folds onto
        nothing all give the unknown label.
        """
        spec = self.class_spec(class_key)
        if spec is None or spec.subclasses is None or not spec.subclasses.tokens:
            return self.unknown_label
        return spec.subclasses.normalize(value)

    def compares_by_containment(self, class_key: str | None, field_name: str) -> bool:
        """True when a field is scored by expected-within-predicted token containment."""
        spec = self.class_spec(class_key)
        field_spec = spec.field(field_name) if spec is not None else None
        if field_spec is not None:
            return field_spec.compare == "containment"
        return field_name in self.field_name_defaults.containment

    def scores_coverage_by_recall(self, class_key: str | None, field_name: str) -> bool:
        """True when an entity-list field is scored by ground-truth coverage (recall) instead of F1."""
        spec = self.class_spec(class_key)
        field_spec = spec.field(field_name) if spec is not None else None
        if field_spec is not None:
            return field_spec.coverage_scoring == "recall"
        return field_name in self.field_name_defaults.recall_coverage
