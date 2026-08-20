from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from sales_mobile_ingest.adapters import (
    DOC_EVIDENCE_UNAVAILABLE,
    OFFICIAL_DOC_CANDIDATE,
    REAL_DEVICE_VERIFIED,
    adapter_profiles,
    classify_candidate,
)
from sales_mobile_ingest.android_calllog_probe import (
    AMBIGUOUS,
    EXACT,
    HIGH_CONFIDENCE,
    MAX_PROBE_ROWS,
    NO_MATCH,
    PACKAGE_NAME,
    PROBE_SCHEMA_VERSION,
    ProbeResultError,
    correlation_for_recording,
    mask_phone_number,
    parse_probe_result,
    safe_probe_summary,
)
from sales_mobile_ingest.config import ensure_layout, resolve_data_root
from sales_mobile_ingest.calllog_export import (
    AMBIGUOUS as EXPORT_AMBIGUOUS,
    EXACT as EXPORT_EXACT,
    HIGH_CONFIDENCE as EXPORT_HIGH_CONFIDENCE,
    NO_MATCH as EXPORT_NO_MATCH,
    CallLogExportError,
    correlate_recording_to_calllog,
    inspect_xml_schema,
    parse_synctech_calllog_export,
    registered_calllog_export_providers,
    safe_rows_summary,
)
from sales_mobile_ingest.contract import build_metadata, sha256_file, validate_recording
from sales_mobile_ingest.events import (
    EventValidationError,
    build_communication_event,
    normalise_phone_number,
    replace_event_atomically,
    validate_event,
    write_event_atomically,
)
from sales_mobile_ingest.identity import (
    CALL_LOG_EXPOSED,
    CALL_LOG_NOT_EXPOSED,
    DIRECT_RECORDING_PHONE_ID_CONFLICT,
    DIRECT_RECORDING_PHONE_ID_NOT_FOUND,
    audio_tag_phone_candidate_details,
    classify_call_log_exposure,
    filename_structure,
    phone_candidate_details,
    resolve_direct_phone,
    safe_wpd_summary,
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


def _probe_payload(rows: list[dict] | None = None) -> dict:
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "package_name": PACKAGE_NAME,
        "probe_timestamp": "2026-08-18T05:34:00Z",
        "api_level": 36,
        "manufacturer": "OPPO",
        "model": "synthetic model",
        "permission_status": "GRANTED",
        "query_status": "PASS",
        "query_exception_class": None,
        "rows": rows if rows is not None else [],
    }


def _call_row(*, time_ms: int = 1_755_495_221_000, duration: int = 190, number: str = "13812345678", call_type: int = 2) -> dict:
    return {
        "date_epoch_ms": time_ms,
        "duration_seconds": duration,
        "number": number,
        "type": call_type,
        "cached_name": None,
    }


def test_android_probe_manifest_is_minimal_and_package_is_fixed() -> None:
    root = Path(__file__).parents[1] / "android" / "calllog-probe"
    manifest = (root / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    module = (root / "app" / "build.gradle").read_text(encoding="utf-8")
    assert "android.permission.READ_CALL_LOG" in manifest
    assert "android.permission.INTERNET" not in manifest
    assert "WRITE_CALL_LOG" not in manifest
    assert "READ_CONTACTS" not in manifest
    assert f"applicationId '{PACKAGE_NAME}'" in module
    assert "targetSdk 36" in module


def test_android_probe_result_parser_requires_a_bounded_known_payload() -> None:
    payload = _probe_payload([_call_row()])
    assert parse_probe_result(payload)["rows"][0]["type"] == 2
    with pytest.raises(ProbeResultError):
        parse_probe_result(_probe_payload([_call_row() for _ in range(MAX_PROBE_ROWS + 1)]))
    with pytest.raises(ProbeResultError):
        parse_probe_result({"schema_version": "wrong", "package_name": PACKAGE_NAME, "rows": []})


def test_phone_masking_and_safe_summary_do_not_expose_synthetic_number() -> None:
    assert mask_phone_number("13812345678") == "138****5678"
    assert mask_phone_number("12") == "***"
    event = {
        "occurred_at": "2025-08-18T13:33:41+08:00",
        "occurred_at_source": "wpd_modified_at",
        "duration_seconds": 190.028,
    }
    summary = safe_probe_summary(_probe_payload([_call_row()]), event)
    rendered = json.dumps(summary, ensure_ascii=False)
    assert "13812345678" not in rendered
    assert "138****5678" in rendered
    assert summary["rows"][0]["direction"] == "outgoing"


def test_recording_calllog_correlation_is_conservative_about_time_provenance() -> None:
    rows = [_call_row()]
    wpd_result = correlation_for_recording(
        occurred_at="2025-08-18T13:33:41+08:00",
        occurred_at_source="wpd_modified_at",
        recording_duration_seconds=190.028,
        rows=rows,
    )
    assert wpd_result["status"] == HIGH_CONFIDENCE
    assert wpd_result["candidate_count"] == 1
    exact_result = correlation_for_recording(
        occurred_at="2025-08-18T13:33:41+08:00",
        occurred_at_source="recorded_at",
        recording_duration_seconds=190.028,
        rows=rows,
    )
    assert exact_result["status"] == EXACT
    ambiguous = correlation_for_recording(
        occurred_at="2025-08-18T13:33:41+08:00",
        occurred_at_source="wpd_modified_at",
        recording_duration_seconds=190.028,
        rows=[_call_row(), _call_row(number="13999990000", call_type=1)],
    )
    assert ambiguous["status"] == AMBIGUOUS
    no_match = correlation_for_recording(
        occurred_at="2025-08-18T13:33:41+08:00",
        occurred_at_source="wpd_modified_at",
        recording_duration_seconds=190.028,
        rows=[_call_row(duration=120)],
    )
    assert no_match["status"] == NO_MATCH


def _synctech_xml(rows: list[dict[str, str]]) -> str:
    elements = "".join(
        "<call " + " ".join(f'{key}="{value}"' for key, value in row.items()) + " />"
        for row in rows
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><calls count="{len(rows)}">{elements}</calls>'


def test_synctech_xml_schema_and_parser_keep_values_out_of_safe_summary(tmp_path: Path) -> None:
    xml_path = tmp_path / "calls-synthetic.xml"
    xml_path.write_text(_synctech_xml([{
        "date": "1736907330000", "duration": "190", "type": "2", "number": "13812345678", "contact_name": "Synthetic Contact",
    }]), encoding="utf-8")
    schema = inspect_xml_schema(xml_path)
    assert schema["root_element"] == "calls"
    assert schema["element_count"] == 2
    assert schema["element_tag_counts"] == {"call": 1, "calls": 1}
    assert schema["element_attribute_types"]["call"]["date"] == ["integer"]
    rows = parse_synctech_calllog_export(xml_path, artifact_sha256="a" * 64)
    assert rows[0]["call_direction"] == "outgoing"
    assert rows[0]["canonical_call_id"].startswith("clg_")
    rendered = json.dumps(safe_rows_summary(rows), ensure_ascii=False)
    assert "13812345678" not in rendered
    assert "Synthetic Contact" not in rendered


def test_calllog_export_provider_registry_separates_discovery_from_business_flow() -> None:
    providers = registered_calllog_export_providers()
    assert len(providers) == 1
    assert providers[0].provider_id == "synctech-sms-backup-restore/v1"
    assert providers[0].directory_names == ("SMSBackupRestore",)
    assert providers[0].filename_prefixes == ("calls-",)


def test_synctech_parser_rejects_malformed_rows_and_accepts_no_calls(tmp_path: Path) -> None:
    malformed = tmp_path / "calls-malformed.xml"
    malformed.write_text('<calls><call date="1736907330000" type="2" /></calls>', encoding="utf-8")
    with pytest.raises(CallLogExportError):
        parse_synctech_calllog_export(malformed, artifact_sha256="b" * 64)
    empty = tmp_path / "calls-empty.xml"
    empty.write_text('<calls count="0" />', encoding="utf-8")
    assert parse_synctech_calllog_export(empty, artifact_sha256="c" * 64) == []


def test_synctech_correlation_is_unique_and_conservative(tmp_path: Path) -> None:
    xml_path = tmp_path / "calls-multiple.xml"
    xml_path.write_text(_synctech_xml([
        {"date": "1736907330000", "duration": "190", "type": "2", "number": "13812345678"},
        {"date": "1736907331000", "duration": "190", "type": "1", "number": "13999990000"},
    ]), encoding="utf-8")
    rows = parse_synctech_calllog_export(xml_path, artifact_sha256="d" * 64)
    high = correlate_recording_to_calllog(
        occurred_at="2025-01-15T02:15:30+00:00", occurred_at_source="wpd_modified_at", recording_duration_seconds=190, rows=[rows[0]],
    )
    assert high.status == EXPORT_HIGH_CONFIDENCE
    exact = correlate_recording_to_calllog(
        occurred_at="2025-01-15T02:15:30+00:00", occurred_at_source="filename", recording_duration_seconds=190, rows=[rows[0]],
    )
    assert exact.status == EXPORT_EXACT
    ambiguous = correlate_recording_to_calllog(
        occurred_at="2025-01-15T02:15:30+00:00", occurred_at_source="wpd_modified_at", recording_duration_seconds=190, rows=rows,
    )
    assert ambiguous.status == EXPORT_AMBIGUOUS
    no_match = correlate_recording_to_calllog(
        occurred_at="2025-01-15T02:15:30+00:00", occurred_at_source="wpd_modified_at", recording_duration_seconds=120, rows=rows,
    )
    assert no_match.status == EXPORT_NO_MATCH


def test_wpd_modified_time_can_only_use_explicit_local_offset_end_alignment(tmp_path: Path) -> None:
    event_time = datetime.fromisoformat("2025-01-15T02:05:20+00:00").timestamp()
    row_time_ms = int((event_time + 28_800 - 190) * 1000)
    xml_path = tmp_path / "calls-timezone.xml"
    xml_path.write_text(_synctech_xml([{
        "date": str(row_time_ms), "duration": "190", "type": "2", "number": "13812345678",
    }]), encoding="utf-8")
    row = parse_synctech_calllog_export(xml_path, artifact_sha256="e" * 64)[0]
    result = correlate_recording_to_calllog(
        occurred_at="2025-01-15T02:05:20+00:00",
        occurred_at_source="wpd_modified_at",
        recording_duration_seconds=190,
        rows=[row],
        local_utc_offset_seconds=28_800,
    )
    assert result.status == EXPORT_HIGH_CONFIDENCE
    assert result.time_alignment == "wpd_modified_at_local_offset_minus_recording_duration"
    assert result.time_delta_seconds == 0


def test_calllog_export_ingest_enriches_once_and_skips_duplicate_snapshot(tmp_path: Path) -> None:
    xml_path = tmp_path / "calls-synthetic.xml"
    xml_path.write_text(_synctech_xml([{
        "date": "1736907330000", "duration": "190", "type": "2", "number": "13812345678", "contact_name": "Synthetic Contact",
    }]), encoding="utf-8")

    class FakeBridge:
        def discover_calllog_exports(self, **_: object) -> dict:
            return {
                "devices": [{
                    "device_key": "synthetic-oppo",
                    "display_name": "OPPO A6 Pro (synthetic)",
                    "directories": [{
                        "relative_path": "Internal shared storage/SMSBackupRestore",
                        "files": [{
                            "name": "calls-synthetic.xml",
                            "extension": ".xml",
                            "relative_path": "Internal shared storage/SMSBackupRestore/calls-synthetic.xml",
                            "size_bytes": xml_path.stat().st_size,
                            "modified_at": "2025-01-15T02:15:30+00:00",
                        }],
                    }],
                }],
            }

        def copy_to_staging(self, _source: dict, destination_dir: Path) -> Path:
            target = destination_dir / "calls-synthetic.xml"
            shutil.copyfile(xml_path, target)
            return target

    ingestor = Ingestor(tmp_path / "data", bridge=FakeBridge())
    staged = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a")
    recording_source = source_for(staged)
    recording_source["duration_seconds"] = 190
    assert ingestor.ingest_staged_for_test(staged, recording_source) == "imported"
    first = ingestor.ingest_calllog_exports()
    assert first.new_artifacts == first.canonical_rows_new == first.events_enriched == 1
    event_path = next(ingestor.paths["events"].glob("*.json"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["phone_number_raw"] == "13812345678"
    assert event["phone_number_source"] == "calllog_export"
    assert event["call_direction"] == "outgoing"
    second = ingestor.ingest_calllog_exports()
    assert second.duplicate_artifacts == second.canonical_rows_duplicate == second.events_already_enriched == 1
    assert len(list(ingestor.paths["events"].glob("*.json"))) == 1


def test_calllog_row_persists_for_a_late_arriving_recording(tmp_path: Path) -> None:
    xml_path = tmp_path / "calls-late.xml"
    xml_path.write_text(_synctech_xml([{
        "date": "1736907330000", "duration": "190", "type": "1", "number": "13812345678",
    }]), encoding="utf-8")

    class FakeBridge:
        def discover_calllog_exports(self, **_: object) -> dict:
            return {"devices": [{
                "device_key": "synthetic-oppo",
                "display_name": "OPPO A6 Pro (synthetic)",
                "directories": [{"relative_path": "Internal/SMSBackupRestore", "files": [{
                    "name": "calls-late.xml", "extension": ".xml", "relative_path": "Internal/SMSBackupRestore/calls-late.xml",
                    "size_bytes": xml_path.stat().st_size, "modified_at": "2025-01-15T02:15:30+00:00",
                }]}],
            }]}

        def copy_to_staging(self, _source: dict, destination_dir: Path) -> Path:
            target = destination_dir / "calls-late.xml"
            shutil.copyfile(xml_path, target)
            return target

    ingestor = Ingestor(tmp_path / "data", bridge=FakeBridge())
    assert ingestor.ingest_calllog_exports().canonical_rows_new == 1
    assert not list(ingestor.paths["events"].glob("*.json"))
    staged = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a")
    source = source_for(staged)
    source["duration_seconds"] = 190
    assert ingestor.ingest_staged_for_test(staged, source) == "imported"
    late = ingestor.ingest_calllog_exports()
    assert late.canonical_rows_duplicate == 1
    assert late.events_enriched == 1


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

        def discover_calllog_exports(self, **_: object) -> dict:
            return {"devices": []}

    ingestor = Ingestor(tmp_path / "data", bridge=FakeBridge())
    summary = ingestor.ingest_once(limit=1)
    assert summary.new_imports == 1
    assert summary.source_attempts == 1
    assert summary.calllog_failures == 0
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


def test_event_is_deterministic_relative_and_created_after_recording_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Unit fixtures must not inherit a developer machine's ignored business identity.
    monkeypatch.setattr("sales_mobile_ingest.service.resolve_salesperson_identity", lambda: None)
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


def test_filename_phone_candidates_are_conservative_and_keep_raw_display_value() -> None:
    candidates = phone_candidate_details("call-138 0013 8000-20250115.mp3")
    assert candidates == [{"raw": "138 0013 8000", "normalized": "13800138000"}]
    assert phone_candidate_details("recording-20250115-190.mp3") == []
    assert filename_structure("call-138 0013 8000-20250115.mp3")["datetime_token_start"] is None


def test_direct_identity_is_unresolved_without_evidence_and_conflict_is_not_silently_chosen() -> None:
    none = resolve_direct_phone({"filename": [], "wpd_metadata": [], "audio_metadata": []})
    assert none["status"] == DIRECT_RECORDING_PHONE_ID_NOT_FOUND
    conflict = resolve_direct_phone({
        "filename": [{"raw": "13800138000", "normalized": "13800138000"}],
        "wpd_metadata": [{"raw": "13900139000", "normalized": "13900139000"}],
    })
    assert conflict["status"] == DIRECT_RECORDING_PHONE_ID_CONFLICT
    assert conflict["phone_number_normalized"] is None


def test_audio_tag_identity_probe_is_read_only_and_uses_synthetic_tags() -> None:
    metadata = {"format": "id3v2.3", "tags": {"TIT2": ["customer 138 0013 8000"], "TPE1": ["synthetic"]}}
    candidates = audio_tag_phone_candidate_details(metadata)
    assert candidates == [{"raw": "138 0013 8000", "normalized": "13800138000"}]


def test_wpd_summary_redacts_values_and_call_log_capability_is_explicit() -> None:
    raw = {
        "source": {"properties": {"System.Title": "private-name", "System.Size": "9"}, "shell_columns": [{"label": "Name", "value": "private-name"}]},
        "adjacent_objects": [{"is_folder": False, "extension": ".mp3"}],
    }
    summary = safe_wpd_summary(raw)
    assert "private-name" not in json.dumps(summary)
    blocked = classify_call_log_exposure({"devices": [{"storage": [{"direct_children": [{"name": "Music"}]}]}]})
    exposed = classify_call_log_exposure({"devices": [{"storage": [{"direct_children": [{"name": "Call History"}]}]}]})
    assert blocked["status"] == CALL_LOG_NOT_EXPOSED
    assert blocked["backup_or_export_object_exposed"] is False
    assert exposed["status"] == CALL_LOG_EXPOSED


def test_controlled_event_enrichment_keeps_event_id_and_media_identity(tmp_path: Path) -> None:
    ingestor = Ingestor(tmp_path / "data")
    staged = stage_file(ingestor.paths["stage"], "20250115_101530_call.m4a")
    assert ingestor.ingest_staged_for_test(staged, source_for(staged)) == "imported"
    recording_path = next(ingestor.paths["ready"].glob("*.json"))
    recording = json.loads(recording_path.read_text(encoding="utf-8"))
    event_path = next(ingestor.paths["events"].glob("*.json"))
    original_event = json.loads(event_path.read_text(encoding="utf-8"))
    updated_recording = {
        **recording,
        "phone_number": "138 0013 8000",
        "phone_number_source": "filename",
        "phone_number_confidence": "low",
    }
    validate_recording(updated_recording)
    ingestor._write_sidecar_atomically(recording_path, updated_recording)
    updated_event = build_communication_event(
        recording=updated_recording, installation_id=original_event["installation_id"], salesperson_id=None
    )
    replace_event_atomically(event_path, updated_event, ingestor.paths["ready"] / recording["media_filename"], recording_path)
    rewritten = json.loads(event_path.read_text(encoding="utf-8"))
    assert rewritten["event_id"] == original_event["event_id"]
    assert rewritten["media_sha256"] == original_event["media_sha256"]
    assert rewritten["phone_number_normalized"] == "13800138000"
    assert rewritten["phone_number_source"] == "filename"
