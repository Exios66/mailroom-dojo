"""The label surface the Dojo scores against, as structural Protocols.

The labels package (``mailroom_labels``) owns label snapshots: per document, the
class, subclass, content hash, whether the document text matched the dataset hash,
and per field a label with its status (``hub``, ``interim``, ``near_gold`` or
``gold``), source and reviewer. The Dojo never imports that package: these
Protocols name only the attributes the Dojo reads, so any object with them works.

:func:`expected_values` turns one labelled document into the expected-value mapping
:func:`~mailroom_dojo.field_scoring.score_extraction` takes. It keeps only the
document's scorable fields and refuses an unverified document unless the caller
asks for it, in which case the result is flagged; an unverified document is never
scored silently.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from ..profile import FrozenMapping, ScoringProfile
from ..values import is_empty_value

__all__ = [
    "LABEL_STATUSES",
    "ExpectedDocument",
    "ExpectedValues",
    "FieldLabel",
    "LabelSource",
    "PresenceExpectation",
    "UnverifiedDocumentError",
    "expected_values",
]

#: Label statuses in precedence order, lowest first.
LABEL_STATUSES = ("hub", "interim", "near_gold", "gold")


class PresenceExpectation(TypedDict):
    """Whether a category (a clause family) is expected in a document, the span that answers it, and the predicted field searched."""

    expected: bool
    answer: str
    field: str


@runtime_checkable
class FieldLabel(Protocol):
    """One field's label: its value, status (one of :data:`LABEL_STATUSES`), source and reviewer (None when unreviewed)."""

    @property
    def value(self) -> Any:
        """The labelled value."""

    @property
    def status(self) -> str:
        """``hub``, ``interim``, ``near_gold`` or ``gold``."""

    @property
    def source(self) -> str:
        """Where the label came from."""

    @property
    def reviewer(self) -> str | None:
        """The reviewer's initials for a gold label, else None."""


@runtime_checkable
class ExpectedDocument(Protocol):
    """One labelled document as the labels package publishes it (its ``DocumentLabels``).

    ``verified`` is False when the document's content hash did not match the
    dataset; ``unverified_reasons`` then says why. ``scorable_fields`` names the
    fields whose labels may be scored.
    """

    @property
    def document_id(self) -> str:
        """The document's id in the dataset."""

    @property
    def doc_class(self) -> str:
        """The document's class key."""

    @property
    def subclass(self) -> str | None:
        """The document's subclass, if any."""

    @property
    def content_sha256(self) -> str:
        """The sha256 of the document text the labels were made on."""

    @property
    def verified(self) -> bool:
        """True when the content hash matched the dataset."""

    @property
    def unverified_reasons(self) -> Collection[str]:
        """Why the document is unverified (empty when verified)."""

    @property
    def fields(self) -> Mapping[str, FieldLabel]:
        """Field name -> label."""

    @property
    def scorable_fields(self) -> Collection[str]:
        """The field names whose labels may be scored."""


@runtime_checkable
class LabelSource(Protocol):
    """A label snapshot: its id, its manifest hash and its labelled documents."""

    @property
    def snapshot_id(self) -> str:
        """The snapshot's id, recorded with every run scored against it."""

    @property
    def manifest_hash(self) -> str:
        """The hash of the snapshot's manifest."""

    def documents(self) -> Iterable[ExpectedDocument]:
        """Every labelled document in the snapshot."""


class UnverifiedDocumentError(ValueError):
    """A document whose content hash did not match the dataset was about to be scored without the caller asking for it."""

    def __init__(self, document_id: str, reasons: Collection[str]) -> None:
        """Name the document and why it is unverified."""
        listed = ", ".join(str(reason) for reason in reasons) or "no reason given"
        super().__init__(f"document {document_id!r} is unverified ({listed}); pass unverified='flag' to score it flagged")
        self.document_id = document_id
        self.reasons = tuple(str(reason) for reason in reasons)


@dataclass(frozen=True)
class ExpectedValues:
    """The expected values of one document, ready for ``score_extraction``.

    ``fields`` holds the scorable fields' label values (and any row-variant marker
    derived from the subclass, named in ``derived_fields``); ``field_status`` holds
    each field's label status. ``unscored_fields`` names labelled fields left out
    because they are not scorable. ``verified`` is False only when the caller asked
    to score an unverified document.
    """

    document_id: str
    class_key: str
    subclass: str | None
    verified: bool
    fields: FrozenMapping[str, Any]
    field_status: FrozenMapping[str, str]
    unscored_fields: tuple[str, ...] = ()
    derived_fields: tuple[str, ...] = ()
    unverified_reasons: tuple[str, ...] = field(default=())


def expected_values(
    document: ExpectedDocument,
    profile: ScoringProfile,
    *,
    unverified: Literal["refuse", "flag"] = "refuse",
) -> ExpectedValues:
    """Return the expected values of one labelled document for scoring.

    Only fields in ``scorable_fields`` that carry a label are kept, with their values
    as labelled. A field status outside :data:`LABEL_STATUSES` raises ``ValueError``
    naming the document and field; a class the profile does not know raises
    ``KeyError``. For each row variant of the class whose marker is derived from the
    subclass, an empty marker field is filled with the subclass token when it is a
    marker value. An unverified document raises :class:`UnverifiedDocumentError`
    unless ``unverified="flag"``, which returns it with ``verified=False``.
    """
    if unverified not in ("refuse", "flag"):
        raise ValueError(f"unverified must be 'refuse' or 'flag', not {unverified!r}")
    reasons = tuple(str(reason) for reason in (getattr(document, "unverified_reasons", ()) or ()))
    if not document.verified and unverified == "refuse":
        raise UnverifiedDocumentError(document.document_id, reasons)
    spec = profile.require_class(document.doc_class)
    scorable = set(document.scorable_fields)
    values: dict[str, Any] = {}
    statuses: dict[str, str] = {}
    unscored: list[str] = []
    for name, label in document.fields.items():
        if label.status not in LABEL_STATUSES:
            raise ValueError(
                f"document {document.document_id!r} field {name!r}: label status {label.status!r} "
                f"is not one of {', '.join(LABEL_STATUSES)}"
            )
        if name not in scorable:
            unscored.append(name)
            continue
        values[name] = label.value
        statuses[name] = label.status
    derived: list[str] = []
    for variant in spec.variants:
        marker = variant.marker
        if marker.derive_from != "subclass" or not is_empty_value(values.get(marker.field)):
            continue
        token = str(document.subclass or "").strip().lower()
        if token in marker.values:
            values[marker.field] = token
            derived.append(marker.field)
    return ExpectedValues(
        document_id=document.document_id,
        class_key=spec.key,
        subclass=document.subclass,
        verified=bool(document.verified),
        fields=FrozenMapping(values),
        field_status=FrozenMapping(statuses),
        unscored_fields=tuple(sorted(unscored)),
        derived_fields=tuple(derived),
        unverified_reasons=reasons if not document.verified else (),
    )
