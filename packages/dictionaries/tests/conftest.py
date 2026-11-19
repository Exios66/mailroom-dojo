from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

BASE: dict[str, Any] = {
    "name": "colors",
    "version": 1,
    "description": "Test colors.",
    "ordered": False,
    "entries": [
        {"key": "red", "label": "Red", "description": "Red.", "aliases": ["crimson"]},
        {"key": "light-blue", "label": "Light blue", "description": "Light blue."},
        {"key": "green", "label": "Green", "description": "Green."},
    ],
}


@pytest.fixture
def colors() -> dict[str, Any]:
    """A fresh copy of a valid dictionary named ``colors`` to mutate in a test."""
    return copy.deepcopy(BASE)


WriteDict = Callable[..., Path]


@pytest.fixture
def write_dict(tmp_path: Path) -> WriteDict:
    """Write a dictionary (a mapping, or raw YAML text) to ``tmp_path/<name>.yaml``."""

    def write(data: dict[str, Any] | str, name: str | None = None) -> Path:
        if isinstance(data, str):
            stem = name or "colors"
            text = data
        else:
            stem = name or str(data.get("name", "colors"))
            text = yaml.safe_dump(data, sort_keys=False)
        path = tmp_path / f"{stem}.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return write
