from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters import classify_candidate, device_identity
from .bridge import MtpBridge
from .config import ensure_layout
from .contract import build_metadata, iso_now, sha256_file, validate_recording
from .state import StateStore


@dataclass
class IngestSummary:
    devices: int = 0
    candidates_scanned: int = 0
    candidates_accepted: int = 0
    new_imports: int = 0
    duplicates: int = 0
    failures: int = 0
    source_attempts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "devices": self.devices,
            "candidates_scanned": self.candidates_scanned,
            "candidates_accepted": self.candidates_accepted,
            "new_imports": self.new_imports,
            "duplicates": self.duplicates,
            "failures": self.failures,
            "source_attempts": self.source_attempts,
        }


class Ingestor:
    def __init__(
        self,
        data_root: Path,
        *,
        bridge: MtpBridge | None = None,
        sidecar_writer: Callable[[Path, dict[str, Any]], None] | None = None,
    ) -> None:
        self.data_root = data_root
        self.paths = ensure_layout(data_root)
        self.bridge = bridge or MtpBridge()
        self.state = StateStore(self.paths["state"])
        self.sidecar_writer = sidecar_writer or self._write_sidecar_atomically

    def probe(self) -> dict[str, Any]:
        raw = self.bridge.probe(self.state.known_dirs())
        result: list[dict[str, Any]] = []
        for device in raw.get("devices", []):
            vendor, model = device_identity(device.get("display_name"))
            candidate_rows: list[dict[str, Any]] = []
            for candidate in device.get("candidates", []):
                decision = classify_candidate(
                    device_name=device.get("display_name"),
                    relative_path=candidate["relative_path"],
                    files=candidate.get("files", []),
                )
                candidate_rows.append({
                    "relative_path": candidate["relative_path"],
                    "audio_files": len(candidate.get("files", [])),
                    "accepted": decision.accepted,
                    "adapter": decision.adapter,
                    "score": decision.score,
                    "evidence": decision.evidence,
                    "from_cached_directory": bool(candidate.get("from_cached_directory")),
                })
            result.append({
                "display_name": device.get("display_name"),
                "vendor": vendor,
                "model": model,
                "storage_roots": device.get("storage_roots", []),
                "candidates": candidate_rows,
            })
        return {"devices": result, "bridge_observation": raw.get("observation", "ok")}

    def ingest_once(self, limit: int | None = None) -> IngestSummary:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        summary = IngestSummary()
        stop_requested = False
        raw = self.bridge.probe(self.state.known_dirs())
        devices = raw.get("devices", [])
        summary.devices = len(devices)
        for device in devices:
            vendor, model = device_identity(device.get("display_name"))
            accepted_any = False
            for candidate in device.get("candidates", []):
                if stop_requested:
                    break
                summary.candidates_scanned += 1
                decision = classify_candidate(
                    device_name=device.get("display_name"),
                    relative_path=candidate["relative_path"],
                    files=candidate.get("files", []),
                )
                if not decision.accepted:
                    continue
                accepted_any = True
                summary.candidates_accepted += 1
                candidate_succeeded = False
                for item in candidate.get("files", []):
                    if limit is not None and summary.source_attempts >= limit:
                        stop_requested = True
                        break
                    source = {
                        **item,
                        "device_key": device["device_key"],
                        "device_name": device.get("display_name"),
                        "device_vendor": vendor,
                        "device_model": model,
                        "adapter": decision.adapter,
                    }
                    summary.source_attempts += 1
                    outcome = self._ingest_source(source)
                    if outcome == "imported":
                        summary.new_imports += 1
                        candidate_succeeded = True
                    elif outcome == "duplicate":
                        summary.duplicates += 1
                        candidate_succeeded = True
                    else:
                        summary.failures += 1
                if candidate_succeeded:
                    self.state.remember_directory(
                        str(device["device_key"]), str(device.get("display_name", "")), candidate["relative_path"]
                    )
            if not stop_requested and not accepted_any and device.get("search_depth", 3) < 5:
                # A directory-name-only expansion remains bounded and only happens after insufficient evidence.
                expanded = self.bridge.probe(self.state.known_dirs(), search_depth=5)
                expanded_device = next((row for row in expanded.get("devices", []) if row.get("device_key") == device.get("device_key")), None)
                if expanded_device:
                    for candidate in expanded_device.get("candidates", []):
                        if candidate in device.get("candidates", []):
                            continue
                        summary.candidates_scanned += 1
                        decision = classify_candidate(
                            device_name=device.get("display_name"), relative_path=candidate["relative_path"], files=candidate.get("files", [])
                        )
                        if not decision.accepted:
                            continue
                        summary.candidates_accepted += 1
                        candidate_succeeded = False
                        for item in candidate.get("files", []):
                            if limit is not None and summary.source_attempts >= limit:
                                stop_requested = True
                                break
                            source = {**item, "device_key": device["device_key"], "device_name": device.get("display_name"), "device_vendor": vendor, "device_model": model, "adapter": decision.adapter}
                            summary.source_attempts += 1
                            outcome = self._ingest_source(source)
                            if outcome == "imported":
                                summary.new_imports += 1
                                candidate_succeeded = True
                            elif outcome == "duplicate":
                                summary.duplicates += 1
                                candidate_succeeded = True
                            else:
                                summary.failures += 1
                        if candidate_succeeded:
                            self.state.remember_directory(
                                str(device["device_key"]), str(device.get("display_name", "")), candidate["relative_path"]
                            )
            if stop_requested:
                break
        self.state.save()
        self._log("ingest", summary.as_dict())
        return summary

    def ingest_staged_for_test(self, staged_path: Path, source: dict[str, Any]) -> str:
        """Commit a locally created staging file; used only by synthetic regression tests."""
        outcome = self._commit_staged(staged_path, source)
        self.state.save()
        return outcome

    def _ingest_source(self, source: dict[str, Any]) -> str:
        key = self._source_key(source)
        source_size = int(source.get("size_bytes") or 0)
        if self.state.source_is_current(key, source_size, source.get("modified_at")):
            known_sha = self.state.source_sha256(key)
            if known_sha and self._ready_pair_exists(known_sha):
                return "duplicate"
        stage_directory = self.paths["stage"] / uuid.uuid4().hex
        stage_directory.mkdir(parents=True, exist_ok=True)
        try:
            staged_path = self.bridge.copy_to_staging(source, stage_directory)
            return self._commit_staged(staged_path, source)
        except Exception as exc:
            staged_files = [path for path in stage_directory.glob("*") if path.is_file()]
            for path in staged_files:
                self._move_to_failed(path, source, str(exc))
            return "failed"
        finally:
            shutil.rmtree(stage_directory, ignore_errors=True)

    def _commit_staged(self, staged_path: Path, source: dict[str, Any]) -> str:
        if not staged_path.exists() or not staged_path.is_file():
            raise RuntimeError("Staging file is absent")
        expected_size = int(source.get("size_bytes") or 0)
        actual_size = staged_path.stat().st_size
        if expected_size and actual_size != expected_size:
            self._move_to_failed(staged_path, source, f"size_mismatch expected={expected_size} actual={actual_size}")
            return "failed"
        # Some MTP providers omit System.Size. The completed local byte count is then
        # the best reliable size observation and is carried into the contract/state.
        source = {**source, "size_bytes": actual_size}
        sha256 = sha256_file(staged_path)
        source_key = self._source_key(source)
        if sha256 in self.state.data["imports"] or self._ready_pair_exists(sha256):
            staged_path.unlink(missing_ok=True)
            self.state.remember_source(source_key, actual_size, source.get("modified_at"), sha256)
            return "duplicate"

        timestamp, _ = self._recorded_timestamp(source)
        extension = Path(str(source["name"])).suffix.lower()
        if not extension:
            self._move_to_failed(staged_path, source, "source_has_no_extension")
            return "failed"
        base_name = f"{timestamp}_{sha256[:12]}"
        media_path = self.paths["ready"] / f"{base_name}{extension}"
        sidecar_path = self.paths["ready"] / f"{base_name}.json"
        if media_path.exists() and not sidecar_path.exists():
            self._move_ready_orphan_to_failed(media_path, "orphaned_ready_media_recovered")
        if media_path.exists() or sidecar_path.exists():
            self._move_to_failed(staged_path, source, "ready_name_collision")
            return "failed"

        metadata = build_metadata(source=source, sha256=sha256, media_filename=media_path.name)
        validate_recording(metadata)
        try:
            os.replace(staged_path, media_path)
            # This is the explicit downstream commit boundary: media exists before JSON can appear.
            self.sidecar_writer(sidecar_path, metadata)
        except Exception:
            if media_path.exists() and not sidecar_path.exists():
                self._move_ready_orphan_to_failed(media_path, "sidecar_commit_failed")
            raise
        self.state.remember_import(sha256, media_path.name)
        self.state.remember_source(source_key, actual_size, source.get("modified_at"), sha256)
        return "imported"

    def _recorded_timestamp(self, source: dict[str, Any]) -> tuple[str, str]:
        from .contract import recorded_time_from_evidence

        imported_at = iso_now()
        value, provenance = recorded_time_from_evidence(str(source["name"]), source.get("modified_at"), imported_at)
        compact = value.replace("-", "").replace(":", "").replace("+", "_").replace("T", "_")[:15]
        return compact, provenance

    def _ready_pair_exists(self, sha256: str) -> bool:
        item = self.state.data["imports"].get(sha256)
        if item:
            media = self.paths["ready"] / item["media_filename"]
            return media.exists() and media.with_suffix(".json").exists()
        for metadata_path in self.paths["ready"].glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if metadata.get("sha256") == sha256:
                return (self.paths["ready"] / metadata.get("media_filename", "")).exists()
        return False

    def _source_key(self, source: dict[str, Any]) -> str:
        return f"{source.get('device_key', '')}|{source['relative_path']}"

    def _write_sidecar_atomically(self, path: Path, metadata: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".metadata-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not (path.parent / metadata["media_filename"]).exists():
                raise RuntimeError("Refusing sidecar commit before media exists")
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _move_to_failed(self, source_path: Path, source: dict[str, Any], reason: str) -> None:
        token = uuid.uuid4().hex[:12]
        failed_media = self.paths["failed"] / f"{iso_now().replace(':', '').replace('+', '_')}_{token}{source_path.suffix}.part"
        if source_path.exists():
            shutil.move(str(source_path), failed_media)
        evidence = {
            "failed_at": iso_now(),
            "reason": reason,
            "source_name": source.get("name"),
            "source_relative_path": source.get("relative_path"),
            "expected_size_bytes": source.get("size_bytes"),
        }
        self._write_json_atomic(failed_media.with_suffix(".failure.json"), evidence)

    def _move_ready_orphan_to_failed(self, media_path: Path, reason: str) -> None:
        target = self.paths["failed"] / f"{iso_now().replace(':', '').replace('+', '_')}_{media_path.name}.orphan"
        shutil.move(str(media_path), target)
        self._write_json_atomic(target.with_suffix(".failure.json"), {"failed_at": iso_now(), "reason": reason})

    def _write_json_atomic(self, path: Path, content: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".failure-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(content, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _log(self, event: str, data: dict[str, Any]) -> None:
        line = json.dumps({"at": iso_now(), "event": event, **data}, ensure_ascii=False, sort_keys=True)
        with (self.paths["logs"] / "ingest.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
