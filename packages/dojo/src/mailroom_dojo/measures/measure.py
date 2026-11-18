"""``Measure``: a number that carries its sample size, cost basis and label status.

Every Dojo layer returns ``Measure`` values instead of bare floats, so a report cannot
print a number without saying how many items it rests on, what its cost figures are
based on and how trustworthy the labels behind it are. A ``Measure`` validates itself
when it is built; an invalid one raises :class:`MeasureError` and never exists.

The rules, ported from the old Dojo's ``metrics/scorecard_honesty.py`` (issue numbers
are the old repository's):

1. A value is a finite real number or ``None``: never a bool, NaN or infinity (the old
   ``_is_real_score`` and the golden canonical form, which rejects non-finite floats).
2. ``n == 0`` has no value: nothing scored proves nothing (``aggregate_quality_honest``
   reports ``quality_mean=None`` and ``export_verdict="unproven"`` for an empty
   population, #16). ``n`` is a non-negative int.
3. A missing value is never silent: ``value=None`` needs a ``reason`` (the old
   ``GtAssessment.reason``, ``n_unscored_by_reason``, and "``unknown`` is never
   silent" in ``headline_scope``, #95).
4. Fail-closed ground truth: ``label_status="unverified"`` (labels needed but missing,
   stale or unverified) has ``value=None``, never a silent zero (#16; the port design,
   section 4.2).
5. ``cost_basis`` is one of :data:`COST_BASES` (``COST_BASIS_VALUES``, #22, plus
   ``none`` for a number with no cost in it); ``label_status`` is one of
   :data:`LABEL_STATUSES`.
6. A confidence interval belongs to a value: no interval without a value, bounds
   finite and ordered, level strictly between 0 and 1, and a named method.
7. Two measures are compared only when they measure the same thing on the same basis:
   the same ``method`` (``assert_comparable_metric_ids``, #17), the same cost basis
   (``validate_comparison_provenance``, #22), the same label status (the provenance
   rule that both legs rest on the same ground truth), and both have a value (a delta
   with an unmeasured side is unmeasurable, not "insignificant", as in
   ``delta_significance``).

Rules that depend on the metric (a rate lies in ``[0, 1]``, a metric that needs labels
cannot claim ``label_status="none"``, a cost metric needs a cost basis) are checked by
:meth:`mailroom_dojo.measures.MetricTable.check`, which knows each metric's row.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "COST_BASES",
    "LABEL_STATUSES",
    "LABEL_STATUS_WORDS",
    "ConfidenceInterval",
    "IncomparableMeasuresError",
    "Measure",
    "MeasureError",
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


class MeasureError(ValueError):
    """A ``Measure`` or ``ConfidenceInterval`` that breaks a provenance rule."""


class IncomparableMeasuresError(ValueError):
    """Two measures that must not be compared; ``problems`` says why."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__("measures are not comparable: " + "; ".join(problems))
        self.problems = problems


def _is_real(value: Any) -> bool:
    """A finite int or float that is not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_count(value: Any) -> bool:
    """A non-negative int that is not a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class ConfidenceInterval:
    """An interval around a value: ``low`` to ``high`` at ``level`` (0.95 for 95%), by ``method``."""

    low: float
    high: float
    level: float
    method: str

    def __post_init__(self) -> None:
        for name in ("low", "high", "level"):
            if not _is_real(getattr(self, name)):
                raise MeasureError(f"ci.{name} must be a finite number, got {getattr(self, name)!r}")
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
    """

    value: float | int | None
    n: int
    cost_basis: str
    label_status: str
    method: str
    ci: ConfidenceInterval | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not _is_count(self.n):
            raise MeasureError(f"n must be a non-negative int, got {self.n!r}")
        if self.value is not None and not _is_real(self.value):
            raise MeasureError(f"value must be a finite number or None, got {self.value!r}")
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
        cls, *, method: str, reason: str, n: int = 0, label_status: str = "none", cost_basis: str = "none"
    ) -> Measure:
        """A measure with no value, saying why (for example ``reason="gt_missing"``)."""
        return cls(
            value=None, n=n, cost_basis=cost_basis, label_status=label_status, method=method, reason=reason
        )

    def to_dict(self) -> dict[str, Any]:
        """A plain dict for JSON output; ``ci`` and ``reason`` are always present (maybe null)."""
        return {
            "value": self.value,
            "n": self.n,
            "ci": None if self.ci is None else self.ci.to_dict(),
            "cost_basis": self.cost_basis,
            "label_status": self.label_status,
            "method": self.method,
            "reason": self.reason,
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


def comparability_problems(left: Measure, right: Measure) -> tuple[str, ...]:
    """Why ``left`` and ``right`` cannot be compared; empty when they can."""
    problems: list[str] = []
    if left.method != right.method:
        problems.append(f"method_mismatch ({left.method!r} vs {right.method!r})")
    if left.cost_basis != right.cost_basis:
        problems.append(f"cost_basis_mismatch ({left.cost_basis!r} vs {right.cost_basis!r})")
    if left.label_status != right.label_status:
        problems.append(f"label_status_mismatch ({left.label_status!r} vs {right.label_status!r})")
    for side, measure in (("left", left), ("right", right)):
        if measure.value is None:
            problems.append(f"{side}_unmeasured ({measure.reason})")
    return tuple(problems)


def assert_comparable(left: Measure, right: Measure) -> None:
    """Raise :class:`IncomparableMeasuresError` unless the two measures may be compared."""
    problems = comparability_problems(left, right)
    if problems:
        raise IncomparableMeasuresError(problems)
