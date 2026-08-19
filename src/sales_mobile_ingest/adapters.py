from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


AUDIO_EXTENSIONS = {".m4a", ".amr", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".3gp", ".opus"}
_CANDIDATE_NAMES = {
    "recordings",
    "recorder",
    "record",
    "call recordings",
    "callrecord",
    "call recording",
    "通话录音",
    "录音",
}
_CALL_RE = re.compile(r"(?:call|phone|通话|电话)", re.IGNORECASE)
_DATE_RE = re.compile(r"(?:19|20)\d{2}[-_]?\d{2}[-_]?\d{2}[ _-]?\d{2}[-_]?\d{2}[-_]?\d{2}")
_NEGATIVE_PATH_RE = re.compile(r"(?:music|ringtones?|notifications?|podcasts?)", re.IGNORECASE)


@dataclass(frozen=True)
class CandidateDecision:
    accepted: bool
    adapter: str | None
    score: int
    evidence: list[str]


def device_identity(device_name: str | None) -> tuple[str | None, str | None]:
    name = (device_name or "").strip()
    if not name:
        return None, None
    if "oppo" in name.lower():
        return "OPPO", name
    return None, name


def classify_candidate(
    *, device_name: str | None, relative_path: str, files: list[dict[str, Any]]
) -> CandidateDecision:
    """Classify only an already-bounded candidate listing, never an entire phone."""
    directory_name = relative_path.replace("\\", "/").rstrip("/").split("/")[-1].strip()
    lower_name = directory_name.casefold()
    audio = [f for f in files if str(f.get("extension", "")).lower() in AUDIO_EXTENSIONS]
    if not audio:
        return CandidateDecision(False, None, 0, ["no_supported_audio"])

    evidence: list[str] = [f"audio_files:{len(audio)}"]
    score = 1
    explicit_call_directory = bool(_CALL_RE.search(directory_name))
    filename_signals = sum(
        bool(_CALL_RE.search(str(f.get("name", ""))) or _DATE_RE.search(str(f.get("name", ""))))
        for f in audio
    )
    is_named_candidate = lower_name in {name.casefold() for name in _CANDIDATE_NAMES}
    if explicit_call_directory:
        score += 4
        evidence.append("explicit_call_directory")
    elif is_named_candidate:
        score += 2
        evidence.append("candidate_directory_name")
    if filename_signals:
        score += 2
        evidence.append(f"call_or_datetime_filename:{filename_signals}")
    if _NEGATIVE_PATH_RE.search(relative_path) and not explicit_call_directory:
        score -= 8
        evidence.append("music_like_path_rejected")
        return CandidateDecision(False, None, score, evidence)
    if _NEGATIVE_PATH_RE.search(relative_path) and explicit_call_directory:
        evidence.append("explicit_call_directory_overrides_music_parent")

    vendor, _ = device_identity(device_name)
    if explicit_call_directory:
        return CandidateDecision(True, "oppo-v1" if vendor == "OPPO" else "generic-call-recording-v1", score, evidence)
    if vendor == "OPPO" and is_named_candidate and filename_signals:
        evidence.append("oppo_v1_name_plus_content")
        return CandidateDecision(True, "oppo-v1", score, evidence)
    evidence.append("insufficient_call_recording_evidence")
    return CandidateDecision(False, None, score, evidence)
