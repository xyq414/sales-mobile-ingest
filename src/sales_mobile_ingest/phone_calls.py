from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .contract import iso_now
from .events import normalise_phone_number


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHONE_CALL_SCHEMA_PATH = PROJECT_ROOT / "contract" / "phone_call.schema.json"
LINK_SCHEMA_PATH = PROJECT_ROOT / "contract" / "call_recording_link.schema.json"


class PhoneCallValidationError(ValueError):
    pass


def _validate(value: dict[str, Any], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        raise PhoneCallValidationError("; ".join(error.message for error in errors[:5]))


def validate_phone_call(value: dict[str, Any]) -> None:
    _validate(value, PHONE_CALL_SCHEMA_PATH)


def validate_call_recording_link(value: dict[str, Any]) -> None:
    _validate(value, LINK_SCHEMA_PATH)


def phone_call_id(*, provider_id: str, device_id: str, source_row_id: str) -> str:
    stable = f"phone-call/v1|{provider_id}|{device_id}|{source_row_id}"
    return "pc_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()


def build_phone_call(
    *,
    row: dict[str, Any],
    device_id: str,
    snapshot: dict[str, Any],
    attribution: dict[str, Any],
    existing: dict[str, Any] | None = None,
    ingested_at: str | None = None,
) -> dict[str, Any]:
    ingested_at = ingested_at or iso_now()
    call_id = phone_call_id(
        provider_id=str(row["provider_id"]), device_id=device_id, source_row_id=str(row["canonical_call_id"])
    )
    if existing is not None and existing.get("call_id") != call_id:
        raise PhoneCallValidationError("refusing PhoneCall enrichment that changes call_id")
    raw_phone = row.get("phone_number_raw") if isinstance(row.get("phone_number_raw"), str) else None
    contact = row.get("contact_name") if isinstance(row.get("contact_name"), str) else None
    subscription_present = any(row.get(key) is not None for key in (
        "subscription_id", "subscription_component_name", "subscription_slot_index"
    ))
    value = {
        "schema_version": "phone-call/v1",
        "call_id": call_id,
        "device_id": device_id,
        "device_vendor": row.get("device_vendor"),
        "device_model": row.get("device_model"),
        "source_provider": row["provider_id"],
        "source_row_id": row["canonical_call_id"],
        "source_artifact_sha256": row["source_artifact_sha256"],
        "source_artifact_sha256s": sorted(set(
            (existing.get("source_artifact_sha256s", []) if existing else []) + [row["source_artifact_sha256"]]
        )),
        "source_snapshot_id": snapshot["snapshot_id"],
        "source_snapshot_ids": sorted(set(
            (existing.get("source_snapshot_ids", []) if existing else []) + [snapshot["snapshot_id"]]
        )),
        "snapshot": {
            "backup_timestamp": snapshot.get("backup_timestamp"),
            "imported_at": snapshot["imported_at"],
            "root_count": snapshot.get("root_count"),
            "backup_mode": snapshot.get("backup_mode"),
            "freshness": snapshot["freshness"],
            "parse_status": snapshot["parse_status"],
            "stale_after_seconds": snapshot["stale_after_seconds"],
        },
        "occurred_at": row["occurred_at"],
        "occurred_at_source": "calllog_export",
        "occurred_at_confidence": "high",
        "duration_seconds": row.get("duration_seconds"),
        "duration_source": "calllog_export" if row.get("duration_seconds") is not None else "unknown",
        "phone_number_raw": raw_phone,
        "phone_number_normalized": normalise_phone_number(raw_phone),
        "phone_number_source": "calllog_export" if raw_phone else "unknown",
        "contact_name": contact,
        "contact_name_source": "calllog_export" if contact else "unknown",
        "direction": row.get("call_direction") or "unknown",
        "direction_source": "calllog_export",
        "disposition": row.get("call_disposition") or "unknown",
        "disposition_source": "calllog_export",
        "raw_call_type": row.get("call_type"),
        "subscription": {
            "subscription_id": row.get("subscription_id"),
            "component_name": row.get("subscription_component_name"),
            "slot_index": row.get("subscription_slot_index"),
            "source": "calllog_export" if subscription_present else "unknown",
        },
        **attribution,
        "created_at": existing.get("created_at") if existing else ingested_at,
        "ingested_at": existing.get("ingested_at") if existing else ingested_at,
        "last_enriched_at": ingested_at,
    }
    if existing:
        # A later snapshot may add display data, but absence never erases earlier evidence.
        for field in ("phone_number_raw", "phone_number_normalized", "contact_name"):
            if value[field] is None and existing.get(field) is not None:
                value[field] = existing[field]
        for field in ("phone_number_source", "contact_name_source"):
            if value[field] == "unknown" and existing.get(field) != "unknown":
                value[field] = existing[field]
    validate_phone_call(value)
    return value


def build_call_recording_link(
    *,
    recording_id: str,
    device_id: str | None,
    status: str,
    candidate_call_ids: list[str],
    provider_ids: list[str],
    call_id: str | None,
    time_delta_seconds: float | None,
    duration_delta_seconds: float | None,
    time_alignment: str | None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    link_id = "lnk_" + hashlib.sha256(f"call-recording-link/v1|{recording_id}".encode("utf-8")).hexdigest()
    now = iso_now()
    value = {
        "schema_version": "call-recording-link/v1",
        "link_id": link_id,
        "recording_id": recording_id,
        "call_id": call_id,
        "device_id": device_id,
        "status": status,
        "candidate_call_ids": sorted(set(candidate_call_ids)),
        "provider_ids": sorted(set(provider_ids)),
        "time_delta_seconds": time_delta_seconds,
        "duration_delta_seconds": duration_delta_seconds,
        "time_alignment": time_alignment,
        "evidence_source": "calllog_recording_reconciliation",
        "created_at": existing.get("created_at") if existing else now,
        "last_evaluated_at": now,
    }
    validate_call_recording_link(value)
    return value


def write_contract_atomically(path: Path, value: dict[str, Any], *, kind: str) -> bool:
    if kind == "phone_call":
        validate_phone_call(value)
    elif kind == "link":
        validate_call_recording_link(value)
    else:
        raise ValueError("unknown contract kind")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        comparable_existing = {**existing, "last_enriched_at": value.get("last_enriched_at")} if kind == "phone_call" else {
            **existing, "last_evaluated_at": value.get("last_evaluated_at")
        }
        if comparable_existing == value:
            return False
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{kind}-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        return True
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
