from __future__ import annotations

import json
from pathlib import Path

import pytest

import sales_mobile_ingest.config as config_module
import sales_mobile_ingest.service as service_module
from sales_mobile_ingest.cloud_handoff import (
    CloudHandoffError,
    CloudHandoffPublisher,
    package_date_and_folder,
    sanitize_windows_component,
    validate_cloud_event,
    validate_cloud_recording,
    validate_cloud_package,
)
from sales_mobile_ingest.config import SalespersonIdentity
from sales_mobile_ingest.cli import main as cli_main
from sales_mobile_ingest.contract import build_metadata, sha256_file
from sales_mobile_ingest.events import build_communication_event
from sales_mobile_ingest.state import StateStore
from sales_mobile_ingest.service import Ingestor


IDENTITY = SalespersonIdentity("S007", "Synthetic Sales")


def test_cloud_package_examples_are_valid_and_synthetic() -> None:
    examples = Path(__file__).parents[1] / "contract" / "examples"
    recording = json.loads((examples / "cloud_recording_package.example.json").read_text(encoding="utf-8"))
    event = json.loads((examples / "cloud_communication_event_package.example.json").read_text(encoding="utf-8"))
    validate_cloud_recording(recording)
    validate_cloud_event(event)
    assert "C:\\Users\\" not in json.dumps({"recording": recording, "event": event})


def _source(media: Path, *, timestamp: str = "2025-01-15T02:15:30+00:00") -> dict:
    return {
        "name": media.name,
        "relative_path": "Internal/Call Recordings/synthetic.m4a",
        "size_bytes": media.stat().st_size,
        "modified_at": timestamp,
        "device_vendor": "OPPO",
        "device_model": "Synthetic OPPO",
        "adapter": "oppo-v1",
        "duration_seconds": 190.0,
        "duration_source": "wpd",
    }


def _write_local_pair(root: Path, *, content: bytes = b"synthetic-cloud-audio", occurred_at: str = "2025-01-15T02:15:30+00:00") -> tuple[Path, Path, dict, dict]:
    recordings = root / "ready" / "recordings"
    events = root / "ready" / "events"
    recordings.mkdir(parents=True)
    events.mkdir(parents=True)
    media = recordings / "source.m4a"
    media.write_bytes(content)
    recording = build_metadata(
        source=_source(media, timestamp=occurred_at), sha256=sha256_file(media), media_filename=media.name,
        imported_at=occurred_at,
    )
    recording_path = recordings / "source.json"
    recording_path.write_text(json.dumps(recording), encoding="utf-8")
    event = build_communication_event(
        recording=recording,
        installation_id="ins_00000000-0000-0000-0000-000000000000",
        salesperson_id=IDENTITY.salesperson_id,
        salesperson_name=IDENTITY.salesperson_name,
    )
    event = {**event, "occurred_at": occurred_at}
    event_path = events / f"{event['event_id']}.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    return recordings, events, recording, event


def _publisher(tmp_path: Path, recordings: Path, events: Path) -> CloudHandoffPublisher:
    root = tmp_path / "cloud-root"
    root.mkdir(exist_ok=True)
    return CloudHandoffPublisher(
        data_root=tmp_path / "data",
        ready_recordings=recordings,
        ready_events=events,
        state=StateStore(tmp_path / "data" / "state"),
        identity=IDENTITY,
        cloud_handoff_root=root,
    )


def _single_package(root: Path) -> Path:
    return next(path for path in root.rglob("*") if path.is_dir() and (path / "event.json").is_file())


def test_salesperson_folder_sanitizes_display_without_changing_business_id() -> None:
    assert sanitize_windows_component(' S:007 / Name? ') == "S_007 _ Name_"
    assert sanitize_windows_component("CON") == "_CON"


def test_salesperson_identity_is_only_valid_when_id_and_name_are_both_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    config_module.update_local_config({"salesperson_id": "S007"})
    assert config_module.resolve_salesperson_identity() is None
    config_module.update_local_config({"salesperson_name": "Synthetic Sales"})
    assert config_module.resolve_salesperson_identity() == IDENTITY


def test_confirmed_sync_root_creates_only_a_dedicated_child_and_configures_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    data_root = tmp_path / "data"
    sync_root = tmp_path / "confirmed-sync-root"
    sync_root.mkdir()
    assert cli_main([
        "--data-root", str(data_root), "configure-cloud-handoff", "--sync-root", str(sync_root),
    ]) == 0
    handoff = sync_root / "销售通话数据"
    assert handoff.is_dir()
    assert config_module.local_config()["cloud_handoff_root"] == str(handoff.resolve())


def test_cloud_publish_creates_exactly_three_portable_files(tmp_path: Path) -> None:
    recordings, events, recording, event = _write_local_pair(tmp_path / "data")
    publisher = _publisher(tmp_path, recordings, events)
    summary = publisher.publish()
    assert summary.published == 1
    cloud_root = tmp_path / "cloud-root"
    package = _single_package(cloud_root)
    assert package.parts[-3] == "S007_Synthetic Sales"
    assert package.parts[-2] == "2025-01-15"
    assert set(path.name for path in package.iterdir()) == {"audio.m4a", "recording.json", "event.json"}
    result = validate_cloud_package(package)
    assert result.recording_id == recording["recording_id"]
    assert result.event_id == event["event_id"]
    assert (package / "audio.m4a").read_bytes() == (recordings / "source.m4a").read_bytes()
    exported_recording = json.loads((package / "recording.json").read_text(encoding="utf-8"))
    exported_event = json.loads((package / "event.json").read_text(encoding="utf-8"))
    assert exported_recording["sha256"] == sha256_file(package / "audio.m4a")
    assert exported_event["media_ref"] == "audio.m4a"
    package_text = json.dumps({"recording": exported_recording, "event": exported_event})
    assert "C:\\Users\\" not in package_text
    assert "source.m4a" not in package_text


def test_missing_salesperson_blocks_publish_without_creating_any_cloud_package(tmp_path: Path) -> None:
    recordings, events, _, event = _write_local_pair(tmp_path / "data")
    cloud_root = tmp_path / "cloud-root"
    cloud_root.mkdir()
    publisher = CloudHandoffPublisher(
        data_root=tmp_path / "data", ready_recordings=recordings, ready_events=events,
        state=StateStore(tmp_path / "data" / "state"), identity=None, cloud_handoff_root=cloud_root,
    )
    summary = publisher.publish()
    assert summary.status == "SALESPERSON_IDENTITY_UNCONFIGURED"
    assert summary.blocked == 1
    assert list(cloud_root.iterdir()) == []
    assert publisher.state.cloud_publication(event["event_id"])["status"] == "SALESPERSON_IDENTITY_UNCONFIGURED"


def test_existing_valid_package_is_idempotent_and_late_enrichment_does_not_duplicate(tmp_path: Path) -> None:
    recordings, events, _, event = _write_local_pair(tmp_path / "data")
    publisher = _publisher(tmp_path, recordings, events)
    assert publisher.publish().published == 1
    assert publisher.publish().already_published == 1
    event_path = events / f"{event['event_id']}.json"
    late = json.loads(event_path.read_text(encoding="utf-8"))
    late["phone_number_raw"] = "15500001234"
    late["phone_number_normalized"] = "15500001234"
    late["phone_number_source"] = "calllog_export"
    late["phone_number_confidence"] = "medium"
    event_path.write_text(json.dumps(late), encoding="utf-8")
    summary = publisher.publish()
    assert summary.already_published == summary.immutable_enrichment_pending == 1
    assert len(list((tmp_path / "cloud-root").rglob("event.json"))) == 1


def test_name_change_keeps_the_salesperson_id_stable_package_path(tmp_path: Path) -> None:
    recordings, events, _, event = _write_local_pair(tmp_path / "data")
    first = _publisher(tmp_path, recordings, events)
    assert first.publish().published == 1
    event_path = events / f"{event['event_id']}.json"
    renamed_event = json.loads(event_path.read_text(encoding="utf-8"))
    renamed_event["salesperson_name"] = "Renamed Sales"
    event_path.write_text(json.dumps(renamed_event), encoding="utf-8")
    second = CloudHandoffPublisher(
        data_root=tmp_path / "data", ready_recordings=recordings, ready_events=events,
        state=first.state, identity=SalespersonIdentity("S007", "Renamed Sales"),
        cloud_handoff_root=tmp_path / "cloud-root",
    )
    summary = second.publish()
    assert summary.already_published == summary.immutable_enrichment_pending == 1
    assert len(list((tmp_path / "cloud-root").rglob("event.json"))) == 1
    assert "S007_Synthetic Sales" in str(_single_package(tmp_path / "cloud-root"))


def test_missing_cloud_root_blocks_publish_without_creating_any_cloud_package(tmp_path: Path) -> None:
    recordings, events, _, _ = _write_local_pair(tmp_path / "data")
    publisher = CloudHandoffPublisher(
        data_root=tmp_path / "data", ready_recordings=recordings, ready_events=events,
        state=StateStore(tmp_path / "data" / "state"), identity=IDENTITY, cloud_handoff_root=None,
    )
    summary = publisher.publish()
    assert summary.status == "CLOUD_HANDOFF_ROOT_UNCONFIGURED"
    assert summary.blocked == 1


def test_watch_cycle_automatically_publishes_a_complete_event_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud_root = tmp_path / "cloud-root"
    cloud_root.mkdir()

    class FakeBridge:
        def probe(self, *_: object, **__: object) -> dict:
            return {"devices": []}

        def discover_calllog_exports(self, **_: object) -> dict:
            return {"devices": []}

    monkeypatch.setattr(service_module, "resolve_salesperson_identity", lambda: IDENTITY)
    monkeypatch.setattr(service_module, "resolve_cloud_handoff_root", lambda: cloud_root)
    ingestor = Ingestor(tmp_path / "data", bridge=FakeBridge())
    staged = ingestor.paths["stage"] / "synthetic.m4a"
    staged.write_bytes(b"automatic-watch-publish")
    assert ingestor.ingest_staged_for_test(staged, _source(staged)) == "imported"
    summary = ingestor.ingest_once()
    assert summary.cloud_handoff_status == "CLOUD_HANDOFF_READY"
    assert summary.cloud_packages_published == 1
    assert len(list(cloud_root.rglob("event.json"))) == 1


def test_package_validator_rejects_incomplete_and_extra_file(tmp_path: Path) -> None:
    package = tmp_path / "incomplete"
    package.mkdir()
    (package / "audio.m4a").write_bytes(b"synthetic")
    with pytest.raises(CloudHandoffError):
        validate_cloud_package(package)
    recordings, events, _, _ = _write_local_pair(tmp_path / "data")
    publisher = _publisher(tmp_path, recordings, events)
    assert publisher.publish().published == 1
    valid = _single_package(tmp_path / "cloud-root")
    (valid / "unexpected.txt").write_text("synthetic", encoding="utf-8")
    with pytest.raises(CloudHandoffError):
        validate_cloud_package(valid)


def test_same_second_events_use_stable_distinct_event_id_shortcodes(tmp_path: Path) -> None:
    recordings, events, _, event_one = _write_local_pair(tmp_path / "data", content=b"one")
    media_two = recordings / "source-two.m4a"
    media_two.write_bytes(b"two")
    recording_two = build_metadata(
        source=_source(media_two), sha256=sha256_file(media_two), media_filename=media_two.name,
        imported_at="2025-01-15T02:15:30+00:00",
    )
    (recordings / "source-two.json").write_text(json.dumps(recording_two), encoding="utf-8")
    event_two = build_communication_event(
        recording=recording_two,
        installation_id="ins_00000000-0000-0000-0000-000000000000",
        salesperson_id=IDENTITY.salesperson_id,
        salesperson_name=IDENTITY.salesperson_name,
    )
    (events / f"{event_two['event_id']}.json").write_text(json.dumps(event_two), encoding="utf-8")
    assert package_date_and_folder(event_one)[0] == package_date_and_folder(event_two)[0]
    assert package_date_and_folder(event_one)[1] != package_date_and_folder(event_two)[1]
    publisher = _publisher(tmp_path, recordings, events)
    assert publisher.publish().published == 2
    assert len(list((tmp_path / "cloud-root").rglob("event.json"))) == 2


def test_conflicting_existing_directory_is_not_overwritten_and_staging_stays_outside_root(tmp_path: Path) -> None:
    recordings, events, _, event = _write_local_pair(tmp_path / "data")
    publisher = _publisher(tmp_path, recordings, events)
    date, call = package_date_and_folder(event)
    conflict = tmp_path / "cloud-root" / "S007_Synthetic Sales" / date / call
    conflict.mkdir(parents=True)
    (conflict / "unexpected.txt").write_text("do not overwrite", encoding="utf-8")
    summary = publisher.publish()
    assert summary.conflicts == 1
    assert (conflict / "unexpected.txt").is_file()
    assert publisher.state.cloud_publication(event["event_id"])["status"] == "conflict"
    assert not list((tmp_path / "cloud-root").rglob("*.stage-*"))
    local_stage = tmp_path / "data" / "cloud-handoff-stage"
    assert not local_stage.exists() or not list(local_stage.glob("*"))
