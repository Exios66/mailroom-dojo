"""Scoring profiles: one client's classes, fields, vocabularies and thresholds as immutable data."""

from .field_types import BUILTIN_SCALAR_TYPES, ENTITY_LIST_PREFIX, element_type, is_entity_list
from .frozen import FrozenMapping
from .label_folding import FOLD_METHODS, CatalogEntry, LabelVocabulary
from .loading import PROFILE_FORMAT, load_scoring_profile, profile_canonical_form
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

__all__ = [
    "BUILTIN_SCALAR_TYPES",
    "ENTITY_LIST_PREFIX",
    "FOLD_METHODS",
    "PROFILE_FORMAT",
    "AuditSettings",
    "CatalogEntry",
    "ClassSpec",
    "EmbeddingSettings",
    "FieldNameDefaults",
    "FieldSpec",
    "FrozenMapping",
    "LabelVocabulary",
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
    "element_type",
    "is_entity_list",
    "load_scoring_profile",
    "profile_canonical_form",
]
