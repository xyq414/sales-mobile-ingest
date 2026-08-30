from __future__ import annotations

import json
import os
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .resources import SOURCE_PROJECT_ROOT, is_packaged

PROJECT_ROOT = SOURCE_PROJECT_ROOT
CONFIG_PATH_ENV = "SALES_MOBILE_INGEST_CONFIG_PATH"


class ConfigError(ValueError):
    """Raised when a local configuration cannot be safely used."""


@dataclass(frozen=True)
class SalespersonIdentity:
    """Explicit business identity; it is never inferred from the workstation."""

    salesperson_id: str
    salesperson_name: str


def desktop_config_path() -> Path:
    """Return a stable per-user location that survives moving or updating the EXE."""
    local_app_data = os.getenv("LOCALAPPDATA")
    base = Path(local_app_data).expanduser() if local_app_data else Path.home() / "AppData" / "Local"
    return base / "SalesMobileIngest" / "config.json"


def active_config_path() -> Path:
    explicit = os.getenv(CONFIG_PATH_ENV)
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser().resolve()
    if is_packaged():
        return desktop_config_path()
    return PROJECT_ROOT / "config.local.json"


def use_desktop_config() -> Path:
    """Select the desktop config for this process, including source-mode GUI runs."""
    path = desktop_config_path()
    os.environ.setdefault(CONFIG_PATH_ENV, str(path))
    return active_config_path()


def local_config() -> dict[str, Any]:
    """Read the ignored per-machine or per-user configuration."""
    config_path = active_config_path()
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid local configuration: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigError("local configuration must contain a JSON object")
    return data


def update_local_config(updates: dict[str, Any]) -> None:
    """Atomically update the ignored per-machine configuration."""
    config_path = active_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = local_config()
    data.update(updates)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=config_path.parent)
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


def backup_legacy_config_for_migration(destination_dir: Path) -> Path | None:
    """Make one private, rollback-oriented backup without changing the legacy config."""
    config_path = PROJECT_ROOT / "config.local.json"
    if not config_path.is_file():
        return None
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / "config.local.legacy-salesperson.backup.json"
    if not destination.exists():
        shutil.copy2(config_path, destination)
    return destination


def migrate_legacy_config_to_desktop() -> bool:
    """Copy legacy settings once; the repository-local file remains untouched."""
    destination = desktop_config_path()
    legacy = PROJECT_ROOT / "config.local.json"
    if destination.exists() or not legacy.is_file() or destination.resolve() == legacy.resolve():
        return False
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("legacy config.local.json cannot be migrated safely") from exc
    if not isinstance(data, dict):
        raise ConfigError("legacy config.local.json must contain a JSON object")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".config-migration-", suffix=".json", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


def resolve_data_root(cli_value: str | None = None) -> Path:
    """Return a user-configurable data root without encoding a drive letter."""
    configured: str | None = cli_value
    relative_base = PROJECT_ROOT
    if not configured:
        configured = local_config().get("data_root")
        relative_base = _config_relative_base()
    if not configured:
        configured = os.getenv("SALES_MOBILE_INGEST_DATA_ROOT")
        relative_base = PROJECT_ROOT
    if not configured:
        configured = str(Path.home() / "Documents" / "SalesMobileIngestData")
    root = Path(os.path.expandvars(configured)).expanduser()
    if not root.is_absolute():
        root = (relative_base / root).resolve()
    return root


def ensure_layout(data_root: Path) -> dict[str, Path]:
    paths = {
        "inbox": data_root / "inbox" / "recordings",
        "stage": data_root / "inbox" / "recordings" / ".stage",
        "ready": data_root / "ready" / "recordings",
        "events": data_root / "ready" / "events",
        "calls": data_root / "ready" / "calls",
        "call_links": data_root / "ready" / "call-links",
        "failed": data_root / "failed" / "recordings",
        "state": data_root / "state",
        "logs": data_root / "logs",
        "calllog_diagnostics": data_root / "diagnostics" / "calllog-backup",
        "calllog_stage": data_root / "diagnostics" / "calllog-backup" / ".stage",
        "calllog_failed": data_root / "diagnostics" / "calllog-backup" / "failed",
        "migration_evidence": data_root / "diagnostics" / "migrations",
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
    configured = cli_value or os.getenv("SALES_MOBILE_INGEST_CLOUD_HANDOFF_ROOT")
    relative_base = PROJECT_ROOT
    if not configured:
        configured = _configured_string("cloud_handoff_root")
        relative_base = _config_relative_base()
    if not configured:
        return None
    root = Path(os.path.expandvars(configured)).expanduser()
    if not root.is_absolute():
        root = (relative_base / root).resolve()
    return root


def _config_relative_base() -> Path:
    config_path = active_config_path().resolve()
    legacy = (PROJECT_ROOT / "config.local.json").resolve()
    return PROJECT_ROOT if config_path == legacy else config_path.parent


def resolve_calllog_freshness_seconds() -> int:
    value = local_config().get("calllog_freshness_seconds", 48 * 60 * 60)
    if not isinstance(value, int) or isinstance(value, bool) or value < 60:
        raise ConfigError("calllog_freshness_seconds must be an integer of at least 60")
    return value
