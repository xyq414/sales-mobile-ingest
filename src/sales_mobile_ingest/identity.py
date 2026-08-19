from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .events import normalise_phone_number


DIRECT_RECORDING_PHONE_ID_AVAILABLE = "DIRECT_RECORDING_PHONE_ID_AVAILABLE"
DIRECT_RECORDING_PHONE_ID_NOT_FOUND = "DIRECT_RECORDING_PHONE_ID_NOT_FOUND_IN_CURRENT_SAMPLE"
DIRECT_RECORDING_PHONE_ID_CONFLICT = "DIRECT_RECORDING_PHONE_ID_CONFLICT"
CALL_LOG_NOT_EXPOSED = "CALL_LOG_NOT_EXPOSED_VIA_CURRENT_MTP_WPD"
CALL_LOG_EXPOSED = "CALL_LOG_EXPOSED_VIA_CURRENT_MTP_WPD"
CALL_LOG_PROBE_UNAVAILABLE = "CALL_LOG_MTP_WPD_PROBE_UNAVAILABLE"

_DATETIME_TOKEN = re.compile(r"(?:19|20)\d{2}[-_]?\d{2}[-_]?\d{2}[ _-]?\d{2}[-_]?\d{2}[-_]?\d{2}")
_CHINA_MOBILE = re.compile(r"(?<!\d)(?:\+?86[-_. ]?)?(1[3-9](?:[-_. ]?\d){9})(?!\d)")
_E164 = re.compile(r"(?<![+\d])(\+[1-9](?:[-_. ]?\d){6,14})(?!\d)")
_LABELLED_PHONE = re.compile(r"(?:phone|tel|mobile|number)[\s:_-]*(\+?[0-9][0-9()\- .]{5,20})", re.IGNORECASE)
_IDENTITY_PROPERTY_NAMES = {
    "system.title", "system.subject", "system.comment", "system.author", "system.music.artist",
}
_CALL_LOG_NAMES = ("call log", "calllog", "call history", "callhistory", "通话记录")
_CONTACT_NAMES = ("contacts", "contact", "联系人")
_BACKUP_EXPORT_NAMES = ("backup", "export", "备份", "导出")


def filename_structure(filename: str) -> dict[str, Any]:
    suffix = Path(filename).suffix
    stem = filename[:-len(suffix)] if suffix else filename
    match = _DATETIME_TOKEN.search(stem)
    return {
        "basename_length": len(filename),
        "extension": suffix.lower(),
        "digit_run_lengths": [len(item.group(0)) for item in re.finditer(r"\d+", stem)],
        "separators": sorted(set(item for item in stem if item in "-_ .()")),
        "datetime_token_start": match.start() if match else None,
    }


def phone_candidate_details(text: str | None) -> list[dict[str, str]]:
    """Return conservative raw/normalized pairs; caller keeps raw evidence local."""
    if not text:
        return []
    raw_matches: list[str] = []
    raw_matches.extend(match.group(1) for match in _LABELLED_PHONE.finditer(text))
    raw_matches.extend(match.group(0) for match in _CHINA_MOBILE.finditer(text))
    raw_matches.extend(match.group(1) for match in _E164.finditer(text))
    result: list[dict[str, str]] = []
    for raw in raw_matches:
        normalized = normalise_phone_number(raw)
        if normalized and not any(item["normalized"] == normalized for item in result):
            result.append({"raw": raw, "normalized": normalized})
    return result


def phone_candidates(text: str | None) -> list[str]:
    return [item["normalized"] for item in phone_candidate_details(text)]


def mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}****{digits[-4:]}"


def resolve_direct_phone(candidates_by_source: dict[str, Iterable[str | dict[str, str]]]) -> dict[str, Any]:
    normalized_sources: dict[str, list[str]] = {}
    raw_by_normalized: dict[str, str] = {}
    for source, candidates in candidates_by_source.items():
        normalized_sources[source] = []
        for candidate in candidates:
            if isinstance(candidate, dict):
                normalized = candidate.get("normalized")
                raw = candidate.get("raw")
            else:
                normalized = candidate
                raw = candidate
            if normalized and normalized not in normalized_sources[source]:
                normalized_sources[source].append(normalized)
                raw_by_normalized.setdefault(normalized, raw or normalized)
    values = sorted({candidate for candidates in normalized_sources.values() for candidate in candidates})
    if not values:
        return {
            "status": DIRECT_RECORDING_PHONE_ID_NOT_FOUND,
            "phone_number_raw": None,
            "phone_number_normalized": None,
            "phone_number_source": "unknown",
            "phone_number_confidence": "unknown",
            "evidence_level": "NONE",
            "source_candidate_counts": {source: len(candidates) for source, candidates in normalized_sources.items()},
        }
    if len(values) > 1:
        return {
            "status": DIRECT_RECORDING_PHONE_ID_CONFLICT,
            "phone_number_raw": None,
            "phone_number_normalized": None,
            "phone_number_source": "unknown",
            "phone_number_confidence": "unknown",
            "evidence_level": "CONFLICT",
            "source_candidate_counts": {source: len(candidates) for source, candidates in normalized_sources.items()},
            "masked_candidates": [mask_phone(value) for value in values],
        }
    value = values[0]
    sources = [source for source, candidates in normalized_sources.items() if value in candidates]
    source = next((item for item in ("wpd_metadata", "audio_metadata", "filename") if item in sources), sources[0])
    return {
        "status": DIRECT_RECORDING_PHONE_ID_AVAILABLE,
        "phone_number_raw": raw_by_normalized.get(value, value),
        "phone_number_normalized": value,
        "phone_number_source": source,
        "phone_number_confidence": "low",
        "evidence_level": "REAL_SAMPLE_PROVISIONAL",
        "corroborated_sources": sources,
        "source_candidate_counts": {source_name: len(candidates) for source_name, candidates in normalized_sources.items()},
        "masked_phone": mask_phone(value),
    }


def read_mp3_id3(path: Path, *, maximum_tag_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    """Read a bounded ID3v2 tag without changing the media file or adding a dependency."""
    with path.open("rb") as handle:
        header = handle.read(10)
        if len(header) != 10 or header[:3] != b"ID3":
            return {"format": "none", "tags": {}}
        version = header[3]
        size = _syncsafe_int(header[6:10])
        if size > maximum_tag_bytes:
            return {"format": f"id3v2.{version}", "tags": {}, "tag_too_large": True}
        payload = handle.read(size)
    tags: dict[str, list[str]] = {}
    offset = 0
    frame_header_size = 6 if version == 2 else 10
    while offset + frame_header_size <= len(payload):
        frame_id_bytes = payload[offset:offset + (3 if version == 2 else 4)]
        if not frame_id_bytes.strip(b"\x00"):
            break
        try:
            frame_id = frame_id_bytes.decode("ascii")
        except UnicodeDecodeError:
            break
        if version == 2:
            frame_size = int.from_bytes(payload[offset + 3:offset + 6], "big")
        else:
            size_bytes = payload[offset + 4:offset + 8]
            frame_size = _syncsafe_int(size_bytes) if version == 4 else int.from_bytes(size_bytes, "big")
        if frame_size < 0 or offset + frame_header_size + frame_size > len(payload):
            break
        frame = payload[offset + frame_header_size:offset + frame_header_size + frame_size]
        value = _decode_id3_text(frame)
        if value:
            tags.setdefault(frame_id, []).append(value)
        offset += frame_header_size + frame_size
    return {"format": f"id3v2.{version}", "tags": tags}


def _syncsafe_int(value: bytes) -> int:
    if len(value) != 4:
        return 0
    return (value[0] << 21) | (value[1] << 14) | (value[2] << 7) | value[3]


def _decode_id3_text(frame: bytes) -> str | None:
    if not frame:
        return None
    encoding = frame[0]
    payload = frame[1:]
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(encoding)
    if codec is None:
        return None
    try:
        return payload.decode(codec, errors="replace").replace("\x00", " ").strip() or None
    except UnicodeDecodeError:
        return None


def audio_tag_phone_candidate_details(audio_metadata: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for values in (audio_metadata.get("tags") or {}).values():
        for value in values:
            for candidate in phone_candidate_details(value):
                if not any(item["normalized"] == candidate["normalized"] for item in candidates):
                    candidates.append(candidate)
    return candidates


def audio_tag_phone_candidates(audio_metadata: dict[str, Any]) -> list[str]:
    return [item["normalized"] for item in audio_tag_phone_candidate_details(audio_metadata)]


def wpd_phone_candidate_details(inspect_response: dict[str, Any] | None) -> list[dict[str, str]]:
    if not inspect_response:
        return []
    values: list[str] = []
    source = inspect_response.get("source") or {}
    for key, value in (source.get("properties") or {}).items():
        if key.casefold() in _IDENTITY_PROPERTY_NAMES and isinstance(value, str):
            values.append(value)
    for column in source.get("shell_columns") or []:
        label = str(column.get("label") or "").casefold()
        if any(token in label for token in ("title", "subject", "comment", "author", "artist")):
            values.append(str(column.get("value") or ""))
    candidates: list[dict[str, str]] = []
    for value in values:
        for candidate in phone_candidate_details(value):
            if not any(item["normalized"] == candidate["normalized"] for item in candidates):
                candidates.append(candidate)
    return candidates


def wpd_phone_candidates(inspect_response: dict[str, Any] | None) -> list[str]:
    return [item["normalized"] for item in wpd_phone_candidate_details(inspect_response)]


def safe_wpd_summary(inspect_response: dict[str, Any] | None) -> dict[str, Any]:
    if not inspect_response:
        return {"available": False, "property_names_with_values": [], "shell_column_count": 0}
    source = inspect_response.get("source") or {}
    properties = source.get("properties") or {}
    adjacent = inspect_response.get("adjacent_objects") or []
    extension_counts = Counter(str(item.get("extension") or "").lower() for item in adjacent if not item.get("is_folder"))
    return {
        "available": True,
        "property_names_with_values": sorted(key for key, value in properties.items() if value not in (None, "")),
        "shell_column_count": len(source.get("shell_columns") or []),
        "adjacent_object_count": len(adjacent),
        "adjacent_folder_count": sum(bool(item.get("is_folder")) for item in adjacent),
        "adjacent_extension_distribution": dict(sorted(extension_counts.items())),
    }


def classify_call_log_exposure(capabilities: dict[str, Any] | None) -> dict[str, Any]:
    if not capabilities:
        return {"status": CALL_LOG_PROBE_UNAVAILABLE, "storage_count": 0, "call_history_object_exposed": False, "contact_object_exposed": False}
    names = [
        str(child.get("name") or "").casefold()
        for device in capabilities.get("devices") or []
        for storage in device.get("storage") or []
        for child in storage.get("direct_children") or []
    ]
    has_call_history = any(any(token in name for token in _CALL_LOG_NAMES) for name in names)
    has_contacts = any(any(token in name for token in _CONTACT_NAMES) for name in names)
    has_backup_or_export = any(any(token in name for token in _BACKUP_EXPORT_NAMES) for name in names)
    storage_count = sum(len(device.get("storage") or []) for device in capabilities.get("devices") or [])
    return {
        "status": CALL_LOG_EXPOSED if has_call_history else CALL_LOG_NOT_EXPOSED,
        "storage_count": storage_count,
        "call_history_object_exposed": has_call_history,
        "contact_object_exposed": has_contacts,
        "backup_or_export_object_exposed": has_backup_or_export,
        "visible_first_level_object_count": len(names),
    }
