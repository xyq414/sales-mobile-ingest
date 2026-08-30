from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .config import ConfigError, SalespersonIdentity
from .contract import sha256_file, validate_recording
from .events import validate_event
from .state import StateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLOUD_RECORDING_SCHEMA_PATH = PROJECT_ROOT / "contract" / "cloud_recording_package.schema.json"
CLOUD_EVENT_SCHEMA_PATH = PROJECT_ROOT / "contract" / "cloud_communication_event_package.schema.json"
_WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)), *(f"LPT{number}" for number in range(1, 10)),
}
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2}|/)")


class CloudHandoffError(ValueError):
    """A cloud package cannot be safely built, published or accepted."""


@dataclass(frozen=True)
class PackageValidation:
    event_id: str
    recording_id: str
    media_sha256: str
    media_filename: str
    package_fingerprint: str


@dataclass
class CloudPublishSummary:
    status: str
    published: int = 0
    already_published: int = 0
    immutable_enrichment_pending: int = 0
    conflicts: int = 0
    blocked: int = 0
    failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cloud_handoff_status": self.status,
            "cloud_packages_published": self.published,
            "cloud_packages_already_published": self.already_published,
            "cloud_packages_immutable_enrichment_pending": self.immutable_enrichment_pending,
            "cloud_packages_conflicts": self.conflicts,
            "cloud_packages_blocked": self.blocked,
            "cloud_packages_failures": self.failures,
        }


def cloud_recording_schema() -> dict[str, Any]:
    return json.loads(CLOUD_RECORDING_SCHEMA_PATH.read_text(encoding="utf-8"))


def cloud_event_schema() -> dict[str, Any]:
    return json.loads(CLOUD_EVENT_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_cloud_recording(payload: dict[str, Any]) -> None:
    _validate_schema(payload, cloud_recording_schema(), "cloud recording")
    _reject_absolute_paths(payload)


def validate_cloud_event(payload: dict[str, Any]) -> None:
    _validate_schema(payload, cloud_event_schema(), "cloud event")
    _reject_absolute_paths(payload)


def sanitize_windows_component(value: str) -> str:
    """Create a deterministic display-only Windows folder component."""
    normalized = unicodedata.normalize("NFKC", value.strip())
    cleaned = _WINDOWS_FORBIDDEN.sub("_", normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "unnamed"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:100]


def salesperson_folder_name(identity: SalespersonIdentity) -> str:
    return f"{sanitize_windows_component(identity.salesperson_id)}_{sanitize_windows_component(identity.salesperson_name)}"


def package_date_and_folder(event: dict[str, Any]) -> tuple[str, str]:
    occurred_at = event.get("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at:
        raise CloudHandoffError("OCCURRED_AT_REQUIRED_FOR_CLOUD_HANDOFF")
    try:
        value = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloudHandoffError("OCCURRED_AT_INVALID_FOR_CLOUD_HANDOFF") from exc
    if value.tzinfo is None:
        raise CloudHandoffError("OCCURRED_AT_TIMEZONE_REQUIRED_FOR_CLOUD_HANDOFF")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"evt_[a-f0-9]{64}", event_id):
        raise CloudHandoffError("EVENT_ID_INVALID_FOR_CLOUD_HANDOFF")
    return value.date().isoformat(), f"{value.strftime('%Y%m%d_%H%M%S')}_{event_id[4:16]}"


def project_cloud_package(recording: dict[str, Any], event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Project canonical local contracts into portable cloud-only package contracts."""
    validate_recording(recording)
    validate_event(event)
    if event.get("recording_id") != recording.get("recording_id"):
        raise CloudHandoffError("RECORDING_EVENT_IDENTITY_MISMATCH")
    if event.get("media_sha256") != recording.get("sha256"):
        raise CloudHandoffError("RECORDING_EVENT_SHA256_MISMATCH")
    if event.get("original_extension") != recording.get("original_extension"):
        raise CloudHandoffError("RECORDING_EVENT_EXTENSION_MISMATCH")
    salesperson_id = event.get("salesperson_id")
    salesperson_name = event.get("salesperson_name")
    if event.get("salesperson_identity_status") != "CONFIGURED" or not isinstance(salesperson_id, str) or not salesperson_id.strip() or not isinstance(salesperson_name, str) or not salesperson_name.strip():
        raise CloudHandoffError("SALESPERSON_IDENTITY_UNCONFIGURED")
    if not isinstance(event.get("occurred_at"), str) or not event["occurred_at"]:
        raise CloudHandoffError("OCCURRED_AT_REQUIRED_FOR_CLOUD_HANDOFF")
    extension = str(recording["original_extension"]).lower()
    audio_name = f"audio{extension}"
    cloud_recording = {
        "schema_version": "cloud-recording-package/v1",
        "source_contract_version": recording["schema_version"],
        "recording_id": recording["recording_id"],
        "source_type": recording["source_type"],
        "device_vendor": recording["device_vendor"],
        "device_model": recording["device_model"],
        "original_extension": extension,
        "source_size_bytes": recording["source_size_bytes"],
        "source_modified_at": recording["source_modified_at"],
        "recorded_at": recording["recorded_at"],
        "recorded_at_source": recording["recorded_at_source"],
        "call_direction": recording["call_direction"],
        "duration_seconds": recording["duration_seconds"],
        "duration_source": recording.get("duration_source", "unknown"),
        "sha256": recording["sha256"],
        "media_filename": audio_name,
        "imported_at": recording["imported_at"],
        "ingest_adapter": recording["ingest_adapter"],
        "ingest_status": recording["ingest_status"],
    }
    cloud_event = {
        "schema_version": "cloud-communication-event-package/v1",
        "source_contract_version": event["schema_version"],
        "event_id": event["event_id"],
        "source_type": event["source_type"],
        "recording_id": event["recording_id"],
        "occurred_at": event["occurred_at"],
        "occurred_at_source": event["occurred_at_source"],
        "occurred_at_confidence": event["occurred_at_confidence"],
        "salesperson_id": salesperson_id.strip(),
        "salesperson_name": salesperson_name.strip(),
        "device_vendor": event["device_vendor"],
        "device_model": event["device_model"],
        "phone_number_raw": event["phone_number_raw"],
        "phone_number_normalized": event["phone_number_normalized"],
        "phone_number_source": event["phone_number_source"],
        "phone_number_confidence": event["phone_number_confidence"],
        "contact_name": event["contact_name"],
        "contact_name_source": event["contact_name_source"],
        "call_direction": event["call_direction"],
        "call_direction_source": event["call_direction_source"],
        "duration_seconds": event["duration_seconds"],
        "duration_source": event["duration_source"],
        "media_ref": audio_name,
        "media_sha256": event["media_sha256"],
        "original_extension": extension,
        "ingested_at": event["ingested_at"],
        "adapter": event["adapter"],
        "adapter_evidence_status": event["adapter_evidence_status"],
    }
    validate_cloud_recording(cloud_recording)
    validate_cloud_event(cloud_event)
    return cloud_recording, cloud_event, audio_name


def validate_cloud_package(package_directory: Path) -> PackageValidation:
    if not package_directory.is_dir() or package_directory.is_symlink():
        raise CloudHandoffError("CLOUD_PACKAGE_DIRECTORY_ABSENT")
    children = list(package_directory.iterdir())
    if len(children) != 3 or any(item.is_dir() or item.is_symlink() for item in children):
        raise CloudHandoffError("CLOUD_PACKAGE_MUST_CONTAIN_EXACTLY_THREE_REGULAR_FILES")
    recording_path = package_directory / "recording.json"
    event_path = package_directory / "event.json"
    audio_files = [item for item in children if item.is_file() and re.fullmatch(r"audio\.[A-Za-z0-9]{1,10}", item.name)]
    if not recording_path.is_file() or not event_path.is_file() or len(audio_files) != 1:
        raise CloudHandoffError("CLOUD_PACKAGE_REQUIRED_FILES_INVALID")
    try:
        recording = json.loads(recording_path.read_text(encoding="utf-8"))
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CloudHandoffError("CLOUD_PACKAGE_JSON_INVALID") from exc
    validate_cloud_recording(recording)
    validate_cloud_event(event)
    audio = audio_files[0]
    if recording["media_filename"] != audio.name or event["media_ref"] != audio.name:
        raise CloudHandoffError("CLOUD_PACKAGE_MEDIA_REFERENCE_INVALID")
    if event["recording_id"] != recording["recording_id"]:
        raise CloudHandoffError("CLOUD_PACKAGE_RECORDING_EVENT_IDENTITY_MISMATCH")
    actual_sha256 = sha256_file(audio)
    if recording["sha256"] != actual_sha256 or event["media_sha256"] != actual_sha256:
        raise CloudHandoffError("CLOUD_PACKAGE_MEDIA_SHA256_MISMATCH")
    fingerprint_payload = json.dumps({"recording": recording, "event": event, "sha256": actual_sha256}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return PackageValidation(
        event_id=event["event_id"],
        recording_id=recording["recording_id"],
        media_sha256=actual_sha256,
        media_filename=audio.name,
        package_fingerprint=hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
    )


def validate_cloud_handoff_root(data_root: Path, cloud_handoff_root: Path) -> Path:
    """Validate the configured delivery root without probing or modifying any sync client."""
    root = cloud_handoff_root.expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ConfigError("cloud_handoff_root must be an existing absolute directory")
    data_root_resolved = data_root.resolve()
    root_resolved = root.resolve()
    if root_resolved == data_root_resolved or _is_relative_to(root_resolved, data_root_resolved) or _is_relative_to(data_root_resolved, root_resolved):
        raise ConfigError("cloud_handoff_root must be separate from data_root")
    return root_resolved


class CloudHandoffPublisher:
    """Build complete immutable three-file call packages before they enter a sync root."""

    def __init__(
        self, *, data_root: Path, ready_recordings: Path, ready_events: Path, state: StateStore,
        identity: SalespersonIdentity | None, cloud_handoff_root: Path | None,
    ) -> None:
        self.data_root = data_root
        self.ready_recordings = ready_recordings
        self.ready_events = ready_events
        self.state = state
        self.identity = identity
        self.cloud_handoff_root = cloud_handoff_root
        self.stage_root = data_root / "cloud-handoff-stage"

    def publish(self) -> CloudPublishSummary:
        ready_events = sorted(self.ready_events.glob("*.json"))
        if self.cloud_handoff_root is None:
            self._mark_unpublished(ready_events, "CLOUD_HANDOFF_ROOT_UNCONFIGURED")
            return CloudPublishSummary("CLOUD_HANDOFF_ROOT_UNCONFIGURED", blocked=len(ready_events))
        self._validate_handoff_root()
        summary = CloudPublishSummary("CLOUD_HANDOFF_READY")
        recordings = self._recordings_by_id()
        for event_path in ready_events:
            event_id: str | None = None
            try:
                event = _read_json(event_path, "local event")
                validate_event(event)
                event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else None
                if event.get("salesperson_identity_status") != "CONFIGURED":
                    if event_id:
                        self.state.remember_cloud_publication(
                            event_id, relative_path=None, package_fingerprint=None,
                            status="SALESPERSON_IDENTITY_UNCONFIGURED",
                        )
                    summary.blocked += 1
                    continue
                recording = recordings.get(event.get("recording_id"))
                if recording is None:
                    raise CloudHandoffError("LOCAL_RECORDING_FOR_EVENT_ABSENT")
                outcome = self._publish_one(recording, event)
                if outcome == "published":
                    summary.published += 1
                elif outcome == "already_published":
                    summary.already_published += 1
                elif outcome == "published_immutable_enrichment_pending":
                    summary.already_published += 1
                    summary.immutable_enrichment_pending += 1
                else:
                    self.state.remember_cloud_publication(
                        str(event["event_id"]), relative_path=None, package_fingerprint=None, status="conflict",
                    )
                    summary.conflicts += 1
            except CloudHandoffError:
                if isinstance(event_id, str) and re.fullmatch(r"evt_[a-f0-9]{64}", event_id):
                    self.state.remember_cloud_publication(
                        event_id, relative_path=None, package_fingerprint=None, status="publish_failed",
                    )
                summary.conflicts += 1
            except Exception:
                summary.failures += 1
        if summary.blocked and not (
            summary.published or summary.already_published or summary.conflicts or summary.failures
        ):
            summary.status = "SALESPERSON_IDENTITY_UNCONFIGURED"
        return summary

    def _mark_unpublished(self, event_paths: list[Path], status: str) -> None:
        for event_path in event_paths:
            try:
                event = _read_json(event_path, "local event")
                event_id = event.get("event_id")
                if isinstance(event_id, str) and re.fullmatch(r"evt_[a-f0-9]{64}", event_id):
                    self.state.remember_cloud_publication(
                        event_id, relative_path=None, package_fingerprint=None, status=status,
                    )
            except CloudHandoffError:
                continue

    def _validate_handoff_root(self) -> None:
        assert self.cloud_handoff_root is not None
        self.cloud_handoff_root = validate_cloud_handoff_root(self.data_root, self.cloud_handoff_root)

    def _recordings_by_id(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for path in self.ready_recordings.glob("*.json"):
            recording = _read_json(path, "local recording")
            validate_recording(recording)
            media = self.ready_recordings / str(recording.get("media_filename") or "")
            if media.is_file() and sha256_file(media) == recording.get("sha256"):
                result[str(recording["recording_id"])] = recording
        return result

    def _publish_one(self, recording: dict[str, Any], event: dict[str, Any]) -> str:
        assert self.cloud_handoff_root is not None
        cloud_recording, cloud_event, audio_name = project_cloud_package(recording, event)
        identity = SalespersonIdentity(
            salesperson_id=str(cloud_event["salesperson_id"]),
            salesperson_name=str(cloud_event["salesperson_name"]),
        )
        date_folder, call_folder = package_date_and_folder(cloud_event)
        salesperson_directory = self.state.salesperson_directory(identity.salesperson_id) or salesperson_folder_name(identity)
        destination = self.cloud_handoff_root / salesperson_directory / date_folder / call_folder
        if destination.exists():
            return self._existing_package_outcome(
                destination, cloud_recording, cloud_event, salesperson_directory, date_folder, call_folder, identity
            )
        source_media = self.ready_recordings / str(recording["media_filename"])
        if not source_media.is_file() or sha256_file(source_media) != recording["sha256"]:
            raise CloudHandoffError("LOCAL_MEDIA_CHANGED_BEFORE_CLOUD_HANDOFF")
        self.stage_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f"{event['event_id'][:16]}-", dir=self.stage_root))
        try:
            shutil.copyfile(source_media, stage / audio_name)
            _write_json(stage / "recording.json", cloud_recording)
            _write_json(stage / "event.json", cloud_event)
            validation = validate_cloud_package(stage)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._finalize_stage(stage, destination)
            final_validation = validate_cloud_package(destination)
            if final_validation.package_fingerprint != validation.package_fingerprint:
                raise CloudHandoffError("CLOUD_PACKAGE_CHANGED_DURING_FINALIZE")
            relative_path = Path(salesperson_directory, date_folder, call_folder).as_posix()
            self.state.remember_salesperson_directory(identity.salesperson_id, salesperson_directory)
            self.state.remember_cloud_publication(
                final_validation.event_id, relative_path=relative_path,
                package_fingerprint=final_validation.package_fingerprint, status="published",
            )
            return "published"
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def _existing_package_outcome(
        self, destination: Path, expected_recording: dict[str, Any], expected_event: dict[str, Any],
        salesperson_directory: str, date_folder: str, call_folder: str, identity: SalespersonIdentity,
    ) -> str:
        try:
            existing = validate_cloud_package(destination)
        except CloudHandoffError:
            return "conflict"
        if existing.event_id != expected_event["event_id"] or existing.recording_id != expected_recording["recording_id"] or existing.media_sha256 != expected_recording["sha256"]:
            return "conflict"
        expected_fingerprint = _package_fingerprint(expected_recording, expected_event)
        relative_path = Path(salesperson_directory, date_folder, call_folder).as_posix()
        status = "already_published" if existing.package_fingerprint == expected_fingerprint else "published_immutable_enrichment_pending"
        self.state.remember_salesperson_directory(identity.salesperson_id, salesperson_directory)
        self.state.remember_cloud_publication(
            existing.event_id, relative_path=relative_path, package_fingerprint=existing.package_fingerprint, status=status,
        )
        return status

    def _finalize_stage(self, stage: Path, destination: Path) -> None:
        if destination.exists():
            raise CloudHandoffError("CLOUD_PACKAGE_DESTINATION_ALREADY_EXISTS")
        if _same_volume(stage, destination.parent):
            os.replace(stage, destination)
            return
        target_stage = destination.parent / f".{destination.name}.stage-{uuid.uuid4().hex}"
        try:
            shutil.copytree(stage, target_stage)
            validate_cloud_package(target_stage)
            os.replace(target_stage, destination)
        finally:
            if target_stage.exists():
                shutil.rmtree(target_stage, ignore_errors=True)


def inspect_jianguoyun_environment(configured_root: Path | None) -> dict[str, Any]:
    """Use only executable/process and ordinary directory facts; never client internals."""
    user_profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    candidate_bases = (user_profile, user_profile / "Documents", user_profile / "Desktop")
    candidates: list[str] = []
    for base in candidate_bases:
        if not base.is_dir():
            continue
        for item in base.iterdir():
            if item.is_dir() and re.search(r"nutstore|jianguoyun|坚果|同步", item.name, re.IGNORECASE):
                candidates.append(str(item))
    try:
        tasklist = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        client_running = "nutstore" in tasklist.stdout.casefold()
    except (OSError, subprocess.SubprocessError):
        client_running = False
    if configured_root is not None and configured_root.is_dir():
        status = "CLOUD_HANDOFF_ROOT_CONFIGURED"
    elif not candidates:
        status = "JIANGUOYUN_SYNC_ROOT_NOT_RESOLVED"
    elif len(candidates) > 1:
        status = "JIANGUOYUN_SYNC_ROOT_AMBIGUOUS"
    else:
        status = "JIANGUOYUN_SYNC_ROOT_CANDIDATE_UNCONFIRMED"
    return {
        "status": status,
        "nutstore_client_running": client_running,
        "configured_cloud_handoff_root": str(configured_root) if configured_root else None,
        "sync_root_candidates": sorted(set(candidates)),
    }


def _validate_schema(payload: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        raise CloudHandoffError(f"{label} schema invalid: {errors[0].message}")


def _reject_absolute_paths(value: Any) -> None:
    if isinstance(value, str):
        if _ABSOLUTE_PATH.match(value):
            raise CloudHandoffError("CLOUD_PACKAGE_CONTAINS_ABSOLUTE_PATH")
    elif isinstance(value, dict):
        for nested in value.values():
            _reject_absolute_paths(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_absolute_paths(nested)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CloudHandoffError(f"{label} JSON invalid") from exc
    if not isinstance(value, dict):
        raise CloudHandoffError(f"{label} JSON must be an object")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _package_fingerprint(recording: dict[str, Any], event: dict[str, Any]) -> str:
    payload = json.dumps({"recording": recording, "event": event, "sha256": recording["sha256"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _same_volume(left: Path, right: Path) -> bool:
    return os.path.splitdrive(str(left.resolve()))[0].casefold() == os.path.splitdrive(str(right.resolve()))[0].casefold()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
