from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters import classify_candidate, device_identity
from .bridge import MtpBridge
from .calllog_export import (
    AMBIGUOUS,
    EXACT,
    HIGH_CONFIDENCE,
    NO_MATCH,
    CallLogExportError,
    CallLogExportProvider,
    correlate_recording_to_calllog,
    inspect_xml_schema,
    registered_calllog_export_providers,
    safe_rows_summary,
)
from .config import ensure_layout, resolve_salesperson_id
from .contract import build_metadata, iso_now, sha256_file, validate_recording
from .events import (
    build_communication_event,
    event_path,
    normalise_phone_number,
    replace_event_atomically,
    validate_event,
    write_event_atomically,
)
from .identity import (
    audio_tag_phone_candidate_details,
    classify_call_log_exposure,
    filename_structure,
    phone_candidate_details,
    read_mp3_id3,
    resolve_direct_phone,
    safe_wpd_summary,
    wpd_phone_candidate_details,
)
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
    events_created: int = 0
    events_existing: int = 0
    event_failures: int = 0
    calllog_xml_candidates: int = 0
    calllog_new_artifacts: int = 0
    calllog_canonical_rows_new: int = 0
    calllog_events_enriched: int = 0
    calllog_failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "devices": self.devices,
            "candidates_scanned": self.candidates_scanned,
            "candidates_accepted": self.candidates_accepted,
            "new_imports": self.new_imports,
            "duplicates": self.duplicates,
            "failures": self.failures,
            "source_attempts": self.source_attempts,
            "events_created": self.events_created,
            "events_existing": self.events_existing,
            "event_failures": self.event_failures,
            "calllog_xml_candidates": self.calllog_xml_candidates,
            "calllog_new_artifacts": self.calllog_new_artifacts,
            "calllog_canonical_rows_new": self.calllog_canonical_rows_new,
            "calllog_events_enriched": self.calllog_events_enriched,
            "calllog_failures": self.calllog_failures,
        }


@dataclass
class CallLogExportSummary:
    devices: int = 0
    directories_scanned: int = 0
    xml_candidates: int = 0
    new_artifacts: int = 0
    duplicate_artifacts: int = 0
    schema_valid: int = 0
    schema_failures: int = 0
    copy_failures: int = 0
    parsed_rows: int = 0
    canonical_rows_new: int = 0
    canonical_rows_duplicate: int = 0
    correlations_exact: int = 0
    correlations_high_confidence: int = 0
    correlations_ambiguous: int = 0
    correlations_no_match: int = 0
    events_enriched: int = 0
    events_already_enriched: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "devices": self.devices,
            "directories_scanned": self.directories_scanned,
            "xml_candidates": self.xml_candidates,
            "new_artifacts": self.new_artifacts,
            "duplicate_artifacts": self.duplicate_artifacts,
            "schema_valid": self.schema_valid,
            "schema_failures": self.schema_failures,
            "copy_failures": self.copy_failures,
            "parsed_rows": self.parsed_rows,
            "canonical_rows_new": self.canonical_rows_new,
            "canonical_rows_duplicate": self.canonical_rows_duplicate,
            "correlations_exact": self.correlations_exact,
            "correlations_high_confidence": self.correlations_high_confidence,
            "correlations_ambiguous": self.correlations_ambiguous,
            "correlations_no_match": self.correlations_no_match,
            "events_enriched": self.events_enriched,
            "events_already_enriched": self.events_already_enriched,
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
        self.salesperson_id = resolve_salesperson_id()

    def probe(self) -> dict[str, Any]:
        raw = self.bridge.probe(self.state.known_dirs())
        return self._probe_from_raw(raw)

    def _probe_from_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
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
                    "adapter_evidence_status": decision.adapter_evidence_status,
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

    def save_probe_report(self) -> Path:
        """Persist a diagnostic report with structural filename facts only."""
        raw = self.bridge.probe(self.state.known_dirs())
        report = {
            "report_schema_version": "probe-report/v1",
            "created_at": iso_now(),
            "bridge_observation": raw.get("observation", "ok"),
            "devices": [],
        }
        for device in raw.get("devices", []):
            vendor, model = device_identity(device.get("display_name"))
            candidates: list[dict[str, Any]] = []
            for candidate in device.get("candidates", []):
                files = candidate.get("files", [])
                decision = classify_candidate(
                    device_name=device.get("display_name"), relative_path=candidate["relative_path"], files=files
                )
                extensions: dict[str, int] = {}
                total_size = 0
                patterns: list[dict[str, Any]] = []
                seen_patterns: set[str] = set()
                for item in files:
                    extension = str(item.get("extension") or "").lower()
                    extensions[extension] = extensions.get(extension, 0) + 1
                    total_size += int(item.get("size_bytes") or 0)
                    pattern = self._safe_filename_pattern(str(item.get("name") or ""), extension)
                    fingerprint = json.dumps(pattern, sort_keys=True)
                    if fingerprint not in seen_patterns:
                        patterns.append(pattern)
                        seen_patterns.add(fingerprint)
                candidates.append({
                    "relative_path": candidate["relative_path"],
                    "audio_file_count": len(files),
                    "extension_distribution": extensions,
                    "total_size_bytes": total_size,
                    "metadata_available": {
                        "size_bytes": any(item.get("size_bytes") is not None for item in files),
                        "modified_at": any(item.get("modified_at") is not None for item in files),
                        "duration_seconds": any(item.get("duration_seconds") is not None for item in files),
                    },
                    "filename_structure_patterns": patterns,
                    "accepted": decision.accepted,
                    "adapter": decision.adapter,
                    "adapter_evidence_status": decision.adapter_evidence_status,
                    "evidence": decision.evidence,
                    "probable_call_recording": decision.accepted,
                    "eligible_for_ingest_smoke_test": decision.accepted and bool(files),
                })
            report["devices"].append({
                "vendor": vendor,
                "model": model,
                "transport": "Windows Shell / MTP-WPD",
                "storage_root_count": len(device.get("storage_roots", [])),
                "candidate_count": len(candidates),
                "candidates": candidates,
            })
        reports_dir = self.data_root / "diagnostics" / "probe-reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        safe_timestamp = iso_now().replace(":", "").replace("+", "_")
        output = reports_dir / f"probe-{safe_timestamp}.json"
        self._write_json_atomic(output, report)
        self._log("probe_report_saved", {"device_count": len(report["devices"]), "candidate_count": sum(row["candidate_count"] for row in report["devices"])})
        return output

    def inspect_calllog_exports(self) -> CallLogExportSummary:
        """Copy only bounded public exporter XML files into ignored diagnostics and inspect their shape."""
        sources, devices, directory_count = self._discover_calllog_export_sources()
        summary = CallLogExportSummary(devices=devices, directories_scanned=directory_count, xml_candidates=len(sources))
        for _, source in sources:
            try:
                outcome, _, _, _ = self._stage_calllog_export(source)
                if outcome == "new":
                    summary.new_artifacts += 1
                else:
                    summary.duplicate_artifacts += 1
                summary.schema_valid += 1
            except CallLogExportError:
                summary.schema_failures += 1
            except Exception:
                summary.copy_failures += 1
        self.state.save()
        self._log("calllog_export_inspection", summary.as_dict())
        return summary

    def ingest_calllog_exports(self) -> CallLogExportSummary:
        """Persist canonical rows from registered export providers, then enrich only unique matches."""
        sources, devices, directory_count = self._discover_calllog_export_sources()
        summary = CallLogExportSummary(devices=devices, directories_scanned=directory_count, xml_candidates=len(sources))
        for provider, source in sources:
            try:
                outcome, artifact_sha256, artifact_path, _ = self._stage_calllog_export(source)
                if outcome == "new":
                    summary.new_artifacts += 1
                else:
                    summary.duplicate_artifacts += 1
                rows = provider.parse(artifact_path, artifact_sha256)
                summary.schema_valid += 1
                summary.parsed_rows += len(rows)
                for row in rows:
                    persisted = {**row, "device_key": source["device_key"]}
                    if self.state.remember_calllog_row(persisted):
                        summary.canonical_rows_new += 1
                    else:
                        summary.canonical_rows_duplicate += 1
                self._write_calllog_parse_summary(artifact_sha256, provider.provider_id, rows)
            except CallLogExportError:
                summary.schema_failures += 1
            except Exception:
                summary.copy_failures += 1
        correlation = self._reconcile_calllog_events()
        summary.correlations_exact += correlation[EXACT]
        summary.correlations_high_confidence += correlation[HIGH_CONFIDENCE]
        summary.correlations_ambiguous += correlation[AMBIGUOUS]
        summary.correlations_no_match += correlation[NO_MATCH]
        summary.events_enriched += correlation["events_enriched"]
        summary.events_already_enriched += correlation["events_already_enriched"]
        self.state.save()
        self._log("calllog_export_ingest", summary.as_dict())
        return summary

    def _discover_calllog_export_sources(self) -> tuple[list[tuple[CallLogExportProvider, dict[str, Any]]], int, int]:
        sources: list[tuple[CallLogExportProvider, dict[str, Any]]] = []
        device_keys: set[str] = set()
        directories = 0
        for provider in registered_calllog_export_providers():
            raw = self.bridge.discover_calllog_exports(
                directory_names=list(provider.directory_names),
                file_name_prefixes=list(provider.filename_prefixes),
                search_depth=1,
            )
            for device in raw.get("devices", []):
                device_key = str(device.get("device_key") or "")
                if device_key:
                    device_keys.add(device_key)
                for directory in device.get("directories", []):
                    directories += 1
                    for item in directory.get("files", []):
                        sources.append((provider, {
                            **item,
                            "device_key": device_key,
                            "device_name": str(device.get("display_name") or ""),
                            "calllog_provider": provider.provider_id,
                        }))
        return sources, len(device_keys), directories

    def _stage_calllog_export(self, source: dict[str, Any]) -> tuple[str, str, Path, dict[str, Any]]:
        source_key = self._calllog_source_key(source)
        expected_size = int(source.get("size_bytes") or 0)
        known_sha = self.state.calllog_source_sha256(source_key, expected_size, source.get("modified_at"))
        if known_sha:
            existing = self.paths["calllog_diagnostics"] / f"{known_sha}.xml"
            if existing.is_file():
                return "duplicate", known_sha, existing, self._write_calllog_schema_summary(existing, known_sha)

        stage_directory = self.paths["calllog_stage"] / uuid.uuid4().hex
        stage_directory.mkdir(parents=True, exist_ok=True)
        try:
            staged = self.bridge.copy_to_staging(source, stage_directory)
            actual_size = staged.stat().st_size
            if expected_size and actual_size != expected_size:
                self._move_calllog_stage_to_failed(staged, source, f"size_mismatch expected={expected_size} actual={actual_size}")
                raise RuntimeError("call-log export size mismatch")
            sha256 = sha256_file(staged)
            destination = self.paths["calllog_diagnostics"] / f"{sha256}.xml"
            if destination.exists():
                staged.unlink(missing_ok=True)
                outcome = "duplicate"
            else:
                os.replace(staged, destination)
                outcome = "new"
            self.state.remember_calllog_source(source_key, actual_size, source.get("modified_at"), sha256)
            self.state.remember_calllog_artifact(sha256, actual_size)
            return outcome, sha256, destination, self._write_calllog_schema_summary(destination, sha256)
        except Exception as exc:
            for partial in stage_directory.glob("*"):
                if partial.is_file():
                    self._move_calllog_stage_to_failed(partial, source, str(exc))
            raise
        finally:
            shutil.rmtree(stage_directory, ignore_errors=True)

    def _write_calllog_schema_summary(self, artifact_path: Path, sha256: str) -> dict[str, Any]:
        summary = inspect_xml_schema(artifact_path)
        safe_summary = {
            "artifact_sha256": sha256,
            "artifact_size_bytes": artifact_path.stat().st_size,
            **summary,
        }
        self._write_json_atomic(self.paths["calllog_diagnostics"] / f"{sha256}.schema.json", safe_summary)
        return safe_summary

    def _write_calllog_parse_summary(self, artifact_sha256: str, provider_id: str, rows: list[dict[str, Any]]) -> None:
        self._write_json_atomic(self.paths["calllog_diagnostics"] / f"{artifact_sha256}.parse-summary.json", {
            "parse_summary_version": "synctech-calllog-export-safe-summary/v1",
            "artifact_sha256": artifact_sha256,
            "provider_id": provider_id,
            **safe_rows_summary(rows),
        })

    def _move_calllog_stage_to_failed(self, path: Path, source: dict[str, Any], reason: str) -> None:
        token = uuid.uuid4().hex[:12]
        target = self.paths["calllog_failed"] / f"{token}{path.suffix}.part"
        if path.exists():
            shutil.move(str(path), target)
        self._write_json_atomic(target.with_suffix(".failure.json"), {
            "failed_at": iso_now(),
            "reason": reason[:300],
            "expected_size_bytes": source.get("size_bytes"),
        })

    def _reconcile_calllog_events(self) -> dict[str, int]:
        result = {EXACT: 0, HIGH_CONFIDENCE: 0, AMBIGUOUS: 0, NO_MATCH: 0, "events_enriched": 0, "events_already_enriched": 0}
        safe_observations: list[dict[str, Any]] = []
        current_source_devices = self._current_recording_source_devices()
        for event_path_value in self.paths["events"].glob("*.json"):
            event = json.loads(event_path_value.read_text(encoding="utf-8"))
            validate_event(event)
            recording_path = next((
                path for path in self.paths["ready"].glob("*.json")
                if json.loads(path.read_text(encoding="utf-8")).get("recording_id") == event.get("recording_id")
            ), None)
            if recording_path is None:
                continue
            recording = json.loads(recording_path.read_text(encoding="utf-8"))
            validate_recording(recording)
            device_key = self._recording_device_key(recording, current_source_devices)
            if not device_key:
                continue
            correlation = correlate_recording_to_calllog(
                occurred_at=event.get("occurred_at"),
                occurred_at_source=event.get("occurred_at_source"),
                recording_duration_seconds=event.get("duration_seconds"),
                rows=self.state.calllog_rows_for_device(device_key),
            )
            result[correlation.status] += 1
            safe_observations.append({"event_id": event["event_id"], **correlation.safe_summary()})
            row = correlation.matched_row
            if correlation.status not in {EXACT, HIGH_CONFIDENCE} or row is None:
                continue
            canonical_call_id = str(row["canonical_call_id"])
            if self.state.calllog_enrichment_matches(event["event_id"], canonical_call_id):
                result["events_already_enriched"] += 1
                continue
            updated_event = self._enriched_event(event, row)
            if updated_event != event:
                media_path = self.paths["ready"] / str(recording["media_filename"])
                replace_event_atomically(event_path_value, updated_event, media_path, recording_path)
                result["events_enriched"] += 1
            self.state.remember_calllog_enrichment(event["event_id"], canonical_call_id)
        if safe_observations:
            self._write_json_atomic(self.paths["calllog_diagnostics"] / "latest-correlation-summary.json", {
                "summary_version": "calllog-recording-correlation-summary/v1",
                "observations": safe_observations,
            })
        return result

    @staticmethod
    def _enriched_event(event: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        updated = dict(event)
        phone = row.get("phone_number_raw")
        if isinstance(phone, str) and phone:
            updated["phone_number_raw"] = phone
            updated["phone_number_normalized"] = normalise_phone_number(phone)
            updated["phone_number_source"] = "calllog_export"
            updated["phone_number_confidence"] = "medium"
        contact_name = row.get("contact_name")
        if isinstance(contact_name, str) and contact_name:
            updated["contact_name"] = contact_name
            updated["contact_name_source"] = "calllog_export"
        direction = row.get("call_direction")
        if direction in {"incoming", "outgoing"}:
            updated["call_direction"] = direction
            updated["call_direction_source"] = "calllog_export"
        validate_event(updated)
        return updated

    def _current_recording_source_devices(self) -> dict[str, str]:
        """Read current recording candidates once to bind transient MTP device keys safely."""
        try:
            raw = self.bridge.probe(self.state.known_dirs())
        except Exception:
            return {}
        return {
            str(item.get("relative_path")): str(device.get("device_key"))
            for device in raw.get("devices", [])
            for candidate in device.get("candidates", [])
            for item in candidate.get("files", [])
            if item.get("relative_path") and device.get("device_key")
        }

    def _recording_device_key(self, recording: dict[str, Any], current_source_devices: dict[str, str]) -> str | None:
        current_key = current_source_devices.get(str(recording.get("source_relative_path") or ""))
        if current_key:
            return current_key
        matching_keys = [
            key.split("|", 1)[0]
            for key, source in self.state.data["sources"].items()
            if source.get("sha256") == recording.get("sha256") and "|" in key
        ]
        return matching_keys[0] if len(set(matching_keys)) == 1 else None

    def investigate_identity(self) -> dict[str, Any]:
        """Perform a bounded, read-only identity probe for one ready recording."""
        recordings = sorted(self.paths["ready"].glob("*.json"))
        if len(recordings) != 1:
            raise RuntimeError("Identity investigation currently requires exactly one ready recording")
        recording_path = recordings[0]
        recording = json.loads(recording_path.read_text(encoding="utf-8"))
        validate_recording(recording)
        media_path = self.paths["ready"] / str(recording["media_filename"])
        if not media_path.is_file() or sha256_file(media_path) != recording["sha256"]:
            raise RuntimeError("Ready recording media is absent or its sha256 differs")

        current_source, probe_raw = self._find_current_source(recording)
        wpd_inspection: dict[str, Any] | None = None
        wpd_error: str | None = None
        if current_source:
            try:
                wpd_inspection = self.bridge.inspect_source(current_source)
            except Exception as exc:
                wpd_error = str(exc)[:300]
        else:
            wpd_error = "ready_recording_source_not_currently_enumerated"

        capabilities: dict[str, Any] | None = None
        capability_error: str | None = None
        try:
            capabilities = self.bridge.inspect_capabilities()
        except Exception as exc:
            capability_error = str(exc)[:300]

        audio_metadata = read_mp3_id3(media_path) if recording["original_extension"].casefold() == ".mp3" else {"format": "unsupported", "tags": {}}
        direct = resolve_direct_phone({
            "filename": phone_candidate_details(str(recording["original_filename"])),
            "wpd_metadata": wpd_phone_candidate_details(wpd_inspection),
            "audio_metadata": audio_tag_phone_candidate_details(audio_metadata),
        })
        call_log = classify_call_log_exposure(capabilities)
        existing_event_path, existing_event = self._event_for_recording(recording)
        enriched = False
        if direct["status"] == "DIRECT_RECORDING_PHONE_ID_AVAILABLE":
            enriched = self._apply_direct_phone_enrichment(
                recording_path=recording_path,
                recording=recording,
                event_path_value=existing_event_path,
                event=existing_event,
                direct=direct,
            )

        raw_report = {
            "report_schema_version": "identity-investigation-raw/v1",
            "created_at": iso_now(),
            "recording": recording,
            "event_before": existing_event,
            "current_source_enumerated": current_source,
            "recording_probe": probe_raw,
            "wpd_inspection": wpd_inspection,
            "wpd_error": wpd_error,
            "audio_metadata": audio_metadata,
            "direct_resolution": direct,
            "mtp_wpd_capabilities": capabilities,
            "capability_error": capability_error,
        }
        reports_dir = self.data_root / "diagnostics" / "identity-investigation"
        reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = iso_now().replace(":", "").replace("+", "_")
        raw_path = reports_dir / f"identity-{timestamp}.raw.json"
        summary_path = reports_dir / f"identity-{timestamp}.summary.json"
        self._write_json_atomic(raw_path, raw_report)

        current_event = json.loads(existing_event_path.read_text(encoding="utf-8")) if existing_event_path.is_file() else existing_event
        summary = {
            "report_schema_version": "identity-investigation-summary/v1",
            "created_at": iso_now(),
            "scope": "one_ready_recording_and_its_current_mtp_source",
            "direct_recording_identity": {
                "status": direct["status"],
                "phone_number": direct.get("masked_phone"),
                "phone_number_source": direct["phone_number_source"],
                "phone_number_confidence": direct["phone_number_confidence"],
                "evidence_level": direct["evidence_level"],
                "source_candidate_counts": direct["source_candidate_counts"],
                "filename_structure": filename_structure(str(recording["original_filename"])),
                "audio_tag_names": sorted((audio_metadata.get("tags") or {}).keys()),
                "audio_format": audio_metadata.get("format"),
                "wpd": safe_wpd_summary(wpd_inspection),
                "wpd_inspection_error": wpd_error,
            },
            "call_log_transport": {
                **call_log,
                "capability_probe_error": capability_error,
                "automatic_no_extra_permission_source": "NOT_FOUND_IN_CURRENT_MTP_WPD_PROBE" if call_log["status"] != "CALL_LOG_EXPOSED_VIA_CURRENT_MTP_WPD" else "PRESENT",
            },
            "event": {
                "phone_number_raw": "PRESENT" if current_event.get("phone_number_raw") else "NULL",
                "phone_number_normalized": "PRESENT" if current_event.get("phone_number_normalized") else "NULL",
                "phone_number_source": current_event.get("phone_number_source"),
                "phone_number_confidence": current_event.get("phone_number_confidence"),
                "contact_name": "PRESENT" if current_event.get("contact_name") else "NULL",
                "contact_name_source": current_event.get("contact_name_source"),
                "call_direction": current_event.get("call_direction"),
                "call_direction_source": current_event.get("call_direction_source"),
                "occurred_at_source": current_event.get("occurred_at_source"),
                "duration_source": current_event.get("duration_source"),
                "identity_enrichment_applied": enriched,
            },
        }
        self._write_json_atomic(summary_path, summary)
        self._log("identity_investigation", {
            "direct_status": direct["status"],
            "call_log_status": call_log["status"],
            "identity_enrichment_applied": enriched,
        })
        return {"summary_path": summary_path, "raw_path": raw_path, **summary}

    def _find_current_source(self, recording: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        try:
            raw = self.bridge.probe(self.state.known_dirs())
        except Exception as exc:
            return None, {"probe_error": str(exc)[:300]}
        target_path = str(recording["source_relative_path"])
        for device in raw.get("devices", []):
            for candidate in device.get("candidates", []):
                for item in candidate.get("files", []):
                    if str(item.get("relative_path")) == target_path:
                        return {
                            **item,
                            "device_key": device["device_key"],
                            "device_name": device.get("display_name"),
                            "relative_path": target_path,
                        }, raw
        return None, raw

    def _event_for_recording(self, recording: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        for candidate_path in self.paths["events"].glob("*.json"):
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            if candidate.get("recording_id") == recording.get("recording_id"):
                validate_event(candidate)
                return candidate_path, candidate
        event = build_communication_event(
            recording=recording, installation_id=self.state.installation_id(), salesperson_id=self.salesperson_id
        )
        return event_path(self.paths["events"], event), event

    def _apply_direct_phone_enrichment(
        self,
        *,
        recording_path: Path,
        recording: dict[str, Any],
        event_path_value: Path,
        event: dict[str, Any],
        direct: dict[str, Any],
    ) -> bool:
        updated_recording = {
            **recording,
            "phone_number": direct["phone_number_raw"],
            "phone_number_source": direct["phone_number_source"],
            "phone_number_confidence": direct["phone_number_confidence"],
        }
        validate_recording(updated_recording)
        self._write_sidecar_atomically(recording_path, updated_recording)
        updated_event = build_communication_event(
            recording=updated_recording, installation_id=event["installation_id"], salesperson_id=self.salesperson_id
        )
        media_path = self.paths["ready"] / str(updated_recording["media_filename"])
        if event_path_value.exists():
            replace_event_atomically(event_path_value, updated_event, media_path, recording_path)
        else:
            write_event_atomically(event_path_value, updated_event, media_path, recording_path)
        return True

    @staticmethod
    def _safe_filename_pattern(name: str, extension: str) -> dict[str, Any]:
        stem = name[:-len(extension)] if extension and name.casefold().endswith(extension.casefold()) else name
        digit_runs = [len(match.group(0)) for match in re.finditer(r"\d+", stem)]
        datetime_match = re.search(r"(?:19|20)\d{2}[-_]?\d{2}[-_]?\d{2}[ _-]?\d{2}[-_]?\d{2}[-_]?\d{2}", stem)
        return {
            "basename_length": len(name),
            "extension": extension,
            "digit_run_lengths": digit_runs,
            "separators": sorted(set(character for character in stem if character in "-_ .()")),
            "datetime_token_start": datetime_match.start() if datetime_match else None,
        }

    def ingest_once(self, limit: int | None = None) -> IngestSummary:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        summary = IngestSummary()
        stop_requested = False
        raw = self.bridge.probe(self.state.known_dirs())
        devices = raw.get("devices", [])
        summary.devices = len(devices)
        self._refresh_legacy_duration_provenance(raw)
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
                        "duration_source": "wpd" if item.get("duration_seconds") is not None else "unknown",
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
                            source = {**item, "device_key": device["device_key"], "device_name": device.get("display_name"), "device_vendor": vendor, "device_model": model, "adapter": decision.adapter, "duration_source": "wpd" if item.get("duration_seconds") is not None else "unknown"}
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
        event_summary = self._reconcile_events()
        summary.events_created += event_summary["created"]
        summary.events_existing += event_summary["existing"]
        summary.event_failures += event_summary["failures"]
        try:
            calllog_summary = self.ingest_calllog_exports()
            summary.calllog_xml_candidates += calllog_summary.xml_candidates
            summary.calllog_new_artifacts += calllog_summary.new_artifacts
            summary.calllog_canonical_rows_new += calllog_summary.canonical_rows_new
            summary.calllog_events_enriched += calllog_summary.events_enriched
        except Exception as exc:
            # Call-log export is optional enrichment. It must not block a sound recording import.
            summary.calllog_failures += 1
            self._log("calllog_export_failure", {"reason": str(exc)[:300]})
        self.state.save()
        self._log("ingest", summary.as_dict())
        return summary

    def ingest_staged_for_test(self, staged_path: Path, source: dict[str, Any]) -> str:
        """Commit a locally created staging file; used only by synthetic regression tests."""
        outcome = self._commit_staged(staged_path, source)
        self._reconcile_events()
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

    def _refresh_legacy_duration_provenance(self, raw: dict[str, Any]) -> None:
        """Upgrade only a legacy sidecar that can be matched to current WPD metadata."""
        durations: dict[str, float] = {}
        for device in raw.get("devices", []):
            for candidate in device.get("candidates", []):
                for item in candidate.get("files", []):
                    value = item.get("duration_seconds")
                    if value is not None:
                        durations[str(item.get("relative_path"))] = float(value)
        if not durations:
            return
        for metadata_path in self.paths["ready"].glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            source_path = str(metadata.get("source_relative_path") or "")
            duration = durations.get(source_path)
            if duration is None or metadata.get("duration_source") == "wpd":
                continue
            if metadata.get("duration_seconds") is None:
                continue
            if abs(float(metadata["duration_seconds"]) - duration) > 0.001:
                continue
            updated = {**metadata, "duration_source": "wpd"}
            validate_recording(updated)
            self._write_sidecar_atomically(metadata_path, updated)

    def _reconcile_events(self) -> dict[str, int]:
        result = {"created": 0, "existing": 0, "failures": 0}
        installation_id = self.state.installation_id()
        for recording_path in self.paths["ready"].glob("*.json"):
            try:
                recording = json.loads(recording_path.read_text(encoding="utf-8"))
                validate_recording(recording)
                media_path = self.paths["ready"] / str(recording["media_filename"])
                if not media_path.is_file() or sha256_file(media_path) != recording["sha256"]:
                    raise RuntimeError("recording media is absent or its sha256 differs")
                event = build_communication_event(
                    recording=recording, installation_id=installation_id, salesperson_id=self.salesperson_id
                )
                validate_event(event)
                created = write_event_atomically(event_path(self.paths["events"], event), event, media_path, recording_path)
                result["created" if created else "existing"] += 1
            except Exception as exc:
                result["failures"] += 1
                self._log("event_failure", {"reason": str(exc)[:300]})
        return result

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

    def _calllog_source_key(self, source: dict[str, Any]) -> str:
        return f"{source.get('calllog_provider', '')}|{self._source_key(source)}"

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
