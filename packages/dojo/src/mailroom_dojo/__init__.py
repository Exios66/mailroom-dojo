"""mailroom_dojo: deterministic scoring for the Mailroom.

Scoring rules come from an immutable client profile (``load_scoring_profile``);
nothing is read from the environment or kept in module state.
"""

from .field_scoring import (
    EmbeddingRescue,
    FieldScorerRegistry,
    SimilarityBackend,
    SimilarityFallback,
    is_entity_list,
    score_cms_doc_ref,
    score_containment_field,
    score_date_field,
    score_free_text_field,
    score_id_field,
    score_label_field,
    score_money_field,
    score_name_field,
    with_embedding_rescue,
)
from .normalization import normalize_text, parse_date, parse_money
from .profile import (
    ClassSpec,
    FieldSpec,
    LabelVocabulary,
    ProfileError,
    RowVariant,
    ScoringProfile,
    load_scoring_profile,
)
from .values import is_empty_value, parse_json_container

__all__ = [
    "ClassSpec",
    "EmbeddingRescue",
    "FieldScorerRegistry",
    "FieldSpec",
    "LabelVocabulary",
    "ProfileError",
    "RowVariant",
    "ScoringProfile",
    "SimilarityBackend",
    "SimilarityFallback",
    "is_empty_value",
    "is_entity_list",
    "load_scoring_profile",
    "normalize_text",
    "parse_date",
    "parse_json_container",
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
