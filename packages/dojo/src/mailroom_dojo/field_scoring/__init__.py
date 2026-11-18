"""Field scoring: one scorer per field type, the registry that picks them, and embedding rescue."""

from ..normalization.dates import parse_date
from ..normalization.money import parse_money
from .plugins import BUNDLED_PLUGINS
from .plugins.cms_identifiers import score_cms_doc_ref
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

__all__ = [
    "BUILTIN_SCORERS",
    "BUNDLED_PLUGINS",
    "EmbeddingRescue",
    "FieldScorerRegistry",
    "ResolvedFieldType",
    "ScalarScorer",
    "SimilarityBackend",
    "SimilarityFallback",
    "UnknownFieldTypeError",
    "date_match_similarity",
    "heuristic_field_type",
    "is_entity_list",
    "parse_date",
    "parse_money",
    "score_cms_doc_ref",
    "score_containment_field",
    "score_date_field",
    "score_free_text_field",
    "score_id_field",
    "score_label_field",
    "score_money_field",
    "score_name_field",
    "with_embedding_rescue",
]
