from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    CURRENT_VERSION = 3

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "ingest-state.json"
        self.migration_backup_path: Path | None = None
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": self.CURRENT_VERSION,
                "imports": {},
                "sources": {},
                "devices": {},
                "calllog_exports": {"sources": {}, "artifacts": {}, "rows": {}, "enrichments": {}},
                "cloud_handoff": {"publications": {}, "salesperson_directories": {}},
                "recording_devices": {},
                "phone_calls": {},
                "call_recording_links": {},
                "calllog_snapshots": {},
                "call_fact_handoff": {"publications": {}},
                "device_registry": {"devices": {}, "alias_index": {}, "assignments": {}, "migration": {}},
                "desktop": {"calllog_observations": {}, "import_runs": []},
            }
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read persistent ingest state: {exc}") from exc
        version = loaded.get("version", 1)
        if not isinstance(version, int) or version < 1 or version > self.CURRENT_VERSION:
            raise RuntimeError(f"Unsupported ingest state version: {version}")
        if version < self.CURRENT_VERSION:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.migration_backup_path = self.path.parent / f"ingest-state.v{version}.{stamp}.backup.json"
            shutil.copy2(self.path, self.migration_backup_path)
        for key, empty in (("imports", {}), ("sources", {}), ("devices", {}), ("recording_devices", {}),
                           ("phone_calls", {}), ("call_recording_links", {}), ("calllog_snapshots", {})):
            loaded.setdefault(key, empty)
        calllog_exports = loaded.setdefault("calllog_exports", {})
        calllog_exports.setdefault("sources", {})
        calllog_exports.setdefault("artifacts", {})
        calllog_exports.setdefault("rows", {})
        calllog_exports.setdefault("enrichments", {})
        cloud_handoff = loaded.setdefault("cloud_handoff", {})
        cloud_handoff.setdefault("publications", {})
        cloud_handoff.setdefault("salesperson_directories", {})
        registry = loaded.setdefault("device_registry", {})
        for key in ("devices", "alias_index", "assignments", "migration"):
            registry.setdefault(key, {})
        loaded.setdefault("call_fact_handoff", {}).setdefault("publications", {})
        desktop = loaded.setdefault("desktop", {})
        desktop.setdefault("calllog_observations", {})
        desktop.setdefault("import_runs", [])
        loaded["version"] = self.CURRENT_VERSION
        if version < self.CURRENT_VERSION:
            loaded.setdefault("state_migrations", []).append({
                "from_version": version,
                "to_version": self.CURRENT_VERSION,
                "migrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "backup_filename": self.migration_backup_path.name if self.migration_backup_path else None,
            })
            self.data = loaded
            self.save()
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

    def remember_recording_device(self, recording_id: str, device_id: str) -> None:
        self.data["recording_devices"][recording_id] = device_id

    def recording_device(self, recording_id: str) -> str | None:
        value = self.data["recording_devices"].get(recording_id)
        return value if isinstance(value, str) else None

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

    def calllog_source_sha256(self, key: str, size_bytes: int, modified_at: str | None) -> str | None:
        source = self.data["calllog_exports"]["sources"].get(key)
        if source and source.get("size_bytes") == size_bytes and source.get("modified_at") == modified_at:
            value = source.get("sha256")
            return value if isinstance(value, str) else None
        return None

    def remember_calllog_source(self, key: str, size_bytes: int, modified_at: str | None, sha256: str) -> None:
        self.data["calllog_exports"]["sources"][key] = {
            "size_bytes": size_bytes,
            "modified_at": modified_at,
            "sha256": sha256,
        }

    def remember_calllog_artifact(self, sha256: str, size_bytes: int) -> None:
        self.data["calllog_exports"]["artifacts"][sha256] = {"size_bytes": size_bytes}

    def remember_calllog_row(self, row: dict[str, Any]) -> bool:
        canonical_id = row.get("canonical_call_id")
        device_key = row.get("device_key")
        if not isinstance(canonical_id, str) or not canonical_id or not isinstance(device_key, str) or not device_key:
            raise ValueError("canonical_call_id and device_key are required for persistent call-log state")
        rows = self.data["calllog_exports"]["rows"]
        record_key = f"{device_key}|{canonical_id}"
        if record_key in rows:
            return False
        rows[record_key] = row
        return True

    def calllog_rows_for_device(self, device_key: str) -> list[dict[str, Any]]:
        return [
            row for row in self.data["calllog_exports"]["rows"].values()
            if isinstance(row, dict) and row.get("device_key") == device_key
        ]

    def calllog_rows_for_device_id(self, device_id: str) -> list[dict[str, Any]]:
        return [
            row for row in self.data["calllog_exports"]["rows"].values()
            if isinstance(row, dict) and row.get("device_id") == device_id
        ]

    def remember_calllog_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.data["calllog_snapshots"][snapshot["snapshot_id"]] = snapshot

    def observe_calllog_snapshot(
        self, *, device_id: str, snapshot: dict[str, Any], observed_at: str, minimum_interval_seconds: int = 300
    ) -> str:
        """Keep privacy-minimal evidence that a newer public snapshot was observed later."""
        observations = self.data["desktop"]["calllog_observations"]
        current = observations.get(device_id)
        snapshot_id = str(snapshot["snapshot_id"])
        backup_timestamp = snapshot.get("backup_timestamp")
        if not isinstance(current, dict):
            observations[device_id] = {
                "baseline_snapshot_id": snapshot_id,
                "baseline_backup_timestamp": backup_timestamp,
                "baseline_observed_at": observed_at,
                "last_snapshot_id": snapshot_id,
                "last_backup_timestamp": backup_timestamp,
                "last_observed_at": observed_at,
                "observed_update_count": 0,
            }
            return "UNVERIFIED"
        current["last_observed_at"] = observed_at
        current["last_snapshot_id"] = snapshot_id
        current["last_backup_timestamp"] = backup_timestamp
        baseline_backup = current.get("baseline_backup_timestamp")
        baseline_observed = current.get("baseline_observed_at")
        qualifies = False
        if isinstance(backup_timestamp, str) and isinstance(baseline_backup, str) and isinstance(baseline_observed, str):
            try:
                backup = datetime.fromisoformat(backup_timestamp.replace("Z", "+00:00"))
                baseline = datetime.fromisoformat(baseline_backup.replace("Z", "+00:00"))
                observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                baseline_seen = datetime.fromisoformat(baseline_observed.replace("Z", "+00:00"))
                qualifies = backup > baseline and (observed - baseline_seen).total_seconds() >= minimum_interval_seconds
            except ValueError:
                qualifies = False
        if qualifies:
            if snapshot_id != current.get("verified_snapshot_id"):
                current["observed_update_count"] = int(current.get("observed_update_count", 0)) + 1
                current["verified_snapshot_id"] = snapshot_id
                current["verified_at"] = observed_at
            current["baseline_snapshot_id"] = snapshot_id
            current["baseline_backup_timestamp"] = backup_timestamp
            current["baseline_observed_at"] = observed_at
        return "OBSERVED_UPDATE" if int(current.get("observed_update_count", 0)) > 0 else "UNVERIFIED"

    def remember_desktop_import_run(self, summary: dict[str, Any]) -> None:
        runs = self.data["desktop"]["import_runs"]
        runs.append(summary)
        del runs[:-20]

    def latest_desktop_import_run(self) -> dict[str, Any] | None:
        runs = self.data["desktop"]["import_runs"]
        return dict(runs[-1]) if runs and isinstance(runs[-1], dict) else None

    def remember_phone_call(self, call: dict[str, Any]) -> None:
        self.data["phone_calls"][call["call_id"]] = {
            "device_id": call["device_id"],
            "source_row_id": call["source_row_id"],
            "path": f"ready/calls/{call['call_id']}.json",
        }

    def remember_call_recording_link(self, link: dict[str, Any]) -> None:
        self.data["call_recording_links"][link["recording_id"]] = {
            "link_id": link["link_id"],
            "call_id": link["call_id"],
            "status": link["status"],
            "path": f"ready/call-links/{link['link_id']}.json",
        }

    def calllog_enrichment_matches(self, event_id: str, canonical_call_id: str) -> bool:
        return self.data["calllog_exports"]["enrichments"].get(event_id) == canonical_call_id

    def remember_calllog_enrichment(self, event_id: str, canonical_call_id: str) -> None:
        self.data["calllog_exports"]["enrichments"][event_id] = canonical_call_id

    def cloud_publication(self, event_id: str) -> dict[str, Any] | None:
        value = self.data["cloud_handoff"]["publications"].get(event_id)
        return value if isinstance(value, dict) else None

    def remember_cloud_publication(
        self, event_id: str, *, relative_path: str | None, package_fingerprint: str | None, status: str
    ) -> None:
        self.data["cloud_handoff"]["publications"][event_id] = {
            "relative_path": relative_path,
            "package_fingerprint": package_fingerprint,
            "status": status,
        }

    def salesperson_directory(self, salesperson_id: str) -> str | None:
        value = self.data["cloud_handoff"]["salesperson_directories"].get(salesperson_id)
        return value if isinstance(value, str) and value else None

    def remember_salesperson_directory(self, salesperson_id: str, directory_name: str) -> None:
        self.data["cloud_handoff"]["salesperson_directories"][salesperson_id] = directory_name
