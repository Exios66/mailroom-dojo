"""Field scoring: one scorer per field type, entity lists, audits, presence, row variants, trace capture and
whole extractions."""

from ..normalization.dates import parse_date
from ..normalization.money import parse_money
from .audits import (
    audit_list_field,
    audit_scalar_field,
    date_grounded_in_document,
    field_audit_support,
    verification_tokens,
    verify_list_items,
)
from .category_presence import (
    PresenceSimilarity,
    disaggregate_clause_spans,
    presence_candidates,
    score_category_presence,
    split_clause_spans,
)
from .entity_lists import EntityListScore, as_list, element_similarity, score_entity_list
from .extraction import (
    ExtractionScoreResult,
    UnmappedFieldError,
    field_is_ambiguous,
    is_accepted_unscored_field,
    is_never_scored_field,
    score_extraction,
    score_field,
)
from .plugins import BUNDLED_PLUGINS
from .plugins.cms_identifiers import score_cms_doc_ref
from .row_variants import (
    ActiveVariant,
    active_row_variant,
    documented_absent_additions,
    is_unstated_marker,
    marker_from_fallback,
    variant_field_type,
)
from .scalar_scorers import (
    date_match_similarity,
    score_containment_field,
    score_date_field,
    score_free_text_field,
    score_id_field,
    score_label_field,
    score_money_field,
    score_name_field,
)
from .scorer_registry import (
    BUILTIN_SCORERS,
    FieldScorerRegistry,
    ResolvedFieldType,
    ScalarScorer,
    ScorerResultError,
    UnknownFieldTypeError,
    heuristic_field_type,
    is_entity_list,
)
from .similarity_backend import (
    EmbeddingRescue,
    SimilarityBackend,
    SimilarityFallback,
    with_embedding_rescue,
)
from .trace_capture import (
    TRACE_FIELDS,
    TraceCaptureOptions,
    capture_trace,
    confidence_calibration_error,
    confidence_gate,
    empty_trace_payload,
    parse_confidence,
    parse_reasoning,
)

__all__ = [
    "ActiveVariant",
    "BUILTIN_SCORERS",
    "BUNDLED_PLUGINS",
    "EmbeddingRescue",
    "EntityListScore",
    "ExtractionScoreResult",
    "FieldScorerRegistry",
    "PresenceSimilarity",
    "ResolvedFieldType",
    "ScalarScorer",
    "SimilarityBackend",
    "SimilarityFallback",
    "TRACE_FIELDS",
    "TraceCaptureOptions",
    "ScorerResultError",
    "UnknownFieldTypeError",
    "UnmappedFieldError",
    "active_row_variant",
    "as_list",
    "audit_list_field",
    "audit_scalar_field",
    "capture_trace",
    "confidence_calibration_error",
    "confidence_gate",
    "date_grounded_in_document",
    "date_match_similarity",
    "disaggregate_clause_spans",
    "documented_absent_additions",
    "element_similarity",
    "empty_trace_payload",
    "field_audit_support",
    "field_is_ambiguous",
    "heuristic_field_type",
    "is_accepted_unscored_field",
    "is_entity_list",
    "is_never_scored_field",
    "is_unstated_marker",
    "marker_from_fallback",
    "parse_confidence",
    "parse_date",
    "parse_money",
    "parse_reasoning",
    "presence_candidates",
    "score_category_presence",
    "score_cms_doc_ref",
    "score_containment_field",
    "score_date_field",
    "score_entity_list",
    "score_extraction",
    "score_field",
    "score_free_text_field",
    "score_id_field",
    "score_label_field",
    "score_money_field",
    "score_name_field",
    "split_clause_spans",
    "variant_field_type",
    "verification_tokens",
    "verify_list_items",
    "with_embedding_rescue",
]
