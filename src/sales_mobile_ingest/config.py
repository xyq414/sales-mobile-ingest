from __future__ import annotations

import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised when a local configuration cannot be safely used."""


def resolve_data_root(cli_value: str | None = None) -> Path:
    """Return a user-configurable data root without encoding a drive letter."""
    configured: str | None = cli_value or os.getenv("SALES_MOBILE_INGEST_DATA_ROOT")
    if not configured:
        local_config = PROJECT_ROOT / "config.local.json"
        if local_config.exists():
            try:
                data = json.loads(local_config.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"Invalid config.local.json: {exc.msg}") from exc
            configured = data.get("data_root")
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
        "failed": data_root / "failed" / "recordings",
        "state": data_root / "state",
        "logs": data_root / "logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths
