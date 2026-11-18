"""Element scorer for CMS DE-SynPUF supporting-document items (the ``cms_doc_ref`` type).

A CMS claim row lists its supporting documents as identifier strings ("provider NPI
5040521237", "NDC 00703051002", "facility provider 360029") or descriptors
("HCPCS detail (1 codes)"). Identifiers match only as exact tokens of the same kind;
a near-miss identifier is a different provider or drug, so prose similarity earns
nothing.
"""

from __future__ import annotations

import re

from ..similarity_backend import EmbeddingRescue

__all__ = ["CMS_DOC_REF_TYPE", "cms_document_signature", "score_cms_doc_ref"]

#: The field type name this plug-in registers.
CMS_DOC_REF_TYPE = "cms_doc_ref"

_FACILITY = re.compile(
    r"\bfacility\s+provider\s*(?:\b(?:id|identifier|number|no)\b)?[\s:#.-]*([a-z0-9]*\d[a-z0-9]*)"
)
_NPI = re.compile(r"(?<![a-z0-9])(\d{10})(?![a-z0-9])")
_NDC_DIGITS = re.compile(r"(?<![a-z0-9])(\d{9,11})(?![a-z0-9])")
_NDC_SEGMENTS = re.compile(r"(?<![a-z0-9-])(\d{4,5})-(\d{3,4})-(\d{1,2})(?![a-z0-9-])")
_ELEVEN_DIGITS = re.compile(r"(?<![a-z0-9])(\d{11})(?![a-z0-9])")
_NDC_WORD = re.compile(r"\bndc\b")
_HYPHEN_BETWEEN_DIGITS = re.compile(r"(?<=\d)-(?=\d)")
_NOT_LETTERS = re.compile(r"[^a-z]+")


def cms_document_signature(text: object) -> tuple[str, str]:
    """Classify one supporting-document item as ``(kind, value)``.

    Kinds, tried in order: ``facility_provider`` (the alphanumeric id after
    "facility provider"); ``ndc`` from a hyphenated NDC, each segment padded to
    the 11-digit 5-4-2 form; ``ndc`` from 9 to 11 digits after the word NDC,
    left-padded to 11; ``npi`` (10 digits); ``ndc`` (any 11 digits); otherwise
    ``descriptor``, the item's letters with the words "code" and "codes" dropped.
    """
    raw = str(text or "").strip().lower()
    facility = _FACILITY.search(raw)
    if facility:
        return "facility_provider", facility.group(1)
    if _NDC_WORD.search(raw):
        segmented = _NDC_SEGMENTS.search(raw)
        if segmented:
            labeler, product, package = segmented.groups()
            return "ndc", labeler.zfill(5) + product.zfill(4) + package.zfill(2)
    squeezed = _HYPHEN_BETWEEN_DIGITS.sub("", raw)
    if _NDC_WORD.search(squeezed):
        match = _NDC_DIGITS.search(squeezed)
        if match:
            return "ndc", match.group(1).zfill(11)
    npi = _NPI.search(squeezed)
    if npi:
        return "npi", npi.group(1)
    eleven_digits = _ELEVEN_DIGITS.search(squeezed)
    if eleven_digits:
        return "ndc", eleven_digits.group(1)
    words = [word for word in _NOT_LETTERS.sub(" ", raw).split() if word not in ("code", "codes")]
    return "descriptor", " ".join(words)


def score_cms_doc_ref(predicted: object, expected: object, rescue: EmbeddingRescue | None = None) -> float:
    """Return 1.0 when both items have the same kind and value, else 0.0 (an empty value never matches).

    ``rescue`` is accepted for the scorer signature and never used.
    """
    predicted_kind, predicted_value = cms_document_signature(predicted)
    expected_kind, expected_value = cms_document_signature(expected)
    if not predicted_value or not expected_value:
        return 0.0
    return 1.0 if (predicted_kind, predicted_value) == (expected_kind, expected_value) else 0.0
