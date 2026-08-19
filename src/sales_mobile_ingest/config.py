from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised when a local configuration cannot be safely used."""


def local_config() -> dict[str, Any]:
    """Read the ignored per-machine configuration without exposing it to Git."""
    config_path = PROJECT_ROOT / "config.local.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid config.local.json: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config.local.json must contain a JSON object")
    return data


def resolve_data_root(cli_value: str | None = None) -> Path:
    """Return a user-configurable data root without encoding a drive letter."""
    configured: str | None = cli_value or os.getenv("SALES_MOBILE_INGEST_DATA_ROOT")
    if not configured:
        configured = local_config().get("data_root")
    if not configured:
        configured = str(Path.home() / "Documents" / "SalesMobileIngestData")
    root = Path(os.path.expandvars(configured)).expanduser()
    if not root.is_absolute():
        root = (PROJECT_ROOT / root).resolve()
    return root


def ensure_layout(data_root: Path) -> dict[str, Path]:
    paths = {
        "inbox": data_root / "inbox" / "recordings",
        "stage": data_root / "inbox" / "recordings" / ".stage",
        "ready": data_root / "ready" / "recordings",
        "events": data_root / "ready" / "events",
        "failed": data_root / "failed" / "recordings",
        "state": data_root / "state",
        "logs": data_root / "logs",
        "calllog_diagnostics": data_root / "diagnostics" / "calllog-backup",
        "calllog_stage": data_root / "diagnostics" / "calllog-backup" / ".stage",
        "calllog_failed": data_root / "diagnostics" / "calllog-backup" / "failed",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def resolve_salesperson_id() -> str | None:
    """Return an optional user-supplied identity; never infer it from Windows/Git."""
    value = local_config().get("salesperson_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("salesperson_id must be a string or null")
    return value.strip() or None
