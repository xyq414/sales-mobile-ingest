from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "ingest-state.json"
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "imports": {}, "sources": {}, "devices": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read persistent ingest state: {exc}") from exc
        for key, empty in (("imports", {}), ("sources", {}), ("devices", {})):
            loaded.setdefault(key, empty)
        return loaded

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def source_is_current(self, key: str, size_bytes: int, modified_at: str | None) -> bool:
        source = self.data["sources"].get(key)
        if not source:
            return False
        return source.get("size_bytes") == size_bytes and source.get("modified_at") == modified_at

    def source_sha256(self, key: str) -> str | None:
        source = self.data["sources"].get(key)
        return source.get("sha256") if source else None

    def remember_source(self, key: str, size_bytes: int, modified_at: str | None, sha256: str) -> None:
        self.data["sources"][key] = {
            "size_bytes": size_bytes,
            "modified_at": modified_at,
            "sha256": sha256,
        }

    def remember_import(self, sha256: str, media_filename: str) -> None:
        self.data["imports"][sha256] = {"media_filename": media_filename}

    def installation_id(self) -> str:
        value = self.data.get("installation_id")
        if not isinstance(value, str) or not value:
            value = f"ins_{uuid.uuid4()}"
            self.data["installation_id"] = value
        return value

    def known_dirs(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for device_key, item in self.data["devices"].items():
            for relative_path in item.get("last_successful_relative_dirs", []):
                result.append({
                    "device_key": device_key,
                    "device_name": item.get("display_name", ""),
                    "relative_path": relative_path,
                })
        return result

    def remember_directory(self, device_key: str, display_name: str, relative_path: str) -> None:
        device = self.data["devices"].setdefault(device_key, {
            "display_name": display_name,
            "last_successful_relative_dirs": [],
        })
        device["display_name"] = display_name
        directories = device.setdefault("last_successful_relative_dirs", [])
        if relative_path in directories:
            directories.remove(relative_path)
        directories.insert(0, relative_path)
        del directories[5:]
