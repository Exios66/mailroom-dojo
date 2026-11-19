"""Review policies: the cause catalog, the routing rules and the spot-check sample.

A policy lives at ``config/review/<name>.yaml``::

    name: <name>                      # must equal the file name without .yaml
    description: <text>               # optional
    default_queue: <token>            # optional, default "main"
    causes:                           # the cause catalog
      - token: <token or pattern>     # e.g. sorter_low_confidence, decision_point:*:*
        description: <text>
        priority: high | normal | low
        sla_hours: <number > 0>
    rules:                            # ordered; the first match wins
      - name: <token>
        when: {cause_in: [<token or pattern>...], class_in: [<ref>...], confidence_below: <0..1>}
        then: {priority: ..., sla_hours: ..., queue: <token>, required_role: <token>, prefill_ref: <ref>}
      - name: <token>                 # the final rule is the default: no `when`
        then: {}
    sample: {rate: <0..1>, seed: <text or int>}   # optional; the spot-check sample

Meaning
-------
- A cause is a token (``models.is_token``); a class and a prefill are references
  (``models.is_ref``: ASCII letters, digits and ``_ . : - /``, no whitespace or controls).
  A catalog *pattern* is a token whose segments may be ``*``, each matching exactly one
  segment, so ``decision_point:*:*`` matches ``decision_point:<slot>:<cause>``. An exact
  entry wins over a pattern; two patterns that can match the same token are refused, so a
  lookup has at most one answer.
- A rule matches when every condition it names holds: ``cause_in`` (any request cause
  matches any listed token or pattern), ``class_in`` (the class hint is listed; no hint
  never matches), ``confidence_below`` (a confidence hint strictly below it; no hint never
  matches). Rules are tried in file order and the first match wins. The final rule must
  have no ``when`` (the default), and no other rule may lack one (it would hide the rules
  after it).
- ``then`` overrides the catalog. Priority and SLA come from the rule when it sets them
  (it must set both or neither), else from the catalog: the most urgent priority and the
  shortest SLA among the request's causes. ``queue`` defaults to ``default_queue``.
- Outage tokens (``transient_exhausted``, ``provider_unavailable``) are refused anywhere
  in a policy, as a whole token or as a segment: a provider outage makes a document wait,
  never park (R1). ``Desk.enqueue`` refuses them in a request too.
- Sampling is deterministic: a document is in the sample when the SHA-256 of
  ``{"doc_id": <doc_id>, "seed": <seed>}`` (canonical JSON), read as a fraction of 2**64
  from its first 16 hex digits, is below ``rate``. A policy with ``rate > 0`` must list
  ``spot_check`` in its catalog (sampled documents enter with that cause).

The policy hash is ``hash_obj`` of the validated, normalised content (every value, in a
fixed shape), so comments, key order and number formatting (``24`` vs ``24.0``) do not
change it, and any change of meaning does. It is recorded on every enqueue.
``load_policy`` collects every problem before raising ``PolicyError``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from mailroom_desk._yaml import load_yaml_file, show
from mailroom_desk.errors import PolicyError
from mailroom_desk.models import PRIORITIES, is_ref, is_token, priority_rank
from mailroom_labels.profile import ClientProfile
from mailroom_record.hashing import hash_obj

__all__ = [
    "OUTAGE_TOKENS",
    "POLICY_SCHEMA",
    "SPOT_CHECK",
    "CauseSpec",
    "Policy",
    "Route",
    "Rule",
    "Sample",
    "Then",
    "When",
    "is_outage",
    "load_policy",
    "parse_policy",
    "pattern_matches",
    "unknown_classes",
]

POLICY_SCHEMA = "mailroom-desk/review-policy/v1"
OUTAGE_TOKENS: frozenset[str] = frozenset({"transient_exhausted", "provider_unavailable"})
SPOT_CHECK = "spot_check"

_TOP_KEYS = {"name", "description", "default_queue", "causes", "rules", "sample"}
_REQUIRED_TOP = {"name", "causes", "rules"}
_CAUSE_KEYS = {"token", "description", "priority", "sla_hours"}
_RULE_KEYS = {"name", "when", "then"}
_WHEN_KEYS = {"cause_in", "class_in", "confidence_below"}
_THEN_KEYS = {"priority", "sla_hours", "queue", "required_role", "prefill_ref"}
_SAMPLE_KEYS = {"rate", "seed"}
_PATTERN_RE = re.compile(r"(?:[a-z0-9_]+|\*)(?::(?:[a-z0-9_]+|\*))*")
_MAX_SLA = 24 * 366

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def is_outage(token: str) -> bool:
    """True when ``token`` (or any of its ``:`` segments) names a provider outage."""
    return any(seg in OUTAGE_TOKENS for seg in token.split(":"))


def _is_pattern(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 200 and _PATTERN_RE.fullmatch(value) is not None


def pattern_matches(pattern: str, token: str) -> bool:
    """True when ``token`` matches ``pattern`` (``*`` is one whole segment)."""
    p, t = pattern.split(":"), token.split(":")
    return len(p) == len(t) and all(a == "*" or a == b for a, b in zip(p, t, strict=True))


def _patterns_overlap(a: str, b: str) -> bool:
    """True when some token matches both ``a`` and ``b``."""
    x, y = a.split(":"), b.split(":")
    return len(x) == len(y) and all(p == q or p == "*" or q == "*" for p, q in zip(x, y, strict=True))


class CauseSpec(BaseModel):
    """One catalog entry: a token or pattern, its description, default priority and SLA."""

    model_config = _FROZEN

    token: str
    description: str
    priority: Literal["high", "normal", "low"]
    sla_hours: float

    @property
    def is_pattern(self) -> bool:
        return "*" in self.token.split(":")


class When(BaseModel):
    model_config = _FROZEN

    cause_in: tuple[str, ...] | None = None
    class_in: tuple[str, ...] | None = None
    confidence_below: float | None = None

    @property
    def is_default(self) -> bool:
        return self.cause_in is None and self.class_in is None and self.confidence_below is None


class Then(BaseModel):
    model_config = _FROZEN

    priority: Literal["high", "normal", "low"] | None = None
    sla_hours: float | None = None
    queue: str | None = None
    required_role: str | None = None
    prefill_ref: str | None = None


class Rule(BaseModel):
    model_config = _FROZEN

    name: str
    when: When
    then: Then

    def matches(self, causes: Sequence[str], class_hint: str | None, confidence_hint: float | None) -> bool:
        w = self.when
        if w.cause_in is not None and not any(pattern_matches(p, c) for p in w.cause_in for c in causes):
            return False
        if w.class_in is not None and (class_hint is None or class_hint not in w.class_in):
            return False
        return not (
            w.confidence_below is not None
            and (confidence_hint is None or not confidence_hint < w.confidence_below)
        )


class Sample(BaseModel):
    model_config = _FROZEN

    rate: float = 0.0
    seed: str = ""

    def fraction(self, doc_id: str) -> float:
        """Where ``doc_id`` falls in ``[0, 1)`` for this seed (see the module doc)."""
        return int(hash_obj({"doc_id": doc_id, "seed": self.seed})[:16], 16) / 2**64

    def selects(self, doc_id: str) -> bool:
        """True when ``doc_id`` is in the spot-check sample."""
        return self.rate > 0 and self.fraction(doc_id) < self.rate


class Route(BaseModel):
    """The outcome of the rules for one request."""

    model_config = _FROZEN

    rule: str
    priority: Literal["high", "normal", "low"]
    sla_hours: float
    queue: str
    required_role: str | None
    prefill_ref: str | None


class Policy(BaseModel):
    """A validated review policy. Build it with ``load_policy`` or ``parse_policy``."""

    model_config = _FROZEN

    name: str
    description: str | None
    default_queue: str
    causes: tuple[CauseSpec, ...]
    rules: tuple[Rule, ...]
    sample: Sample
    hash: str

    def cause_spec(self, token: str) -> CauseSpec | None:
        """The catalog entry for ``token``: the exact entry, else the one matching pattern."""
        for spec in self.causes:
            if not spec.is_pattern and spec.token == token:
                return spec
        for spec in self.causes:
            if spec.is_pattern and pattern_matches(spec.token, token):
                return spec
        return None

    def route(
        self, causes: Sequence[str], class_hint: str | None = None, confidence_hint: float | None = None
    ) -> Route:
        """Apply the rules to known causes (every cause must be in the catalog)."""
        specs = []
        for c in causes:
            spec = self.cause_spec(c)
            if spec is None:
                raise KeyError(c)
            specs.append(spec)
        if not specs:
            raise ValueError("a route needs at least one cause")
        for rule in self.rules:
            if rule.matches(causes, class_hint, confidence_hint):
                break
        else:  # unreachable for a validated policy: the final rule is a default
            raise RuntimeError(f"policy {show(self.name)} has no default rule")
        t = rule.then
        if t.priority is not None and t.sla_hours is not None:
            priority, sla = t.priority, t.sla_hours
        else:
            priority = min((s.priority for s in specs), key=priority_rank)
            sla = min(s.sla_hours for s in specs)
        return Route(
            rule=rule.name,
            priority=priority,
            sla_hours=sla,
            queue=t.queue if t.queue is not None else self.default_queue,
            required_role=t.required_role,
            prefill_ref=t.prefill_ref,
        )


# --- validation -------------------------------------------------------------------


def _is_number(value: object) -> bool:
    """A finite int or float; an int too large for a float is not a number here."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_text(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _keys(raw: Mapping[str, Any], allowed: set[str], where: str, problems: list[str]) -> None:
    for key in sorted(set(raw) - allowed, key=str):
        problems.append(f"{where} has unknown key {show(key)}")


def _sla(value: object, where: str, problems: list[str]) -> float | None:
    if not _is_number(value) or not 0 < value <= _MAX_SLA:  # type: ignore[operator]
        problems.append(f"{where} must be a number of hours > 0 and <= {_MAX_SLA}, got {show(value)}")
        return None
    return float(value)  # type: ignore[arg-type]


def _priority(value: object, where: str, problems: list[str]) -> str | None:
    if value not in PRIORITIES:
        problems.append(f"{where} must be one of {', '.join(PRIORITIES)}, got {show(value)}")
        return None
    return value  # type: ignore[return-value]


def _check_causes(raw: object, problems: list[str]) -> list[CauseSpec]:
    if not isinstance(raw, list) or not raw:
        problems.append("causes must be a non-empty list")
        return []
    out: list[CauseSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"causes[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping")
            continue
        bad = len(problems)
        _keys(entry, _CAUSE_KEYS, where, problems)
        for key in sorted(_CAUSE_KEYS - set(entry)):
            problems.append(f"{where} is missing {show(key)}")
        token = entry.get("token")
        if not _is_pattern(token):
            problems.append(
                f"{where}.token is not a token or pattern "
                f"([a-z0-9_] or '*' segments joined by ':'): {show(token)}"
            )
        elif is_outage(token):
            problems.append(
                f"{where}.token {show(token)} names a provider outage; outages wait, they are never reviewed"
            )
        elif token in seen:
            problems.append(f"{where}.token {show(token)} is listed twice")
        if "description" in entry and not _is_text(entry["description"]):
            problems.append(f"{where}.description must be non-empty text")
        priority = (
            _priority(entry.get("priority"), f"{where}.priority", problems) if "priority" in entry else None
        )
        sla = _sla(entry.get("sla_hours"), f"{where}.sla_hours", problems) if "sla_hours" in entry else None
        if len(problems) == bad:
            seen.add(token)
            out.append(
                CauseSpec(token=token, description=entry["description"], priority=priority, sla_hours=sla)
            )
    patterns = [s.token for s in out if s.is_pattern]
    for i, a in enumerate(patterns):
        for b in patterns[i + 1 :]:
            if _patterns_overlap(a, b):
                problems.append(f"cause patterns {show(a)} and {show(b)} can match the same token")
    return out


def _token_list(raw: object, where: str, problems: list[str], *, patterns: bool) -> tuple[str, ...] | None:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where} must be a non-empty list")
        return None
    out: list[str] = []
    for item in raw:
        ok = _is_pattern(item) if patterns else is_ref(item)
        if not ok:
            problems.append(
                f"{where} holds {show(item)}, not a {'token or pattern' if patterns else 'reference'}"
            )
        elif item in out:
            problems.append(f"{where} lists {show(item)} twice")
        else:
            out.append(item)
    return tuple(out) if len(out) == len(raw) else None


def _check_when(raw: object, where: str, catalog: Sequence[CauseSpec], problems: list[str]) -> When | None:
    if raw is None:
        return When()
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a mapping")
        return None
    _keys(raw, _WHEN_KEYS, where, problems)
    bad = len(problems)
    cause_in = class_in = confidence = None
    if "cause_in" in raw:
        cause_in = _token_list(raw["cause_in"], f"{where}.cause_in", problems, patterns=True)
        for token in cause_in or ():
            if is_outage(token):
                problems.append(
                    f"{where}.cause_in names {show(token)}, a provider outage; outages are never reviewed"
                )
            elif catalog and not any(_patterns_overlap(token, spec.token) for spec in catalog):
                problems.append(
                    f"{where}.cause_in names {show(token)}, which matches no cause in the catalog"
                )
    if "class_in" in raw:
        class_in = _token_list(raw["class_in"], f"{where}.class_in", problems, patterns=False)
    if "confidence_below" in raw:
        value = raw["confidence_below"]
        if not _is_number(value) or not 0 < value <= 1:
            problems.append(f"{where}.confidence_below must be a number in (0, 1], got {show(value)}")
        else:
            confidence = float(value)
    if len(problems) != bad:
        return None
    return When(cause_in=cause_in, class_in=class_in, confidence_below=confidence)


def _check_then(raw: object, where: str, problems: list[str]) -> Then | None:
    if raw is None:
        return Then()
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a mapping")
        return None
    _keys(raw, _THEN_KEYS, where, problems)
    bad = len(problems)
    priority = _priority(raw["priority"], f"{where}.priority", problems) if "priority" in raw else None
    sla = _sla(raw["sla_hours"], f"{where}.sla_hours", problems) if "sla_hours" in raw else None
    if ("priority" in raw) != ("sla_hours" in raw):
        problems.append(
            f"{where} must set priority and sla_hours together, or neither (the catalog then decides)"
        )
    for key in ("queue", "required_role"):
        if key in raw and not is_token(raw[key]):
            problems.append(f"{where}.{key} is not a token: {show(raw[key])}")
    if "prefill_ref" in raw and not is_ref(raw["prefill_ref"]):
        problems.append(f"{where}.prefill_ref must be a reference: {show(raw['prefill_ref'])}")
    if len(problems) != bad:
        return None
    return Then(
        priority=priority,
        sla_hours=sla,
        queue=raw.get("queue"),
        required_role=raw.get("required_role"),
        prefill_ref=raw.get("prefill_ref"),
    )


def _check_rules(raw: object, catalog: Sequence[CauseSpec], problems: list[str]) -> list[Rule]:
    if not isinstance(raw, list) or not raw:
        problems.append("rules must be a non-empty list ending in a default rule")
        return []
    out: list[Rule] = []
    names: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"rules[{i}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping")
            continue
        _keys(entry, _RULE_KEYS, where, problems)
        name = entry.get("name")
        if not is_token(name):
            problems.append(f"{where}.name is not a token: {show(name)}")
        elif name in names:
            problems.append(f"{where}.name {show(name)} is used twice")
        else:
            names.add(name)
            where = f"rules[{i}] ({name})"
        when = _check_when(entry.get("when"), f"{where}.when", catalog, problems)
        then = _check_then(entry.get("then"), f"{where}.then", problems)
        last = i == len(raw) - 1
        if when is not None:
            if last and not when.is_default:
                problems.append(f"{where} is the final rule and must be the default: no `when`")
            elif not last and when.is_default:
                problems.append(
                    f"{where} has no conditions but is not the final rule; it would hide every rule after it"
                )
        if when is not None and then is not None and is_token(name):
            out.append(Rule(name=name, when=when, then=then))
    return out


def _check_sample(raw: object, catalog: Sequence[CauseSpec], problems: list[str]) -> Sample:
    if raw is None:
        return Sample()
    if not isinstance(raw, dict):
        problems.append("sample must be a mapping {rate, seed}")
        return Sample()
    _keys(raw, _SAMPLE_KEYS, "sample", problems)
    rate = raw.get("rate", 0.0)
    if not _is_number(rate) or not 0 <= rate <= 1:
        problems.append(f"sample.rate must be a number in [0, 1], got {show(rate)}")
        rate = 0.0
    seed = raw.get("seed", "")
    if isinstance(seed, int) and not isinstance(seed, bool):
        try:
            seed = str(seed)
        except ValueError:  # more digits than Python converts to text
            problems.append("sample.seed is an integer too long to use as text")
            seed = ""
    if not isinstance(seed, str):
        problems.append(f"sample.seed must be text or an integer, got {show(seed)}")
        seed = ""
    if rate > 0:
        if seed == "":
            problems.append("sample.seed must be set when sample.rate > 0")
        if catalog and not any(pattern_matches(s.token, SPOT_CHECK) for s in catalog):
            problems.append(f"sample.rate > 0 needs a {show(SPOT_CHECK)} cause in the catalog")
    return Sample(rate=float(rate), seed=seed)


def _content(
    name: str,
    description: str | None,
    default_queue: str,
    causes: Iterable[CauseSpec],
    rules: Iterable[Rule],
    sample: Sample,
) -> dict[str, Any]:
    """The normalised policy content the hash is taken over."""
    return {
        "schema": POLICY_SCHEMA,
        "name": name,
        "description": description,
        "default_queue": default_queue,
        "causes": [c.model_dump(mode="json") for c in causes],
        "rules": [r.model_dump(mode="json") for r in rules],
        "sample": sample.model_dump(mode="json"),
    }


def parse_policy(raw: object, path: Path | None = None) -> Policy:
    """Validate a policy already parsed from YAML; raise ``PolicyError`` listing every problem.

    Input too deep or too large to validate (a value nested thousands of levels deep, an
    integer too long to show) is a ``PolicyError`` too, never a ``RecursionError`` or a
    ``ValueError``.
    """
    try:
        return _parse_policy(raw, path)
    except RecursionError:
        raise PolicyError(path, ["the policy is nested too deeply to validate"]) from None
    except (ValueError, OverflowError) as exc:  # a value too large to convert or to show
        raise PolicyError(
            path, [f"a value cannot be validated ({type(exc).__name__}); is it too large?"]
        ) from None


def _parse_policy(raw: object, path: Path | None) -> Policy:
    if not isinstance(raw, dict):
        raise PolicyError(path, ["a policy must be a mapping"])
    problems: list[str] = []
    _keys(raw, _TOP_KEYS, "the policy", problems)
    for key in sorted(_REQUIRED_TOP - set(raw)):
        problems.append(f"missing key {show(key)}")
    name = raw.get("name")
    if "name" in raw and not is_token(name):
        problems.append(f"name is not a token: {show(name)}")
    elif path is not None and "name" in raw and name != path.stem:
        problems.append(f"name {show(name)} differs from the file name {show(path.stem)}")
    description = raw.get("description")
    if description is not None and not _is_text(description):
        problems.append("description must be non-empty text")
    default_queue = raw.get("default_queue", "main")
    if not is_token(default_queue):
        problems.append(f"default_queue is not a token: {show(default_queue)}")
    causes = _check_causes(raw.get("causes"), problems) if "causes" in raw else []
    rules = _check_rules(raw.get("rules"), causes, problems) if "rules" in raw else []
    sample = _check_sample(raw.get("sample"), causes, problems)
    if problems:
        raise PolicyError(path, problems)
    content = _content(name, description, default_queue, causes, rules, sample)
    return Policy(
        name=name,
        description=description,
        default_queue=default_queue,
        causes=tuple(causes),
        rules=tuple(rules),
        sample=sample,
        hash=hash_obj(content),
    )


def load_policy(path: Path | str) -> Policy:
    """Read and validate ``config/review/<name>.yaml``; raise ``PolicyError`` on any problem."""
    path = Path(path)
    raw, problems = load_yaml_file(path)
    if problems:
        raise PolicyError(path, problems)
    return parse_policy(raw, path)


def unknown_classes(policy: Policy, profile: ClientProfile) -> list[str]:
    """Classes a rule's ``class_in`` names that the client profile does not define (sorted).

    A rule naming a class the profile lacks can never match a correctly classified
    document; ``rules check`` (Task D4) reports these.
    """
    named = {c for rule in policy.rules for c in (rule.when.class_in or ())}
    return sorted(named - set(profile.classes))
