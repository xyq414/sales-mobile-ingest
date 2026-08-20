from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .adapters import profile_for_adapter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "contract" / "communication_event.schema.json"
_PHONE_STRIP = re.compile(r"[\s()\-./]+")


class EventValidationError(ValueError):
    pass


def event_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_event(event: dict[str, Any]) -> None:
    validator = Draft202012Validator(event_schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(event), key=lambda item: list(item.path))
    if errors:
        raise EventValidationError("; ".join(error.message for error in errors[:5]))


def normalise_phone_number(value: str | None) -> str | None:
    """Only remove cosmetic separators; never infer a country code or a number."""
    if not value:
        return None
    compact = _PHONE_STRIP.sub("", value.strip())
    if compact.startswith("+"):
        digits = compact[1:]
        return f"+{digits}" if digits.isdigit() and digits else None
    return compact if compact.isdigit() and compact else None


def _occurred_provenance(recording: dict[str, Any]) -> tuple[str, str]:
    source = recording.get("recorded_at_source")
    if source == "filename_datetime":
        return "filename", "high"
    if source == "source_modified_at":
        return "wpd_modified_at", "medium"
    if source == "imported_at":
        return "import_time", "low"
    return "unknown", "unknown"


def build_communication_event(
    *, recording: dict[str, Any], installation_id: str, salesperson_id: str | None,
    salesperson_name: str | None = None,
) -> dict[str, Any]:
    recording_id = recording.get("recording_id")
    if not isinstance(recording_id, str):
        raise EventValidationError("recording_id is required to build a phone_call event")
    source_type = "phone_call"
    event_id = "evt_" + hashlib.sha256(f"communication-event/v1|{source_type}|{recording_id}".encode("utf-8")).hexdigest()
    occurred_source, occurred_confidence = _occurred_provenance(recording)
    adapter = str(recording.get("ingest_adapter") or "generic-call-recording-v1")
    profile = profile_for_adapter(adapter)
    raw_phone = recording.get("phone_number") if isinstance(recording.get("phone_number"), str) else None
    phone_source = recording.get("phone_number_source") if recording.get("phone_number_source") in {
        "filename", "wpd_metadata", "audio_metadata", "manual"
    } else "unknown"
    phone_confidence = recording.get("phone_number_confidence") if recording.get("phone_number_confidence") in {
        "high", "medium", "low"
    } else "unknown"
    configured_identity = bool(salesperson_id and salesperson_id.strip() and salesperson_name and salesperson_name.strip())
    return {
        "schema_version": "communication-event/v1",
        "event_id": event_id,
        "source_type": source_type,
        "recording_id": recording_id,
        "occurred_at": recording.get("recorded_at"),
        "occurred_at_source": occurred_source,
        "occurred_at_confidence": occurred_confidence,
        "salesperson_id": salesperson_id.strip() if configured_identity else None,
        "salesperson_name": salesperson_name.strip() if configured_identity else None,
        "salesperson_identity_status": "CONFIGURED" if configured_identity else "UNCONFIGURED",
        "installation_id": installation_id,
        "device_vendor": recording.get("device_vendor"),
        "device_model": recording.get("device_model"),
        "phone_number_raw": raw_phone,
        "phone_number_normalized": normalise_phone_number(raw_phone),
        "phone_number_source": phone_source,
        "phone_number_confidence": phone_confidence,
        "contact_name": recording.get("contact_name"),
        "contact_name_source": recording.get("contact_name_source") if recording.get("contact_name_source") in {
            "filename", "wpd_metadata", "audio_metadata", "manual"
        } else "unknown",
        "call_direction": recording.get("call_direction") or "unknown",
        "call_direction_source": recording.get("call_direction_source") if recording.get("call_direction_source") in {
            "filename", "wpd_metadata", "manual"
        } else "unknown",
        "duration_seconds": recording.get("duration_seconds"),
        "duration_source": recording.get("duration_source") or "unknown",
        "media_ref": f"ready/recordings/{recording['media_filename']}",
        "media_sha256": recording["sha256"],
        "original_extension": recording["original_extension"],
        "ingested_at": recording["imported_at"],
        "adapter": adapter,
        "adapter_evidence_status": profile.evidence_status,
    }


def event_path(events_dir: Path, event: dict[str, Any]) -> Path:
    return events_dir / f"{event['event_id']}.json"


def write_event_atomically(path: Path, event: dict[str, Any], media_path: Path, recording_path: Path) -> bool:
    """Publish an event only after its media and recording-sidecar commit exist."""
    validate_event(event)
    if not media_path.is_file() or not recording_path.is_file():
        raise EventValidationError("Refusing event commit before complete recording pair exists")
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        validate_event(existing)
        if existing["event_id"] != event["event_id"]:
            raise EventValidationError("event path collision")
        return False
    descriptor, temporary_name = tempfile.mkstemp(prefix=".event-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(event, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not media_path.is_file() or not recording_path.is_file():
            raise EventValidationError("Recording pair disappeared before event commit")
        os.replace(temporary_name, path)
        return True
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def replace_event_atomically(path: Path, event: dict[str, Any], media_path: Path, recording_path: Path) -> None:
    """Atomically replace one existing event after controlled identity enrichment."""
    validate_event(event)
    if not path.is_file() or not media_path.is_file() or not recording_path.is_file():
        raise EventValidationError("Refusing event enrichment without a complete ready recording pair")
    existing = json.loads(path.read_text(encoding="utf-8"))
    validate_event(existing)
    if existing["event_id"] != event["event_id"]:
        raise EventValidationError("Refusing enrichment that changes event_id")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".event-enrichment-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(event, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not media_path.is_file() or not recording_path.is_file():
            raise EventValidationError("Recording pair disappeared before event enrichment commit")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
