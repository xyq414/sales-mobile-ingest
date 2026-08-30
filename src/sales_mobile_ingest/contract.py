from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .resources import resource_path

SCHEMA_PATH = resource_path("contract", "recording.schema.json")
_FILENAME_TIME = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_]?([01]\d)[-_]?([0-3]\d)[ _-]([0-2]\d)[-_]?([0-5]\d)[-_]?([0-5]\d)(?!\d)")


class ContractValidationError(ValueError):
    pass


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_recording(metadata: dict[str, Any]) -> None:
    validator = Draft202012Validator(schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(metadata), key=lambda item: list(item.path))
    if errors:
        formatted = "; ".join(error.message for error in errors[:5])
        raise ContractValidationError(formatted)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recorded_time_from_evidence(filename: str, source_modified_at: str | None, imported_at: str) -> tuple[str, str]:
    match = _FILENAME_TIME.search(filename)
    if match:
        try:
            naive = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4)), int(match.group(5)), int(match.group(6)),
            )
            local_tz = datetime.now().astimezone().tzinfo
            return naive.replace(tzinfo=local_tz).isoformat(timespec="seconds"), "filename_datetime"
        except ValueError:
            pass
    if source_modified_at:
        try:
            parsed = datetime.fromisoformat(source_modified_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat(timespec="seconds"), "source_modified_at"
        except ValueError:
            pass
    return imported_at, "imported_at"


def build_metadata(
    *, source: dict[str, Any], sha256: str, media_filename: str, imported_at: str | None = None
) -> dict[str, Any]:
    imported_at = imported_at or iso_now()
    original_filename = str(source["name"])
    extension = Path(original_filename).suffix.lower()
    recorded_at, recorded_at_source = recorded_time_from_evidence(
        original_filename, source.get("modified_at"), imported_at
    )
    return {
        "schema_version": "recording-contract/v1",
        "recording_id": f"rec_{sha256}",
        "source_type": "phone_call_recording",
        "device_vendor": source.get("device_vendor"),
        "device_model": source.get("device_model"),
        "source_relative_path": str(source["relative_path"]).replace("\\", "/"),
        "original_filename": original_filename,
        "original_extension": extension,
        "source_size_bytes": int(source["size_bytes"]),
        "source_modified_at": source.get("modified_at"),
        "recorded_at": recorded_at,
        "recorded_at_source": recorded_at_source,
        "phone_number": source.get("phone_number"),
        "phone_number_source": source.get("phone_number_source", "unknown"),
        "phone_number_confidence": source.get("phone_number_confidence", "unknown"),
        "contact_name": source.get("contact_name"),
        "contact_name_source": source.get("contact_name_source", "unknown"),
        "call_direction": source.get("call_direction", "unknown"),
        "call_direction_source": source.get("call_direction_source", "unknown"),
        "duration_seconds": source.get("duration_seconds"),
        "duration_source": source.get("duration_source", "wpd" if source.get("duration_seconds") is not None else "unknown"),
        "sha256": sha256,
        "media_filename": media_filename,
        "imported_at": imported_at,
        "ingest_adapter": source["adapter"],
        "ingest_status": "ready",
    }
