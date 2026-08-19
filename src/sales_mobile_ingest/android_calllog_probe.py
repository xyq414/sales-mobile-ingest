from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_NAME = "com.salesmobileingest.calllogprobe"
READ_CALL_LOG_PERMISSION = "android.permission.READ_CALL_LOG"
PROBE_SCHEMA_VERSION = "android-calllog-probe-result/v1"
MAX_PROBE_ROWS = 20

EXACT = "EXACT"
HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
AMBIGUOUS = "AMBIGUOUS"
NO_MATCH = "NO_MATCH"
BLOCKED = "BLOCKED"

_DIRECTION_BY_TYPE = {
    1: "incoming",
    2: "outgoing",
    3: "missed",
    4: "voicemail",
    5: "rejected",
    6: "blocked",
    7: "answered_externally",
}
_RELIABLE_RECORDING_TIME_SOURCES = {"recorded_at", "filename_datetime"}


class ProbeResultError(ValueError):
    """The local app-private result is malformed or exceeds the probe boundary."""


def mask_phone_number(value: str | None) -> str | None:
    """Return a stable display mask without emitting a full subscriber number."""
    if value is None or not str(value).strip():
        return None
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}****{digits[-4:]}"


def call_direction(call_type: int | None) -> str:
    return _DIRECTION_BY_TYPE.get(call_type, "unknown")


def parse_probe_result(value: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Validate the private app result while deliberately retaining raw fields in memory only."""
    if isinstance(value, (str, bytes)):
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProbeResultError("probe result is not valid JSON") from exc
    else:
        payload = value
    if not isinstance(payload, dict):
        raise ProbeResultError("probe result must be an object")
    if payload.get("schema_version") != PROBE_SCHEMA_VERSION:
        raise ProbeResultError("unexpected probe schema version")
    if payload.get("package_name") != PACKAGE_NAME:
        raise ProbeResultError("unexpected probe package name")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_PROBE_ROWS:
        raise ProbeResultError("probe rows must be a bounded list")
    for row in rows:
        if not isinstance(row, dict):
            raise ProbeResultError("probe row must be an object")
        if not isinstance(row.get("date_epoch_ms"), int):
            raise ProbeResultError("probe row date_epoch_ms must be an integer")
        if not isinstance(row.get("duration_seconds"), (int, float)):
            raise ProbeResultError("probe row duration_seconds must be numeric")
        if not isinstance(row.get("type"), int):
            raise ProbeResultError("probe row type must be an integer")
    return payload


def correlation_for_recording(
    *,
    occurred_at: str,
    occurred_at_source: str | None,
    recording_duration_seconds: float | int,
    rows: list[dict[str, Any]],
    maximum_time_delta_seconds: float = 900,
    maximum_duration_delta_seconds: float = 3,
) -> dict[str, Any]:
    """Correlate only a pre-bounded provider result, conservatively and without exposing raw numbers."""
    event_time = _parse_event_time(occurred_at)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        row_time = float(row["date_epoch_ms"]) / 1000
        time_delta = abs(row_time - event_time.timestamp())
        duration_delta = abs(float(row["duration_seconds"]) - float(recording_duration_seconds))
        if time_delta <= maximum_time_delta_seconds and duration_delta <= maximum_duration_delta_seconds:
            candidates.append({
                "row": row,
                "time_delta_seconds": round(time_delta, 3),
                "duration_delta_seconds": round(duration_delta, 3),
            })
    candidates.sort(key=lambda item: (item["duration_delta_seconds"], item["time_delta_seconds"]))
    if not candidates:
        return _correlation_result(status=NO_MATCH, candidate_count=0)
    if len(candidates) > 1:
        return _correlation_result(status=AMBIGUOUS, candidate_count=len(candidates))

    candidate = candidates[0]
    exact_allowed = occurred_at_source in _RELIABLE_RECORDING_TIME_SOURCES
    status = HIGH_CONFIDENCE
    if exact_allowed and candidate["time_delta_seconds"] <= 60 and candidate["duration_delta_seconds"] <= 1:
        status = EXACT
    return _correlation_result(
        status=status,
        candidate_count=1,
        row=candidate["row"],
        time_delta_seconds=candidate["time_delta_seconds"],
        duration_delta_seconds=candidate["duration_delta_seconds"],
    )


def safe_probe_summary(probe_result: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Create an output-safe summary; raw CallLog values must remain outside Git and normal logs."""
    payload = parse_probe_result(probe_result)
    rows = payload["rows"]
    query_status = str(payload.get("query_status") or "UNKNOWN")
    if query_status != "PASS":
        correlation = _correlation_result(status=BLOCKED, candidate_count=0, blocked_at="call_log_provider")
    else:
        correlation = correlation_for_recording(
            occurred_at=str(event["occurred_at"]),
            occurred_at_source=event.get("occurred_at_source"),
            recording_duration_seconds=float(event["duration_seconds"]),
            rows=rows,
        )
    return {
        "schema_version": "android-calllog-probe-safe-summary/v1",
        "package_name": PACKAGE_NAME,
        "probe_timestamp": payload.get("probe_timestamp"),
        "android": {
            "api_level": payload.get("api_level"),
            "manufacturer": payload.get("manufacturer"),
            "model": payload.get("model"),
        },
        "permission_status": payload.get("permission_status"),
        "query_status": query_status,
        "query_exception_class": payload.get("query_exception_class"),
        "candidate_row_count": len(rows),
        "rows": [
            {
                "date_epoch_ms": row["date_epoch_ms"],
                "duration_seconds": row["duration_seconds"],
                "direction": call_direction(row["type"]),
                "phone_number_masked": mask_phone_number(row.get("number")),
                "cached_name_present": bool(row.get("cached_name")),
            }
            for row in rows
        ],
        "recording_correlation": correlation,
    }


def summarize_private_result(raw_result_path: Path, event_path: Path, safe_output_path: Path) -> dict[str, Any]:
    """Read only app-private evidence and write a separately safe local report atomically."""
    payload = parse_probe_result(raw_result_path.read_text(encoding="utf-8"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    summary = safe_probe_summary(payload, event)
    safe_output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_output_path.with_suffix(safe_output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(safe_output_path)
    return summary


def _parse_event_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProbeResultError("recording occurred_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ProbeResultError("recording occurred_at must include a timezone")
    return parsed


def _correlation_result(
    *,
    status: str,
    candidate_count: int,
    row: dict[str, Any] | None = None,
    time_delta_seconds: float | None = None,
    duration_delta_seconds: float | None = None,
    blocked_at: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "candidate_count": candidate_count,
        "uniqueness": "unique" if candidate_count == 1 else ("none" if candidate_count == 0 else "multiple"),
        "time_delta_seconds": time_delta_seconds,
        "duration_delta_seconds": duration_delta_seconds,
        "phone_number_masked": mask_phone_number(row.get("number")) if row else None,
        "direction": call_direction(row.get("type")) if row else None,
        "blocked_at": blocked_at,
    }
