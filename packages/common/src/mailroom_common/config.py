"""Typed configuration: load YAML or JSON files, layer overlays, validate, hash.

The flow is: read each file (by extension) -> optional ``${VAR}`` interpolation ->
:func:`deep_merge` the layers in order -> validate against a :class:`ConfigModel` ->
store :func:`config_hash` of the result on the run. Every failure is a
:class:`~mailroom_common.errors.ConfigError` naming the source file(s).
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from mailroom_common.errors import ConfigError
from mailroom_common.hashing import canonical_json, hash_obj
from mailroom_common.jsonl import parse_json
from mailroom_common.yamlio import loads_yaml

__all__ = ["ConfigModel", "config_hash", "deep_merge", "load_config", "load_layered"]

ModelT = TypeVar("ModelT", bound=BaseModel)

_YAML_SUFFIXES = {".yaml", ".yml"}
_JSON_SUFFIXES = {".json"}
_REFERENCE = re.compile(r"\$\$|\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}|\$\{")


class ConfigModel(BaseModel):
    """Base for every configuration model: immutable, unknown keys refused, strict types.

    Files are validated in pydantic's strict JSON mode: ``1`` is not accepted for a
    string, ``"1"`` not for an int, ``true`` not for a number, but the JSON forms of
    tuples (lists), paths, enums and datetimes (strings) are. Constructing a model
    directly in Python is strict in Python mode (pass a tuple, a ``Path``, an enum).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``overlay`` laid over it; neither input is changed.

    Where both sides hold a mapping, the mappings merge recursively. Anything else from
    the overlay (scalars, lists, ``None``) replaces the base value whole: lists are not
    concatenated, and an explicit ``null`` sets the value to ``None`` rather than
    deleting the key (there is no deletion). The result shares no mutable objects with
    the inputs.

    Associative only when each key path holds the same kind (mapping or not) in every
    layer: if a middle layer replaces a mapping with a scalar and a later layer puts a
    mapping back, grouping the later two first loses the reset.
    """
    merged: dict[str, Any] = {key: copy.deepcopy(value) for key, value in base.items()}
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _dotted(location: Sequence[int | str]) -> str:
    return ".".join(str(part) for part in location) or "<root>"


def _interpolate_text(text: str, env: Mapping[str, str], source: str, where: str) -> str:
    def replace(match: re.Match[str]) -> str:
        whole = match.group(0)
        if whole == "$$":
            return "$"
        name = match.group("name")
        if name is None:
            raise ConfigError(
                "malformed ${...} reference (use ${NAME} or ${NAME:-default}; $$ for a literal $)",
                source=source,
                key=where,
            )
        default = match.group("default")
        value = env.get(name)
        if default is not None and not value:
            return default
        if value is None:
            raise ConfigError(f"undefined variable {name} with no default", source=source, key=where)
        return value

    return _REFERENCE.sub(replace, text)


def _interpolate(data: Any, env: Mapping[str, str], source: str, location: tuple[int | str, ...]) -> Any:
    if isinstance(data, str):
        return _interpolate_text(data, env, source, _dotted(location))
    if isinstance(data, dict):
        return {key: _interpolate(value, env, source, (*location, key)) for key, value in data.items()}
    if isinstance(data, list):
        return [_interpolate(value, env, source, (*location, index)) for index, value in enumerate(data)]
    return data


def _to_json_data(data: Any, source: str, location: tuple[int | str, ...]) -> Any:
    """YAML dates become ISO strings; NaN, infinity and YAML-only types (sets, binary) are refused."""
    if isinstance(data, dict):
        return {key: _to_json_data(value, source, (*location, key)) for key, value in data.items()}
    if isinstance(data, list):
        return [_to_json_data(value, source, (*location, index)) for index, value in enumerate(data)]
    if isinstance(data, (datetime, date)):
        return data.isoformat()
    if isinstance(data, float) and not math.isfinite(data):
        raise ConfigError("NaN and infinity are not allowed in config", source=source, key=_dotted(location))
    if data is None or isinstance(data, (str, int, float, bool)):
        return data
    raise ConfigError(
        f"value of type {type(data).__name__} cannot be represented in JSON",
        source=source,
        key=_dotted(location),
    )


def _read_layer(path: Path, env: Mapping[str, str] | None) -> dict[str, Any]:
    source = str(path)
    suffix = path.suffix.lower()
    if suffix not in _YAML_SUFFIXES | _JSON_SUFFIXES:
        raise ConfigError(
            f"unknown config file extension {path.suffix!r} (expected .yaml, .yml or .json)", source=source
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read config file: {exc}", source=source) from exc
    if suffix in _YAML_SUFFIXES:
        data = loads_yaml(text, source=source)
    else:
        try:
            data = parse_json(text)
        except ValueError as exc:
            raise ConfigError(
                f"invalid JSON: {exc}", source=source, line=getattr(exc, "lineno", None)
            ) from exc
    if data is None:
        raise ConfigError("config file is empty; expected a mapping", source=source)
    if not isinstance(data, dict):
        raise ConfigError(
            f"config file must hold a mapping at the top, got {type(data).__name__}", source=source
        )
    data = _to_json_data(data, source, ())
    if env is not None:
        data = _interpolate(data, env, source, ())
    return data  # type: ignore[no-any-return]


def _validate(data: dict[str, Any], model: type[ModelT], source: str | list[str]) -> ModelT:
    try:
        return model.model_validate_json(canonical_json(data), strict=True)
    except ValidationError as exc:
        problems = [f"{_dotted(error['loc'])}: {error['msg']}" for error in exc.errors()]
        raise ConfigError(
            f"config does not match {model.__name__}: " + "; ".join(problems),
            source=source,
            problems=len(problems),
        ) from exc


def load_config(path: str | Path, model: type[ModelT], *, env: Mapping[str, str] | None = None) -> ModelT:
    """Load one ``.yaml``/``.yml``/``.json`` file and validate it against ``model``.

    With ``env`` (for example ``os.environ``), every string value is interpolated:
    ``${NAME}`` is replaced by the variable, ``${NAME:-default}`` falls back to
    ``default`` when the variable is unset or empty, and ``$$`` is a literal ``$``; a
    ``$`` followed by anything else is left alone. An undefined variable without a
    default, or a malformed ``${``, raises ConfigError naming the variable and key, never
    any value. Without ``env`` strings are taken literally.

    Raises ConfigError for an unknown extension, an unreadable, empty or non-mapping
    file, invalid YAML/JSON (duplicate keys and NaN included), and validation failure
    (the message lists every problem as ``dotted.path: message``).
    """
    path = Path(path)
    return _validate(_read_layer(path, env), model, str(path))


def load_layered(
    paths: Sequence[str | Path], model: type[ModelT], *, env: Mapping[str, str] | None = None
) -> ModelT:
    """Merge the files in ``paths`` in order (later wins, see :func:`deep_merge`), then validate.

    Only the merged result is validated, so an overlay may hold just the keys it changes.
    Each file is read and interpolated as in :func:`load_config`; a validation error
    names every file. An empty ``paths`` is a ConfigError.
    """
    if not paths:
        raise ConfigError("load_layered needs at least one config file")
    merged: dict[str, Any] = {}
    for path in paths:
        merged = deep_merge(merged, _read_layer(Path(path), env))
    return _validate(merged, model, [str(Path(path)) for path in paths])


def config_hash(config: BaseModel | Mapping[str, Any]) -> str:
    """SHA-256 of a config's canonical JSON, for storing on a run.

    A model is dumped in JSON mode first (defaults included), so the hash does not
    depend on key order, on whether the file was YAML or JSON, or on whether a default
    was written out. A mapping is hashed as given.
    """
    data = config.model_dump(mode="json") if isinstance(config, BaseModel) else config
    return hash_obj(data)
