"""Model-emitted ``confidence`` and ``reasoning``: captured for the run log, never scored.

:func:`capture_trace` peels both off a prediction into a payload with a stable shape
(:data:`TRACE_KEYS`), tags the confidence with a gate (``pass``, ``below_min``,
``in_band`` or ``missing``) and, when asked, the calibration error against the
document's correctness. What is captured and how the gate works is set by
:class:`~mailroom_dojo.profile.models.TraceCaptureOptions`, held on the profile.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..profile.models import TraceCaptureOptions

__all__ = [
    "CONFIDENCE_GATES",
    "TRACE_FIELDS",
    "TRACE_KEYS",
    "TraceCaptureOptions",
    "capture_trace",
    "confidence_calibration_error",
    "confidence_gate",
    "empty_trace_payload",
    "parse_confidence",
    "parse_reasoning",
]

#: Prediction keys that are model traces, not extraction fields: never scored.
TRACE_FIELDS: frozenset[str] = frozenset({"confidence", "reasoning"})

#: The keys of every trace payload, in order.
TRACE_KEYS: tuple[str, ...] = (
    "confidence",
    "reasoning",
    "confidence_gate",
    "calibration_error",
    "n_reasoning_entries",
)

#: The values ``confidence_gate`` can take.
CONFIDENCE_GATES: frozenset[str] = frozenset({"pass", "below_min", "in_band", "missing"})

_CONFIDENCE_KEYS = ("confidence", "score", "value")


def empty_trace_payload() -> dict[str, Any]:
    """Return the payload with every key set to None."""
    return {key: None for key in TRACE_KEYS}


def parse_confidence(value: object) -> float | None:
    """Read a model-emitted confidence as a number in ``[0, 1]`` rounded to four places, or None.

    A mapping is read through its first ``confidence``, ``score`` or ``value`` key.
    A number above 1 and at most 100 is a percentage. Blank, boolean, NaN,
    non-numeric and out-of-range values give None.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        for key in _CONFIDENCE_KEYS:
            if key in value:
                return parse_confidence(value.get(key))
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an integer too large for a float
        return None
    if number != number:
        return None
    if 1.0 < number <= 100.0:
        number = number / 100.0
    if number < 0.0 or number > 1.0:
        return None
    return round(number, 4)


def _normalize_entry(item: object) -> dict[str, Any] | None:
    """Read one reasoning entry as ``{field, evidence, section_ref}``, or None when it holds none of them.

    A string is evidence. In a mapping, evidence is the first truthy of
    ``evidence``, ``span``, ``text`` and the section the first truthy of
    ``section_ref``, ``section``, ``ref``.
    """
    if item is None or item == "":
        return None
    if isinstance(item, str):
        text = item.strip()
        return {"field": None, "evidence": text, "section_ref": None} if text else None
    if not isinstance(item, Mapping):
        return None
    field = item.get("field")
    evidence = item.get("evidence") or item.get("span") or item.get("text")
    section = item.get("section_ref") or item.get("section") or item.get("ref")
    if field is None and evidence is None and section is None:
        return None
    return {
        "field": None if field is None else str(field).strip() or None,
        "evidence": None if evidence is None else str(evidence),
        "section_ref": None if section is None else str(section).strip() or None,
    }


def _normalize_entries(items: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    """Normalize a sequence of entries, dropping those that hold nothing."""
    return [entry for entry in (_normalize_entry(item) for item in items) if entry]


def parse_reasoning(value: object) -> dict[str, Any] | None:
    """Read model reasoning as ``{"summary", "entries"}`` (plus ``"text"`` when given as text), or None.

    Accepts a string (the summary), a list of entries, a mapping with ``summary``,
    ``entries`` and ``text``, or a mapping that is itself one entry. Any other value
    is read as text.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return {"summary": text, "entries": [], "text": text}
    if isinstance(value, (list, tuple)):
        entries = _normalize_entries(value)
        if not entries:
            return None
        return {"summary": None, "entries": entries}
    if isinstance(value, Mapping):
        summary = value.get("summary")
        if summary is not None:
            summary = str(summary).strip() or None
        raw_entries = value.get("entries")
        entries = _normalize_entries(raw_entries) if isinstance(raw_entries, (list, tuple)) else []
        text = value.get("text")
        if not summary and not entries and not text:
            entry = _normalize_entry(value)
            if entry and ("field" in value or "evidence" in value):
                return {"summary": None, "entries": [entry]}
            return None
        payload: dict[str, Any] = {"summary": summary, "entries": entries}
        if text:
            payload["text"] = str(text)
        return payload
    text = str(value).strip()
    if not text:
        return None
    return {"summary": text, "entries": [], "text": text}


def _fill_missing_confidence(confidence: float | None, policy: str) -> float | None:
    """Fill a missing confidence by ``policy``: ``assume_1`` gives 1.0, ``assume_0`` gives 0.0, else None."""
    if confidence is not None:
        return confidence
    if policy == "assume_1":
        return 1.0
    if policy == "assume_0":
        return 0.0
    return None


def confidence_gate(confidence: float | None, options: TraceCaptureOptions) -> str:
    """Return ``missing``, ``below_min``, ``in_band`` (half open ``[low, high)``) or ``pass``.

    A missing confidence is first filled by ``options.missing_confidence``; the
    floor ``options.confidence_min`` is checked before the band.
    """
    filled = _fill_missing_confidence(confidence, str(options.missing_confidence or "absent"))
    if filled is None:
        return "missing"
    if options.confidence_min is not None and filled < float(options.confidence_min):
        return "below_min"
    band = options.confidence_band
    if band is not None and len(band) == 2:
        low, high = float(band[0]), float(band[1])
        if low <= filled < high:
            return "in_band"
    return "pass"


def confidence_calibration_error(confidence: object, correctness: object) -> float | None:
    """Return ``|confidence - correctness|`` rounded to four places when both are numbers in ``[0, 1]``, else
    None."""
    if confidence is None or correctness is None:
        return None
    try:
        confidence_value = float(confidence)
        correctness_value = float(correctness)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= confidence_value <= 1.0 and 0.0 <= correctness_value <= 1.0):
        return None
    return round(abs(confidence_value - correctness_value), 4)


def capture_trace(
    predicted: Mapping[str, Any] | None,
    options: TraceCaptureOptions,
    *,
    correctness: float | None = None,
) -> dict[str, Any]:
    """Return the trace payload of a prediction under ``options``.

    ``confidence`` and ``reasoning`` are parsed when captured; ``n_reasoning_entries``
    counts the parsed entries (None when there is no reasoning); the gate is set when
    either is captured; ``calibration_error`` compares the confidence with
    ``correctness`` (typically the document's overall score) when enabled. Keys a
    payload does not fill stay None.
    """
    source = dict(predicted or {})
    payload = empty_trace_payload()
    if options.capture_confidence:
        payload["confidence"] = parse_confidence(source.get("confidence"))
    if options.capture_reasoning:
        payload["reasoning"] = parse_reasoning(source.get("reasoning"))
        entries = (payload["reasoning"] or {}).get("entries") or []
        payload["n_reasoning_entries"] = len(entries) if payload["reasoning"] is not None else None
    if options.capture_confidence or options.capture_reasoning:
        payload["confidence_gate"] = confidence_gate(payload["confidence"], options)
    if options.compute_calibration_error:
        payload["calibration_error"] = confidence_calibration_error(payload["confidence"], correctness)
    return payload
