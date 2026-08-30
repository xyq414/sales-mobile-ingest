from __future__ import annotations

import sys
from pathlib import Path


SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def is_packaged() -> bool:
    """Return whether the process is running from a frozen desktop bundle."""
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def runtime_root() -> Path:
    """Resolve bundled resources without requiring a repository checkout."""
    if is_packaged():
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return SOURCE_PROJECT_ROOT


def resource_path(*parts: str) -> Path:
    return runtime_root().joinpath(*parts)
