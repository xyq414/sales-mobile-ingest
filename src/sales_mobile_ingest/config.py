from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised when a local configuration cannot be safely used."""


@dataclass(frozen=True)
class SalespersonIdentity:
    """Explicit business identity; it is never inferred from the workstation."""

    salesperson_id: str
    salesperson_name: str


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


def update_local_config(updates: dict[str, Any]) -> None:
    """Atomically update the ignored per-machine configuration."""
    config_path = PROJECT_ROOT / "config.local.json"
    data = local_config()
    data.update(updates)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=PROJECT_ROOT)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, config_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


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


def _configured_string(key: str) -> str | None:
    value = local_config().get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string or null")
    return value.strip() or None


def resolve_salesperson_identity() -> SalespersonIdentity | None:
    """Return a complete explicit business identity, or no identity at all."""
    salesperson_id = _configured_string("salesperson_id")
    salesperson_name = _configured_string("salesperson_name")
    if salesperson_id is None and salesperson_name is None:
        return None
    if salesperson_id is None or salesperson_name is None:
        return None
    return SalespersonIdentity(salesperson_id=salesperson_id, salesperson_name=salesperson_name)


def resolve_salesperson_id() -> str | None:
    """Compatibility accessor for callers that only need the stable business ID."""
    identity = resolve_salesperson_identity()
    return identity.salesperson_id if identity else None


def resolve_cloud_handoff_root(cli_value: str | None = None) -> Path | None:
    """Return the explicitly configured cloud handoff root; discovery never guesses one."""
    configured = cli_value or os.getenv("SALES_MOBILE_INGEST_CLOUD_HANDOFF_ROOT") or _configured_string("cloud_handoff_root")
    if not configured:
        return None
    root = Path(os.path.expandvars(configured)).expanduser()
    if not root.is_absolute():
        root = (PROJECT_ROOT / root).resolve()
    return root
