from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_mobile_ingest.adapters import (
    DOC_EVIDENCE_UNAVAILABLE,
    OFFICIAL_DOC_CANDIDATE,
    REAL_DEVICE_VERIFIED,
    adapter_profiles,
    classify_candidate,
)
from sales_mobile_ingest.config import ensure_layout, resolve_data_root
from sales_mobile_ingest.contract import build_metadata, sha256_file, validate_recording
from sales_mobile_ingest.events import (
    EventValidationError,
    build_communication_event,
    normalise_phone_number,
    validate_event,
    write_event_atomically,
)
from sales_mobile_ingest.service import Ingestor


def source_for(path: Path, *, relative_path: str = "Internal shared storage/Recordings/20250115_101530_call.m4a", size: int | None = None) -> dict:
    return {
        "name": path.name,
        "extension": path.suffix.lower(),
        "relative_path": relative_path,
        "size_bytes": len(path.read_bytes()) if size is None else size,
        "modified_at": "2025-01-15T02:15:30+00:00",
        "device_key": "synthetic-oppo",
        "device_name": "OPPO A6 Pro (synthetic)",
        "device_vendor": "OPPO",
        "device_model": "OPPO A6 Pro (synthetic)",
        "adapter": "oppo-v1",
    }


def stage_file(tmp_path: Path, name: str, content: bytes = b"synthetic-call-recording-bytes") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_schema_example_is_valid() -> None:
    example = Path(__file__).parents[1] / "contract" / "examples" / "20250115_101530_8ed3c2f1a4b5.json"
    validate_recording(json.loads(example.read_text(encoding="utf-8")))


def test_communication_event_example_is_valid() -> None:
    example = Path(__file__).parents[1] / "contract" / "examples" / "communication_event.phone_call.example.json"
    validate_event(json.loads(example.read_text(encoding="utf-8")))


def test_nullable_fields_and_stable_content_identity(tmp_path: Path) -> None:
    staged = stage_file(tmp_path, "20250115_101530_call.m4a")
    source = source_for(staged)
    digest = sha256_file(staged)
    metadata = build_metadata(source=source, sha256=digest, media_filename=f"20250115_101530_{digest[:12]}.m4a")
    assert metadata["recording_id"] == f"rec_{digest}"
    assert metadata["phone_number"] is None
    assert metadata["contact_name"] is None
    assert metadata["duration_seconds"] is None
    assert metadata["recorded_at_source"] == "filename_datetime"
    validate_recording(metadata)


def test_standard_filename_and_json_after_media(tmp_path: Path) -> None:
    root = tmp_path / "different-volume-name" / "sales-data"
    ingestor = Ingestor(root)
    staged = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a")
    observed: list[bool] = []

    def writer(path: Path, metadata: dict) -> None:
        observed.append((root / "ready" / "recordings" / metadata["media_filename"]).exists())
        ingestor._write_sidecar_atomically(path, metadata)

    ingestor.sidecar_writer = writer
    assert ingestor.ingest_staged_for_test(staged, source_for(staged)) == "imported"
    ready = root / "ready" / "recordings"
    media = list(ready.glob("*.m4a"))
    sidecars = list(ready.glob("*.json"))
    assert observed == [True]
    assert len(media) == len(sidecars) == 1
    assert media[0].name.startswith("20250115_101530_")
    metadata = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert metadata["media_filename"] == media[0].name
    validate_recording(metadata)


def test_duplicate_bytes_do_not_make_second_ready_object(tmp_path: Path) -> None:
    ingestor = Ingestor(tmp_path / "data")
    first = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a")
    assert ingestor.ingest_staged_for_test(first, source_for(first)) == "imported"
    second = stage_file(ingestor.paths["stage"], "20250115_101531_call.m4a")
    assert ingestor.ingest_staged_for_test(second, source_for(second, relative_path="Internal shared storage/Recordings/20250115_101531_call.m4a")) == "duplicate"
    ready = ingestor.paths["ready"]
    assert len(list(ready.glob("*.m4a"))) == 1
    assert len(list(ready.glob("*.json"))) == 1


def test_partial_file_cannot_become_ready_and_retry_recovers(tmp_path: Path) -> None:
    ingestor = Ingestor(tmp_path / "data")
    partial = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a", b"short")
    assert ingestor.ingest_staged_for_test(partial, source_for(partial, size=99)) == "failed"
    assert not list(ingestor.paths["ready"].iterdir())
    assert list(ingestor.paths["failed"].glob("*.failure.json"))
    repaired = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a", b"correct-bytes")
    assert ingestor.ingest_staged_for_test(repaired, source_for(repaired)) == "imported"
    assert len(list(ingestor.paths["ready"].glob("*.json"))) == 1


def test_configured_data_root_is_not_drive_letter_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chosen = tmp_path / "external-archive" / "sales"
    monkeypatch.setenv("SALES_MOBILE_INGEST_DATA_ROOT", str(chosen))
    assert resolve_data_root() == chosen
    layout = ensure_layout(chosen)
    assert layout["ready"] == chosen / "ready" / "recordings"


def test_oppo_candidate_needs_name_plus_content_but_generic_needs_call_evidence() -> None:
    files = [{"name": "20250115_101530.m4a", "extension": ".m4a"}]
    oppo = classify_candidate(device_name="OPPO A6 Pro", relative_path="Internal/Recordings", files=files)
    assert oppo.accepted and oppo.adapter == "oppo-v1"
    generic = classify_candidate(device_name="Pixel", relative_path="Internal/Recordings", files=files)
    assert not generic.accepted
    call = classify_candidate(device_name="Pixel", relative_path="Internal/Call Recordings", files=files)
    assert call.accepted and call.adapter == "generic-call-recording-v1"


def test_generic_adapter_rejects_music_like_path() -> None:
    decision = classify_candidate(
        device_name="Pixel",
        relative_path="Internal/Music/Recordings",
        files=[{"name": "20250115_101530.m4a", "extension": ".m4a"}],
    )
    assert not decision.accepted
    assert "music_like_path_rejected" in decision.evidence


def test_oppo_wpd_file_can_use_system_file_extension_when_shell_name_has_no_dot() -> None:
    decision = classify_candidate(
        device_name="OPPO A6 Pro 5G",
        relative_path="Internal shared storage/Music/Recordings/Call Recordings",
        files=[{"name": "opaque-shell-name.mp3", "extension": ".mp3"}],
    )
    assert decision.accepted
    assert decision.adapter == "oppo-v1"


def test_one_shot_limit_prevents_first_connection_from_copying_every_candidate(tmp_path: Path) -> None:
    fixture = stage_file(tmp_path, "20250115_101530_fixture.mp3", b"first")

    class FakeBridge:
        def probe(self, cached_dirs: list[dict[str, str]], search_depth: int = 3) -> dict:
            return {
                "devices": [{
                    "device_key": "fake-oppo",
                    "display_name": "OPPO fixture",
                    "storage_roots": ["Internal"],
                    "search_depth": search_depth,
                    "candidates": [{
                        "relative_path": "Internal/Music/Recordings/Call Recordings",
                        "files": [
                            {"name": "20250115_101530_fixture.mp3", "extension": ".mp3", "relative_path": "Internal/Music/Recordings/Call Recordings/a", "size_bytes": 5, "modified_at": None},
                            {"name": "20250115_101531_fixture.mp3", "extension": ".mp3", "relative_path": "Internal/Music/Recordings/Call Recordings/b", "size_bytes": 6, "modified_at": None},
                        ],
                    }],
                }]
            }

        def copy_to_staging(self, source: dict, destination_dir: Path) -> Path:
            target = destination_dir / source["name"]
            target.write_bytes(fixture.read_bytes() if source["name"].endswith("1530_fixture.mp3") else b"second")
            return target

    ingestor = Ingestor(tmp_path / "data", bridge=FakeBridge())
    summary = ingestor.ingest_once(limit=1)
    assert summary.new_imports == 1
    assert summary.source_attempts == 1
    assert len(list(ingestor.paths["ready"].glob("*.json"))) == 1


def test_vendor_profiles_are_explicit_about_evidence_and_path_certification() -> None:
    profiles = {profile.vendor: profile for profile in adapter_profiles()}
    assert profiles["OPPO"].evidence_status == REAL_DEVICE_VERIFIED
    assert profiles["OPPO"].validation_device == "OPPO A6 Pro 5G"
    assert profiles["Xiaomi"].evidence_status == OFFICIAL_DOC_CANDIDATE
    assert profiles["Huawei"].evidence_status == OFFICIAL_DOC_CANDIDATE
    assert profiles["Honor"].evidence_status == OFFICIAL_DOC_CANDIDATE
    assert profiles["vivo"].evidence_status == DOC_EVIDENCE_UNAVAILABLE


def test_official_document_candidate_is_accepted_without_being_claimed_verified() -> None:
    decision = classify_candidate(
        device_name="Xiaomi synthetic",
        relative_path="Internal/MIUI/sound_recorder/call_rec",
        files=[{"name": "20250115_101530.m4a", "extension": ".m4a"}],
    )
    assert decision.accepted
    assert decision.adapter == "xiaomi-miui-hyperos-v1"
    assert decision.adapter_evidence_status == OFFICIAL_DOC_CANDIDATE


def test_vendor_without_documented_path_can_use_generic_explicit_call_fallback() -> None:
    decision = classify_candidate(
        device_name="vivo synthetic",
        relative_path="Internal/Call Recordings",
        files=[{"name": "opaque.mp3", "extension": ".mp3"}],
    )
    assert decision.accepted
    assert decision.adapter == "generic-call-recording-v1"


def test_phone_normalisation_is_conservative() -> None:
    assert normalise_phone_number(" +86 (155) 0000-1234 ") == "+8615500001234"
    assert normalise_phone_number("155 0000 1234") == "15500001234"
    assert normalise_phone_number("call-15500001234") is None
    assert normalise_phone_number(None) is None


def test_event_is_deterministic_relative_and_created_after_recording_pair(tmp_path: Path) -> None:
    ingestor = Ingestor(tmp_path / "data")
    staged = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a")
    assert ingestor.ingest_staged_for_test(staged, source_for(staged)) == "imported"
    events = list(ingestor.paths["events"].glob("*.json"))
    assert len(events) == 1
    event = json.loads(events[0].read_text(encoding="utf-8"))
    validate_event(event)
    assert event["media_ref"].startswith("ready/recordings/")
    assert not Path(event["media_ref"]).is_absolute()
    assert event["salesperson_id"] is None
    assert event["salesperson_identity_status"] == "UNCONFIGURED"
    recording = json.loads(next(ingestor.paths["ready"].glob("*.json")).read_text(encoding="utf-8"))
    rebuilt = build_communication_event(recording=recording, installation_id=event["installation_id"], salesperson_id=None)
    assert rebuilt["event_id"] == event["event_id"]
    assert ingestor._reconcile_events()["created"] == 0
    assert len(list(ingestor.paths["events"].glob("*.json"))) == 1


def test_event_writer_refuses_commit_without_complete_recording_pair(tmp_path: Path) -> None:
    media = stage_file(tmp_path, "audio.m4a")
    recording = build_metadata(
        source=source_for(media), sha256=sha256_file(media), media_filename=media.name,
        imported_at="2025-01-15T02:15:30+00:00",
    )
    event = build_communication_event(recording=recording, installation_id="ins_00000000-0000-0000-0000-000000000000", salesperson_id=None)
    with pytest.raises(EventValidationError):
        write_event_atomically(tmp_path / "event.json", event, media, tmp_path / "recording.json")


def test_probe_report_redacts_raw_filename_and_phone_like_content(tmp_path: Path) -> None:
    class FakeBridge:
        def probe(self, cached_dirs: list[dict[str, str]], search_depth: int = 3) -> dict:
            return {"devices": [{
                "device_key": "secret-serial-never-report", "display_name": "OPPO fixture", "storage_roots": ["Internal"],
                "candidates": [{"relative_path": "Internal/Music/Recordings/Call Recordings", "files": [{
                    "name": "Sensitive-15500001234-20250115_101530.mp3", "extension": ".mp3", "relative_path": "private/file", "size_bytes": 9,
                    "modified_at": "2025-01-15T02:15:30+00:00", "duration_seconds": 9.0,
                }]}],
            }]}

    ingestor = Ingestor(tmp_path / "data", bridge=FakeBridge())
    report_path = ingestor.save_probe_report()
    report_text = report_path.read_text(encoding="utf-8")
    assert "Sensitive-15500001234" not in report_text
    assert "secret-serial" not in report_text
    report = json.loads(report_text)
    assert report["devices"][0]["candidates"][0]["filename_structure_patterns"][0]["extension"] == ".mp3"


def test_legacy_recording_without_additive_duration_source_still_valid(tmp_path: Path) -> None:
    staged = stage_file(tmp_path, "20250115_101530_call.m4a")
    metadata = build_metadata(source=source_for(staged), sha256=sha256_file(staged), media_filename=staged.name)
    metadata.pop("duration_source")
    validate_recording(metadata)
