"""Screenshot, HTML, and log file helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config import HTML_DIR, LOG_DIR, SCREENSHOT_DIR


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def checked_at() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    for path in (SCREENSHOT_DIR, HTML_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def artifact_stem(container: str) -> str:
    return f"{container}_{stamp()}"


def screenshot_path(container: str) -> Path:
    ensure_dirs()
    return SCREENSHOT_DIR / f"{artifact_stem(container)}.png"


def html_path(container: str) -> Path:
    ensure_dirs()
    return HTML_DIR / f"{artifact_stem(container)}.html"


def log_path() -> Path:
    ensure_dirs()
    return LOG_DIR / f"run_{stamp()}.log"


def relative_to_root(path: Path | None) -> str | None:
    if path is None:
        return None
    from config import ROOT

    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)
