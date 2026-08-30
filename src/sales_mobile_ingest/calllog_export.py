from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ElementTree
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


MAX_XML_BYTES = 128 * 1024 * 1024
MAX_XML_ELEMENTS = 100_000
SYNCTECH_PROVIDER_ID = "synctech-sms-backup-restore/v1"
EXACT = "EXACT"
HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
AMBIGUOUS = "AMBIGUOUS"
NO_MATCH = "NO_MATCH"

_DIRECTION_BY_TYPE = {
    1: "incoming", 2: "outgoing", 3: "incoming", 4: "unknown",
    5: "incoming", 6: "incoming", 7: "incoming",
}
_DISPOSITION_BY_TYPE = {
    3: "missed", 4: "voicemail", 5: "rejected", 6: "blocked", 7: "answered_externally",
}
_RELIABLE_RECORDING_TIME_SOURCES = {"recorded_at", "filename", "filename_datetime", "calllog_export"}
_EMPTY_IDENTITY_VALUES = {"", "-1", "unknown", "(unknown)", "null", "none"}


@dataclass(frozen=True)
class CallLogExportProvider:
    """A replaceable public-export adapter; service code only consumes this interface."""

    provider_id: str
    directory_names: tuple[str, ...]
    filename_prefixes: tuple[str, ...]
    parser: Callable[[Path, str], list[dict[str, Any]]]

    def parse(self, artifact_path: Path, artifact_sha256: str) -> list[dict[str, Any]]:
        return self.parser(artifact_path, artifact_sha256)


def registered_calllog_export_providers() -> tuple[CallLogExportProvider, ...]:
    return (
        CallLogExportProvider(
            provider_id=SYNCTECH_PROVIDER_ID,
            directory_names=("SMSBackupRestore",),
            filename_prefixes=("calls-",),
            parser=lambda path, sha256: parse_synctech_calllog_export(path, artifact_sha256=sha256),
        ),
    )


class CallLogExportError(ValueError):
    """The local public export is malformed or outside the bounded inspection contract."""


def inspect_xml_schema(path: Path) -> dict[str, Any]:
    """Return names, inferred primitive types and counts, never XML field values."""
    _validate_xml_artifact(path)

    root_name: str | None = None
    root_attributes: dict[str, set[str]] = defaultdict(set)
    element_attributes: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    element_counts: Counter[str] = Counter()
    element_total = 0
    try:
        for event, element in ElementTree.iterparse(path, events=("start", "end")):
            if event == "start" and root_name is None:
                root_name = _local_name(element.tag)
                for name, value in element.attrib.items():
                    root_attributes[_local_name(name)].add(_primitive_type(value))
            if event == "start" and element.tag is not None:
                element_total += 1
                if element_total > MAX_XML_ELEMENTS:
                    raise CallLogExportError(f"call-log export exceeds {MAX_XML_ELEMENTS} element inspection limit")
                name = _local_name(element.tag)
                element_counts[name] += 1
                for attribute_name, value in element.attrib.items():
                    element_attributes[name][_local_name(attribute_name)].add(_primitive_type(value))
            if event == "end" and element.tag is not None:
                element.clear()
    except ElementTree.ParseError as exc:
        raise CallLogExportError("call-log export is not well-formed XML") from exc

    if root_name is None:
        raise CallLogExportError("call-log export has no XML root element")
    return {
        "schema_summary_version": "calllog-export-xml-schema-summary/v1",
        "root_element": root_name,
        "root_attribute_types": _render_types(root_attributes),
        "element_count": element_total,
        "element_tag_counts": dict(sorted(element_counts.items())),
        "element_attribute_types": {
            element_name: _render_types(attributes)
            for element_name, attributes in sorted(element_attributes.items())
        },
    }


def parse_synctech_calllog_export(path: Path, *, artifact_sha256: str) -> list[dict[str, Any]]:
    """Parse only the schema observed on the real SyncTech XML source; raw fields stay local."""
    _validate_xml_artifact(path)
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise CallLogExportError("call-log export is not well-formed XML") from exc
    if _local_name(root.tag) != "calls":
        raise CallLogExportError("unexpected SyncTech export root element")
    rows: list[dict[str, Any]] = []
    for child in list(root):
        if _local_name(child.tag) != "call":
            continue
        attributes = {_local_name(name): value for name, value in child.attrib.items()}
        try:
            date_epoch_ms = int(attributes["date"])
            duration_seconds = float(attributes["duration"])
            call_type = int(attributes["type"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CallLogExportError("SyncTech call row is missing a valid date, duration, or type") from exc
        if date_epoch_ms < 946_684_800_000 or date_epoch_ms > 4_102_444_800_000:
            raise CallLogExportError("SyncTech call row date is not a plausible epoch-milliseconds value")
        if duration_seconds < 0:
            raise CallLogExportError("SyncTech call row duration must not be negative")
        number = _meaningful_identity(attributes.get("number"))
        contact_name = _meaningful_identity(attributes.get("contact_name"))
        subscription_id = _meaningful_identity(attributes.get("subscription_id"))
        subscription_component_name = _meaningful_identity(attributes.get("subscription_component_name"))
        subscription_slot_index = _explicit_nonnegative_int(
            attributes.get("subscription_slot_index") or attributes.get("sim_slot")
        )
        canonical_id = _canonical_call_id(
            date_epoch_ms=date_epoch_ms,
            duration_seconds=duration_seconds,
            call_type=call_type,
            number=number,
            subscription_id=subscription_id,
        )
        rows.append({
            "provider_id": SYNCTECH_PROVIDER_ID,
            "source_artifact_sha256": artifact_sha256,
            "canonical_call_id": canonical_id,
            "date_epoch_ms": date_epoch_ms,
            "occurred_at": datetime.fromtimestamp(date_epoch_ms / 1000, tz=timezone.utc).isoformat(),
            "duration_seconds": duration_seconds,
            "call_type": call_type,
            "call_direction": _DIRECTION_BY_TYPE.get(call_type, "unknown"),
            "call_disposition": _DISPOSITION_BY_TYPE.get(call_type, "unknown"),
            "phone_number_raw": number,
            "contact_name": contact_name,
            "subscription_id": subscription_id,
            "subscription_component_name": subscription_component_name,
            "subscription_slot_index": subscription_slot_index,
        })
    return rows


@dataclass(frozen=True)
class CallLogCorrelation:
    status: str
    candidate_count: int
    time_delta_seconds: float | None = None
    duration_delta_seconds: float | None = None
    time_alignment: str | None = None
    matched_row: dict[str, Any] | None = None
    candidate_rows: tuple[dict[str, Any], ...] = ()

    def safe_summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_count": self.candidate_count,
            "uniqueness": "unique" if self.candidate_count == 1 else ("none" if self.candidate_count == 0 else "multiple"),
            "time_delta_seconds": self.time_delta_seconds,
            "duration_delta_seconds": self.duration_delta_seconds,
            "time_alignment": self.time_alignment,
        }


def correlate_recording_to_calllog(
    *,
    occurred_at: str | None,
    occurred_at_source: str | None,
    recording_duration_seconds: float | int | None,
    rows: list[dict[str, Any]],
    maximum_time_delta_seconds: float = 900,
    maximum_duration_delta_seconds: float = 3,
    local_utc_offset_seconds: float | None = None,
) -> CallLogCorrelation:
    """Correlate one recording to persisted canonical rows without exposing their identities."""
    if not occurred_at or recording_duration_seconds is None:
        return CallLogCorrelation(status=NO_MATCH, candidate_count=0)
    try:
        event_time = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CallLogExportError("recording event occurred_at is not an ISO 8601 timestamp") from exc
    if event_time.tzinfo is None:
        raise CallLogExportError("recording event occurred_at must include a timezone")
    candidates = _matching_candidates(
        reference_time=event_time,
        recording_duration_seconds=float(recording_duration_seconds),
        rows=rows,
        maximum_time_delta_seconds=maximum_time_delta_seconds,
        maximum_duration_delta_seconds=maximum_duration_delta_seconds,
    )
    time_alignment = "direct"
    if not candidates and occurred_at_source == "wpd_modified_at":
        offset_seconds = local_utc_offset_seconds
        if offset_seconds is None:
            local_offset = datetime.fromtimestamp(event_time.timestamp()).astimezone().utcoffset()
            offset_seconds = local_offset.total_seconds() if local_offset else 0
        if offset_seconds:
            # Some MTP implementations expose a local wall-clock modification value as though
            # it were UTC. A recording file's modified time can additionally represent its end.
            candidates = _matching_candidates(
                reference_time=event_time + timedelta(seconds=offset_seconds) - timedelta(seconds=float(recording_duration_seconds)),
                recording_duration_seconds=float(recording_duration_seconds),
                rows=rows,
                maximum_time_delta_seconds=maximum_time_delta_seconds,
                maximum_duration_delta_seconds=maximum_duration_delta_seconds,
            )
            time_alignment = "wpd_modified_at_local_offset_minus_recording_duration"
    if not candidates:
        return CallLogCorrelation(status=NO_MATCH, candidate_count=0)
    if len(candidates) > 1:
        return CallLogCorrelation(
            status=AMBIGUOUS,
            candidate_count=len(candidates),
            time_alignment=time_alignment,
            candidate_rows=tuple(item[2] for item in candidates),
        )
    time_delta, duration_delta, row = candidates[0]
    exact_allowed = occurred_at_source in _RELIABLE_RECORDING_TIME_SOURCES and time_alignment == "direct"
    status = EXACT if exact_allowed and time_delta <= 60 and duration_delta <= 1 else HIGH_CONFIDENCE
    return CallLogCorrelation(
        status=status,
        candidate_count=1,
        time_delta_seconds=round(time_delta, 3),
        duration_delta_seconds=round(duration_delta, 3),
        time_alignment=time_alignment,
        matched_row=row,
        candidate_rows=(row,),
    )


def _matching_candidates(
    *,
    reference_time: datetime,
    recording_duration_seconds: float,
    rows: list[dict[str, Any]],
    maximum_time_delta_seconds: float,
    maximum_duration_delta_seconds: float,
) -> list[tuple[float, float, dict[str, Any]]]:
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for row in rows:
        time_delta = abs((datetime.fromisoformat(str(row["occurred_at"])).timestamp()) - reference_time.timestamp())
        duration_delta = abs(float(row["duration_seconds"]) - recording_duration_seconds)
        if time_delta <= maximum_time_delta_seconds and duration_delta <= maximum_duration_delta_seconds:
            candidates.append((time_delta, duration_delta, row))
    candidates.sort(key=lambda item: (item[1], item[0], str(item[2]["canonical_call_id"])))
    return candidates


def _render_types(values: dict[str, set[str]]) -> dict[str, list[str]]:
    return {name: sorted(types) for name, types in sorted(values.items())}


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _primitive_type(value: str) -> str:
    if value == "":
        return "empty"
    if value.casefold() in {"true", "false"}:
        return "boolean"
    if re.fullmatch(r"[+-]?\d+", value):
        return "integer"
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)", value):
        return "decimal"
    return "string"


def safe_rows_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise parsed rows without timestamps, numbers, names or raw exporter values."""
    return {
        "parsed_row_count": len(rows),
        "direction_counts": dict(sorted(Counter(str(row["call_direction"]) for row in rows).items())),
        "disposition_counts": dict(sorted(Counter(str(row["call_disposition"]) for row in rows).items())),
        "phone_number_present_count": sum(1 for row in rows if row.get("phone_number_raw")),
        "contact_name_present_count": sum(1 for row in rows if row.get("contact_name")),
        "subscription_id_present_count": sum(1 for row in rows if row.get("subscription_id")),
    }


def _validate_xml_artifact(path: Path) -> None:
    if not path.is_file():
        raise CallLogExportError("call-log export file is absent")
    if path.stat().st_size > MAX_XML_BYTES:
        raise CallLogExportError(f"call-log export exceeds {MAX_XML_BYTES} byte inspection limit")
    prefix = path.read_bytes()[:4096].upper()
    if b"<!DOCTYPE" in prefix or b"<!ENTITY" in prefix:
        raise CallLogExportError("call-log export contains a forbidden XML declaration")


def _meaningful_identity(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    return candidate if candidate.casefold() not in _EMPTY_IDENTITY_VALUES else None


def _explicit_nonnegative_int(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"\d+", value.strip()):
        return None
    parsed = int(value)
    return parsed if parsed >= 0 else None


def _canonical_call_id(
    *, date_epoch_ms: int, duration_seconds: float, call_type: int, number: str | None,
    subscription_id: str | None,
) -> str:
    stable = (
        f"{SYNCTECH_PROVIDER_ID}|{date_epoch_ms}|{duration_seconds:.3f}|{call_type}|"
        f"{number or ''}|{subscription_id or ''}"
    )
    return "clg_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()


def synctech_snapshot_metadata(
    path: Path,
    *,
    artifact_sha256: str,
    imported_at: str,
    stale_after_seconds: int = 48 * 60 * 60,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Extract trustworthy root metadata without claiming history completeness."""
    _validate_xml_artifact(path)
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise CallLogExportError("call-log export is not well-formed XML") from exc
    if _local_name(root.tag) != "calls":
        raise CallLogExportError("unexpected SyncTech export root element")
    attributes = {_local_name(name): value for name, value in root.attrib.items()}
    root_count = _explicit_nonnegative_int(attributes.get("count"))
    backup_timestamp = None
    backup_raw = attributes.get("backup_date") or attributes.get("backup_timestamp")
    if backup_raw and re.fullmatch(r"\d+", backup_raw):
        epoch = int(backup_raw)
        if 946_684_800_000 <= epoch <= 4_102_444_800_000:
            backup_timestamp = datetime.fromtimestamp(epoch / 1000, tz=timezone.utc).isoformat()
    imported = datetime.fromisoformat(imported_at.replace("Z", "+00:00"))
    if imported.tzinfo is None:
        raise CallLogExportError("snapshot import timestamp must include a timezone")
    if backup_timestamp is None:
        freshness = "UNKNOWN"
    else:
        age = (imported - datetime.fromisoformat(backup_timestamp)).total_seconds()
        freshness = "UNKNOWN" if age < -300 else ("FRESH" if age <= stale_after_seconds else "STALE")
    mode = _meaningful_identity(attributes.get("backup_type") or attributes.get("type") or attributes.get("mode"))
    snapshot_id = "snp_" + hashlib.sha256(
        f"{SYNCTECH_PROVIDER_ID}|{device_id or ''}|{artifact_sha256}".encode("utf-8")
    ).hexdigest()
    return {
        "snapshot_id": snapshot_id,
        "provider_id": SYNCTECH_PROVIDER_ID,
        "artifact_sha256": artifact_sha256,
        "backup_timestamp": backup_timestamp,
        "imported_at": imported.isoformat(),
        "root_count": root_count,
        "backup_mode": mode,
        "freshness": freshness,
        "parse_status": "PARSED",
        "stale_after_seconds": stale_after_seconds,
    }
