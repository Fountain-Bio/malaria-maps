"""Minimal .env loader and config access (no external dependency).

GeoNames' free web service authenticates by username only; the geonames.org password is
not used by the API and is not stored here.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ without overriding existing vars."""
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def geonames_username() -> str | None:
    load_env()
    return os.environ.get("GEONAMES_USERNAME")
