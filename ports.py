"""Normalize port display names. Does not decide loaded/sailed."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from config import PORTS_YAML

_PUNCT = re.compile(r"[\s,.\-/]+")


def normalize_key(value: str) -> str:
    return _PUNCT.sub(" ", value).strip().upper()


@lru_cache(maxsize=1)
def _catalog(path: str) -> dict[str, list[str]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    catalog: dict[str, list[str]] = {}
    for canonical, payload in raw.items():
        aliases = payload.get("aliases", []) if isinstance(payload, dict) else payload
        catalog[canonical.upper()] = [normalize_key(a) for a in aliases]
    return catalog


def display_port(location: str | None, yaml_path: Path | None = None) -> str | None:
    """Map a raw location to a canonical display name when possible."""
    if not location or not location.strip():
        return None
    path = str(yaml_path or PORTS_YAML)
    catalog = _catalog(path)
    key = normalize_key(location)
    if not key:
        return None
    for canonical, aliases in catalog.items():
        if key == canonical or key in aliases:
            return canonical
    for canonical, aliases in catalog.items():
        for alias in aliases + [canonical]:
            if alias and (alias in key or key in alias):
                return canonical
    return normalize_key(location)
