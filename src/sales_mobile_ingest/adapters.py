from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


AUDIO_EXTENSIONS = {".m4a", ".amr", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".3gp", ".opus"}

# These are bounded directory *candidates*, not claims that a phone has call
# recordings at a particular location. The bridge never recursively enumerates
# every file on a device.
_CANDIDATE_NAMES = {
    "recordings", "recorder", "record", "call recordings", "callrecord", "call recording",
    "call_rec", "sound_recorder", "sounds", "通话录音", "录音",
}
_CALL_RE = re.compile(r"(?:call|phone|通话|电话)", re.IGNORECASE)
_DATE_RE = re.compile(r"(?:19|20)\d{2}[-_]?\d{2}[-_]?\d{2}[ _-]?\d{2}[-_]?\d{2}[-_]?\d{2}")
_NEGATIVE_PATH_RE = re.compile(r"(?:music|ringtones?|notifications?|podcasts?)", re.IGNORECASE)

REAL_DEVICE_VERIFIED = "REAL_DEVICE_VERIFIED"
OFFICIAL_DOC_CANDIDATE = "OFFICIAL_DOC_CANDIDATE"
DOC_EVIDENCE_UNAVAILABLE = "DOC_EVIDENCE_UNAVAILABLE"
GENERIC_HEURISTIC = "GENERIC_HEURISTIC"


@dataclass(frozen=True)
class AdapterProfile:
    key: str
    vendor: str
    os_family: str
    evidence_status: str
    candidate_paths: tuple[str, ...]
    filename_parser_status: str
    validation_device: str | None
    official_sources: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class CandidateDecision:
    accepted: bool
    adapter: str | None
    adapter_evidence_status: str
    score: int
    evidence: list[str]


# Official source lookup date: 2026-08-19. A candidate is deliberately not
# upgraded to a validated adapter until a physical device passes the same MTP
# probe/ingest evidence as OPPO below.
ADAPTER_PROFILES: tuple[AdapterProfile, ...] = (
    AdapterProfile(
        key="oppo-v1", vendor="OPPO", os_family="ColorOS", evidence_status=REAL_DEVICE_VERIFIED,
        candidate_paths=("Music/Recordings/Call Recordings",), filename_parser_status="REAL_SAMPLE_PROVISIONAL",
        validation_device="OPPO A6 Pro 5G", official_sources=(),
        notes="One Windows MTP real-device import and duplicate pass verified; filename semantics are not generalized.",
    ),
    AdapterProfile(
        key="xiaomi-miui-hyperos-v1", vendor="Xiaomi", os_family="MIUI / HyperOS", evidence_status=OFFICIAL_DOC_CANDIDATE,
        candidate_paths=("MIUI/sound_recorder/call_rec", "MIUI/Sound_recorder/Call_rec"), filename_parser_status="UNVERIFIED",
        validation_device=None,
        official_sources=(
            "https://www.mi.com/uk/support/faq/details/KA-541461/",
            "https://www.mi.com/sg/support/article/KA-07847/",
        ),
        notes="Official path candidate only; no physical device or MTP copy test yet.",
    ),
    AdapterProfile(
        key="huawei-emui-harmonyos-v1", vendor="Huawei", os_family="EMUI / HarmonyOS", evidence_status=OFFICIAL_DOC_CANDIDATE,
        candidate_paths=("Sounds/CallReCord", "Sounds/CallRecord", "Sounds/record"), filename_parser_status="UNVERIFIED",
        validation_device=None,
        official_sources=("https://consumer.huawei.com/cn/support/content/zh-cn00739449/",),
        notes="Official path candidate only; casing differs by published material and requires device certification.",
    ),
    AdapterProfile(
        key="honor-magicos-v1", vendor="Honor", os_family="MagicOS / Magic UI", evidence_status=OFFICIAL_DOC_CANDIDATE,
        candidate_paths=("Sounds/CallRecord",), filename_parser_status="UNVERIFIED", validation_device=None,
        official_sources=("https://www.honor.com/cn/support/content/zh-cn15869200/",),
        notes="Official path candidate only; no physical device or MTP copy test yet.",
    ),
    AdapterProfile(
        key="vivo-originos-funtouch-v1", vendor="vivo", os_family="OriginOS / Funtouch OS", evidence_status=DOC_EVIDENCE_UNAVAILABLE,
        candidate_paths=(), filename_parser_status="UNVERIFIED", validation_device=None,
        official_sources=("https://www.vivo.com/en/support/questionList?categoryId=53700",),
        notes="Official FAQ exposes a call-recording-file question but not a stable path in its available material; no path is guessed.",
    ),
    AdapterProfile(
        key="generic-call-recording-v1", vendor="Generic Android", os_family="Android", evidence_status=GENERIC_HEURISTIC,
        candidate_paths=(), filename_parser_status="UNVERIFIED", validation_device=None, official_sources=(),
        notes="Requires explicit call-directory evidence and audio content; never describes a vendor as verified.",
    ),
)


def adapter_profiles() -> tuple[AdapterProfile, ...]:
    return ADAPTER_PROFILES


def device_identity(device_name: str | None) -> tuple[str | None, str | None]:
    name = (device_name or "").strip()
    if not name:
        return None, None
    lowered = name.casefold()
    if "oppo" in lowered:
        return "OPPO", name
    if any(token in lowered for token in ("xiaomi", "redmi", "poco", " mi ")):
        return "Xiaomi", name
    if "huawei" in lowered:
        return "Huawei", name
    if "honor" in lowered:
        return "Honor", name
    if "vivo" in lowered:
        return "vivo", name
    return None, name


def profile_for_device(device_name: str | None) -> AdapterProfile:
    vendor, _ = device_identity(device_name)
    for profile in ADAPTER_PROFILES:
        if profile.vendor == vendor:
            return profile
    return next(profile for profile in ADAPTER_PROFILES if profile.key == "generic-call-recording-v1")


def profile_for_adapter(adapter_key: str | None) -> AdapterProfile:
    for profile in ADAPTER_PROFILES:
        if profile.key == adapter_key:
            return profile
    return next(profile for profile in ADAPTER_PROFILES if profile.key == "generic-call-recording-v1")


def _normalise_path(value: str) -> str:
    return value.replace("\\", "/").strip("/").casefold()


def _path_matches_profile(relative_path: str, profile: AdapterProfile) -> bool:
    candidate = _normalise_path(relative_path)
    return any(candidate.endswith(_normalise_path(hint)) for hint in profile.candidate_paths)


def classify_candidate(
    *, device_name: str | None, relative_path: str, files: list[dict[str, Any]]
) -> CandidateDecision:
    """Classify an already-bounded candidate listing, never an entire phone."""
    directory_name = relative_path.replace("\\", "/").rstrip("/").split("/")[-1].strip()
    lower_name = directory_name.casefold()
    audio = [item for item in files if str(item.get("extension", "")).lower() in AUDIO_EXTENSIONS]
    profile = profile_for_device(device_name)
    if not audio:
        return CandidateDecision(False, None, profile.evidence_status, 0, ["no_supported_audio"])

    evidence: list[str] = [f"audio_files:{len(audio)}"]
    score = 1
    explicit_call_directory = bool(_CALL_RE.search(directory_name)) or lower_name in {"call_rec", "callrecord"}
    filename_signals = sum(
        bool(_CALL_RE.search(str(item.get("name", ""))) or _DATE_RE.search(str(item.get("name", ""))))
        for item in audio
    )
    is_named_candidate = lower_name in _CANDIDATE_NAMES
    profile_path = _path_matches_profile(relative_path, profile)
    if explicit_call_directory:
        score += 4
        evidence.append("explicit_call_directory")
    elif is_named_candidate:
        score += 2
        evidence.append("candidate_directory_name")
    if filename_signals:
        score += 2
        evidence.append(f"call_or_datetime_filename:{filename_signals}")
    if profile_path:
        score += 5
        evidence.append("vendor_documented_path_candidate")
    if _NEGATIVE_PATH_RE.search(relative_path) and not (explicit_call_directory or profile_path):
        score -= 8
        evidence.append("music_like_path_rejected")
        return CandidateDecision(False, None, profile.evidence_status, score, evidence)
    if _NEGATIVE_PATH_RE.search(relative_path) and (explicit_call_directory or profile_path):
        evidence.append("explicit_call_directory_overrides_music_parent")

    if profile.key == "oppo-v1" and (profile_path or (is_named_candidate and filename_signals)):
        return CandidateDecision(True, profile.key, profile.evidence_status, score, evidence)
    if profile.evidence_status == OFFICIAL_DOC_CANDIDATE and profile_path:
        evidence.append("official_document_candidate_requires_real_device_certification")
        return CandidateDecision(True, profile.key, profile.evidence_status, score, evidence)
    if explicit_call_directory:
        generic = profile_for_adapter("generic-call-recording-v1")
        evidence.append("generic_fallback_for_explicit_call_directory")
        return CandidateDecision(True, generic.key, generic.evidence_status, score, evidence)
    evidence.append("insufficient_call_recording_evidence")
    return CandidateDecision(False, None, profile.evidence_status, score, evidence)
