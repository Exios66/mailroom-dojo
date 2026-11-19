"""``Measure``: a number that carries its sample size, cost basis and label status.

Every Dojo layer returns ``Measure`` values instead of bare floats, so a report cannot
print a number without saying how many items it rests on, what its cost figures are
based on, how trustworthy the labels behind it are, what it is about (``subject``) and
which run produced it (``provenance``). A ``Measure`` validates itself when it is built;
an invalid one raises :class:`MeasureError` and never exists.

Rules 1, 3, 4, 5 and 7 are ported from the old Dojo's ``metrics/scorecard_honesty.py``
(issue numbers are the old repository's); rule 2 is new (an intended difference, see
``packages/dojo/docs/score-changelog.md``). The rest of that module is not ported here:
the D2 plan (``docs/plans/2026-11-18-dojo-d2-measures-plan.md``) names, for every public
function, the chunk that owns it or why it was dropped.

1. A value is a finite real number or ``None``: never a bool, NaN or infinity (the old
   ``_is_real_score`` and the golden canonical form, which rejects non-finite floats).
   Any ``numbers.Real`` but bool is accepted (numpy numbers too) and stored as ``float``.
2. ``n == 0`` has no value: nothing scored proves nothing. New: old scorers returned
   ``0.0`` for some empty inputs; only ``aggregate_quality_honest`` (#16) returned
   ``None`` for an empty population. ``n`` is a non-negative integer (any
   ``numbers.Integral`` but bool, stored as ``int``).
3. A missing value is never silent: ``value=None`` needs a ``reason`` (the old
   ``GtAssessment.reason``, #16).
4. Fail-closed ground truth: ``label_status="unverified"`` (labels needed but missing,
   stale or unverified) has ``value=None``, never a silent zero (#16; the port design,
   section 4.2). :meth:`Measure.missing` defaults to it.
5. ``cost_basis`` is one of :data:`COST_BASES` (``COST_BASIS_VALUES``, #22, plus
   ``none`` for a number with no cost in it); ``label_status`` is one of
   :data:`LABEL_STATUSES`.
6. A confidence interval belongs to a value: no interval without a value, bounds
   finite and ordered, level strictly between 0 and 1, and a named method (new).
7. Two measures are compared only when they measure the same thing on the same basis:
   the same ``method`` (``assert_comparable_metric_ids``, #17), the same ``subject``
   (agent, class and field), the same cost basis and the same provenance
   (``validate_comparison_provenance`` with ``PROVENANCE_KEYS``, #22: every key present
   on both sides and equal, except the keys named in ``vary``, which must be present
   but may differ, and ``draw_seed``, which may be absent on both), the same label
   status (new: both legs rest on the same ground truth), and both have a value (a delta
   with an unmeasured side is unmeasurable, as in ``delta_significance``).

Rules that depend on the metric (a rate lies in ``[0, 1]``, a count is an integer, an
interval lies in the unit's range and contains its value, a metric that needs labels
cannot claim ``label_status="none"``, a cost metric needs a cost basis) are checked by
:meth:`mailroom_dojo.measures.MetricTable.check`, which knows each metric's row.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "COST_BASES",
    "LABEL_STATUSES",
    "LABEL_STATUS_WORDS",
    "PROVENANCE_KEYS",
    "ConfidenceInterval",
    "IncomparableMeasuresError",
    "Measure",
    "MeasureError",
    "Provenance",
    "Subject",
    "assert_comparable",
    "combined_label_status",
    "comparability_problems",
]

#: How a cost figure was obtained. The first three are the old Dojo's
#: ``COST_BASIS_VALUES``: metered GPU time over the busy window, a bill including cold
#: starts, and tokens times a provider price table (an estimate, never comparable with
#: a metered figure). ``none``: the number has no cost in it.
COST_BASES: tuple[str, ...] = ("busy_window", "billed_incl_cold", "token_price_estimate", "none")

#: The status words a resolved label carries in ``mailroom_labels`` (``STATUS_WORDS``),
#: from least to most trusted. Repeated here because the Dojo does not import the labels
#: package; a test checks that the two lists agree.
LABEL_STATUS_WORDS: tuple[str, ...] = ("hub", "interim", "near_gold", "gold")

#: A measure's label status: one status word when every label behind it has it,
#: ``mixed`` when the labels have several, ``none`` when the metric uses no labels
#: (latency, cost, parse rate), and ``unverified`` when labels were needed but missing,
#: stale or unverified, so there is no value (fail closed).
LABEL_STATUSES: tuple[str, ...] = (*LABEL_STATUS_WORDS, "mixed", "none", "unverified")

#: The old ``scorecard_honesty.PROVENANCE_KEYS`` without ``metric_id`` (a measure's
#: ``method``): what a run must state for its numbers to be compared with another run's.
PROVENANCE_KEYS: tuple[str, ...] = (
    "dataset_revision",
    "split",
    "draw_seed",
    "prompt_id",
    "scorer_version",
    "serving_kind",
    "model_id",
)
#: Provenance keys two measures may both leave unset (a full-split run has no draw seed);
#: set on one side only is still a problem. The old ``_OPTIONAL_PAIRED_KEYS``.
_OPTIONAL_PAIRED_KEYS = frozenset({"draw_seed"})


class MeasureError(ValueError):
    """A ``Measure`` or ``ConfidenceInterval`` that breaks a provenance rule."""


class IncomparableMeasuresError(ValueError):
    """Two measures that must not be compared; ``problems`` says why."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__("measures are not comparable: " + "; ".join(problems))
        self.problems = problems


def _as_real(value: Any) -> float | None:
    """``value`` as a finite ``float`` when it is a real number that is not a bool, else ``None``.

    Any ``numbers.Real`` counts (numpy integers and floats, ``Fraction``); ``Decimal`` is not
    one. NaN, infinity and an integer too large for a float give ``None``.
    """
    if not isinstance(value, numbers.Real) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _is_count(value: Any) -> bool:
    """A non-negative integer (``numbers.Integral``, so numpy integers too) that is not a bool."""
    return isinstance(value, numbers.Integral) and not isinstance(value, bool) and value >= 0


def _optional_text(owner: str, name: str, value: Any) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise MeasureError(f"{owner}.{name} must be None or a non-empty string, got {value!r}")


@dataclass(frozen=True)
class Subject:
    """What a measure is about: an agent, a document class and a field, each optional.

    The empty subject (all ``None``) is the whole run. Two measures of the same metric
    about different fields, classes or agents are different numbers and never compared.
    """

    agent: str | None = None
    doc_class: str | None = None
    field: str | None = None

    def __post_init__(self) -> None:
        for name in ("agent", "doc_class", "field"):
            _optional_text("subject", name, getattr(self, name))

    def to_dict(self) -> dict[str, Any]:
        """A plain dict for JSON output."""
        return {"agent": self.agent, "doc_class": self.doc_class, "field": self.field}


@dataclass(frozen=True)
class Provenance:
    """Where a measure comes from: the old ``PROVENANCE_KEYS`` (``metric_id`` is ``method``).

    Every key is optional when the measure is built; comparing two measures needs every
    key on both sides (``draw_seed`` may be absent on both), see :func:`comparability_problems`.
    """

    dataset_revision: str | None = None
    split: str | None = None
    draw_seed: str | None = None
    prompt_id: str | None = None
    scorer_version: str | None = None
    serving_kind: str | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        for name in PROVENANCE_KEYS:
            _optional_text("provenance", name, getattr(self, name))

    def to_dict(self) -> dict[str, Any]:
        """A plain dict for JSON output, in :data:`PROVENANCE_KEYS` order."""
        return {name: getattr(self, name) for name in PROVENANCE_KEYS}


@dataclass(frozen=True)
class ConfidenceInterval:
    """An interval around a value: ``low`` to ``high`` at ``level`` (0.95 for 95%), by ``method``."""

    low: float
    high: float
    level: float
    method: str

    def __post_init__(self) -> None:
        for name in ("low", "high", "level"):
            number = _as_real(getattr(self, name))
            if number is None:
                raise MeasureError(f"ci.{name} must be a finite number, got {getattr(self, name)!r}")
            object.__setattr__(self, name, number)
        if self.low > self.high:
            raise MeasureError(f"ci.low {self.low!r} is above ci.high {self.high!r}")
        if not 0.0 < self.level < 1.0:
            raise MeasureError(f"ci.level must be strictly between 0 and 1, got {self.level!r}")
        if not isinstance(self.method, str) or not self.method.strip():
            raise MeasureError("ci.method must name how the interval was computed")

    def to_dict(self) -> dict[str, Any]:
        """A plain dict for JSON output."""
        return {"low": self.low, "high": self.high, "level": self.level, "method": self.method}


@dataclass(frozen=True)
class Measure:
    """One reported number and its provenance; see the module docstring for the rules.

    ``method`` is the metric id from the metric table (or a formula reference for a
    number outside it). ``reason`` says why ``value`` is ``None`` and is required then.
    ``subject`` says what the number is about (default: the whole run) and
    ``provenance`` which run produced it (needed for a comparison).
    """

    value: float | None
    n: int
    cost_basis: str
    label_status: str
    method: str
    ci: ConfidenceInterval | None = None
    reason: str | None = None
    subject: Subject = Subject()
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        if not _is_count(self.n):
            raise MeasureError(f"n must be a non-negative int, got {self.n!r}")
        object.__setattr__(self, "n", int(self.n))
        if self.value is not None:
            number = _as_real(self.value)
            if number is None:
                raise MeasureError(f"value must be a finite number or None, got {self.value!r}")
            object.__setattr__(self, "value", number)
        if self.cost_basis not in COST_BASES:
            raise MeasureError(f"cost_basis {self.cost_basis!r} is not one of {', '.join(COST_BASES)}")
        if self.label_status not in LABEL_STATUSES:
            raise MeasureError(
                f"label_status {self.label_status!r} is not one of {', '.join(LABEL_STATUSES)}"
            )
        if not isinstance(self.method, str) or not self.method.strip():
            raise MeasureError("method must name the metric id or formula behind the value")
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise MeasureError("reason must be a non-empty string when given")
        if self.ci is not None and not isinstance(self.ci, ConfidenceInterval):
            raise MeasureError(f"ci must be a ConfidenceInterval or None, got {type(self.ci).__name__}")
        if not isinstance(self.subject, Subject):
            raise MeasureError(f"subject must be a Subject, got {type(self.subject).__name__}")
        if self.provenance is not None and not isinstance(self.provenance, Provenance):
            raise MeasureError(
                f"provenance must be a Provenance or None, got {type(self.provenance).__name__}"
            )
        if self.n == 0 and self.value is not None:
            raise MeasureError(f"{self.method}: n == 0 has no value (got {self.value!r}); nothing was scored")
        if self.label_status == "unverified" and self.value is not None:
            raise MeasureError(f"{self.method}: unverified labels give no value (got {self.value!r})")
        if self.value is None and self.reason is None:
            raise MeasureError(f"{self.method}: a missing value needs a reason")
        if self.ci is not None and self.value is None:
            raise MeasureError(f"{self.method}: a confidence interval needs a value")

    @classmethod
    def missing(
        cls,
        *,
        method: str,
        reason: str,
        n: int = 0,
        label_status: str = "unverified",
        cost_basis: str = "none",
        subject: Subject | None = None,
        provenance: Provenance | None = None,
    ) -> Measure:
        """A measure with no value, saying why (for example ``reason="gt_missing"``).

        ``label_status`` defaults to ``unverified`` (fail closed); pass ``none`` for a
        metric that uses no labels.
        """
        return cls(
            value=None,
            n=n,
            cost_basis=cost_basis,
            label_status=label_status,
            method=method,
            reason=reason,
            subject=Subject() if subject is None else subject,
            provenance=provenance,
        )

    def to_dict(self) -> dict[str, Any]:
        """A plain dict for JSON output; all keys present (``ci``, ``reason``, ``provenance`` may be null)."""
        return {
            "value": self.value,
            "n": self.n,
            "ci": None if self.ci is None else self.ci.to_dict(),
            "cost_basis": self.cost_basis,
            "label_status": self.label_status,
            "method": self.method,
            "reason": self.reason,
            "subject": self.subject.to_dict(),
            "provenance": None if self.provenance is None else self.provenance.to_dict(),
        }


def combined_label_status(statuses: Iterable[str]) -> str:
    """The label status of a number built from parts with these statuses.

    One status when all parts share it; ``unverified`` when any part is unverified
    (fail closed); otherwise ``mixed``. An empty input or an unknown status raises.
    """
    seen = tuple(statuses)
    if not seen:
        raise MeasureError("combined_label_status needs at least one status")
    unknown = sorted({status for status in seen if status not in LABEL_STATUSES})
    if unknown:
        raise MeasureError(f"unknown label status {unknown[0]!r}")
    distinct = set(seen)
    if len(distinct) == 1:
        return seen[0]
    if "unverified" in distinct:
        return "unverified"
    return "mixed"


def _absent(value: str | None) -> bool:
    return value is None


def _vary_keys(vary: Iterable[str] | str) -> frozenset[str]:
    keys = frozenset((vary,) if isinstance(vary, str) else vary)  # a bare key, not its letters
    unknown = sorted(keys - set(PROVENANCE_KEYS))
    if unknown:
        raise MeasureError(f"vary names keys that are not provenance keys: {unknown}")
    return keys


def _provenance_problems(left: Measure, right: Measure, vary: frozenset[str]) -> list[str]:
    """The old ``validate_comparison_provenance`` rule, as problem strings."""
    sides = (("left", left.provenance), ("right", right.provenance))
    absent_sides = [side for side, provenance in sides if provenance is None]
    if absent_sides:
        return [f"{side}_provenance_missing" for side in absent_sides]
    assert left.provenance is not None and right.provenance is not None
    missing: list[str] = []
    mismatched: list[str] = []
    for key in PROVENANCE_KEYS:
        lv, rv = getattr(left.provenance, key), getattr(right.provenance, key)
        if key in _OPTIONAL_PAIRED_KEYS and _absent(lv) and _absent(rv):
            continue
        if _absent(lv) or _absent(rv):
            missing.append(key)
        elif key not in vary and lv != rv:
            mismatched.append(key)
    problems: list[str] = []
    if missing:
        problems.append(f"provenance_missing ({', '.join(missing)})")
    if mismatched:
        problems.append(f"provenance_mismatch ({', '.join(mismatched)})")
    return problems


def comparability_problems(
    left: Measure, right: Measure, *, vary: Iterable[str] | str = ()
) -> tuple[str, ...]:
    """Why ``left`` and ``right`` cannot be compared; empty when they can.

    ``vary`` names the provenance keys the comparison is about (for local against API
    serving, ``("serving_kind", "model_id")``): they must be set on both sides but may
    differ. A key outside :data:`PROVENANCE_KEYS` raises :class:`MeasureError`.
    """
    keys = _vary_keys(vary)
    problems: list[str] = []
    if left.method != right.method:
        problems.append(f"method_mismatch ({left.method!r} vs {right.method!r})")
    if left.subject != right.subject:
        problems.append(f"subject_mismatch ({left.subject.to_dict()} vs {right.subject.to_dict()})")
    if left.cost_basis != right.cost_basis:
        problems.append(f"cost_basis_mismatch ({left.cost_basis!r} vs {right.cost_basis!r})")
    if left.label_status != right.label_status:
        problems.append(f"label_status_mismatch ({left.label_status!r} vs {right.label_status!r})")
    problems.extend(_provenance_problems(left, right, keys))
    for side, measure in (("left", left), ("right", right)):
        if measure.value is None:
            problems.append(f"{side}_unmeasured ({measure.reason})")
    return tuple(problems)


def assert_comparable(left: Measure, right: Measure, *, vary: Iterable[str] | str = ()) -> None:
    """Raise :class:`IncomparableMeasuresError` unless the two measures may be compared."""
    problems = comparability_problems(left, right, vary=vary)
    if problems:
        raise IncomparableMeasuresError(problems)
