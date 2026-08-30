from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sales_mobile_ingest.calllog_export import (
    AMBIGUOUS,
    EXACT,
    NO_MATCH,
    parse_synctech_calllog_export,
    synctech_snapshot_metadata,
)
from sales_mobile_ingest.call_fact_handoff import CallFactHandoffPublisher
from sales_mobile_ingest.cli import build_parser
from sales_mobile_ingest.config import SalespersonIdentity, ensure_layout
from sales_mobile_ingest import config as config_module
from sales_mobile_ingest.device_registry import DeviceRegistry, DeviceRegistryError
from sales_mobile_ingest.phone_calls import (
    build_phone_call,
    phone_call_id,
    validate_call_recording_link,
    validate_phone_call,
)
from sales_mobile_ingest.service import Ingestor
from sales_mobile_ingest.state import StateStore


SYNTHETIC_NUMBER = "15500001001"
BASE_DATE_MS = 1_736_907_330_000


def _xml(rows: list[dict[str, str]], **root_attributes: str) -> str:
    root = {"count": str(len(rows)), **root_attributes}
    root_text = " ".join(f'{key}="{value}"' for key, value in root.items())
    elements = "".join(
        "<call " + " ".join(f'{key}="{value}"' for key, value in row.items()) + " />" for row in rows
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><calls {root_text}>{elements}</calls>'


def _row(
    *,
    date_ms: int = BASE_DATE_MS,
    duration: str = "190",
    call_type: str = "2",
    number: str = SYNTHETIC_NUMBER,
    subscription_id: str | None = None,
    contact_name: str | None = None,
) -> dict[str, str]:
    value = {"date": str(date_ms), "duration": duration, "type": call_type, "number": number}
    if subscription_id is not None:
        value["subscription_id"] = subscription_id
        value["subscription_component_name"] = "synthetic.subscription"
    if contact_name is not None:
        value["contact_name"] = contact_name
    return value


class XmlBridge:
    def __init__(self, sources: list[tuple[str, Path]]) -> None:
        self.sources = sources

    def discover_calllog_exports(self, **_: object) -> dict:
        return {"devices": [
            {
                "device_key": alias,
                "display_name": f"Synthetic Android {index}",
                "directories": [{
                    "relative_path": "Internal/SMSBackupRestore",
                    "files": [{
                        "name": path.name,
                        "extension": ".xml",
                        "relative_path": f"Internal/SMSBackupRestore/{path.name}",
                        "size_bytes": path.stat().st_size,
                        "modified_at": "2025-01-15T02:16:00+00:00",
                    }],
                }],
            }
            for index, (alias, path) in enumerate(self.sources)
        ]}

    def copy_to_staging(self, source: dict, destination_dir: Path) -> Path:
        source_path = next(path for _, path in self.sources if path.name == source["name"])
        target = destination_dir / source_path.name
        shutil.copyfile(source_path, target)
        return target

    def probe(self, *_: object, **__: object) -> dict:
        return {"devices": []}


def _recording_source(path: Path, *, alias: str = "device-a", duration: int = 190) -> dict:
    return {
        "name": path.name,
        "extension": path.suffix,
        "relative_path": f"Internal/Recordings/{path.name}",
        "size_bytes": path.stat().st_size,
        "modified_at": "2025-01-15T02:15:30+00:00",
        "duration_seconds": duration,
        "duration_source": "wpd",
        "device_key": alias,
        "device_name": "Synthetic Android",
        "device_vendor": "Synthetic",
        "device_model": "Synthetic Android",
        "adapter": "generic-call-recording-v1",
    }


def _stage(ingestor: Ingestor, *, name: str = "20250115_101530_call.m4a", content: bytes = b"synthetic-audio") -> Path:
    path = ingestor.paths["stage"] / name
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(
    ("raw_type", "direction", "disposition"),
    [
        (1, "incoming", "unknown"),
        (2, "outgoing", "unknown"),
        (3, "incoming", "missed"),
        (4, "unknown", "voicemail"),
        (5, "incoming", "rejected"),
        (6, "incoming", "blocked"),
        (7, "incoming", "answered_externally"),
        (99, "unknown", "unknown"),
    ],
    ids=["incoming", "outgoing", "missed", "voicemail", "rejected", "blocked", "answered-externally", "future"],
)
def test_gc_call_types_keep_direction_disposition_and_raw(
    tmp_path: Path, raw_type: int, direction: str, disposition: str
) -> None:
    path = tmp_path / f"calls-{raw_type}.xml"
    path.write_text(_xml([_row(call_type=str(raw_type))]), encoding="utf-8")
    parsed = parse_synctech_calllog_export(path, artifact_sha256="a" * 64)[0]
    assert (parsed["call_direction"], parsed["call_disposition"], parsed["call_type"]) == (
        direction, disposition, raw_type
    )


def test_gc_dual_sim_is_preserved_and_part_of_source_row_identity(tmp_path: Path) -> None:
    path = tmp_path / "calls-dual-sim.xml"
    path.write_text(_xml([_row(subscription_id="10"), _row(subscription_id="11")]), encoding="utf-8")
    rows = parse_synctech_calllog_export(path, artifact_sha256="b" * 64)
    assert {row["subscription_id"] for row in rows} == {"10", "11"}
    assert len({row["canonical_call_id"] for row in rows}) == 2
    assert all(row["subscription_component_name"] == "synthetic.subscription" for row in rows)
    assert all(row["subscription_slot_index"] is None for row in rows)


def test_gc_contact_and_snapshot_changes_do_not_change_call_identity(tmp_path: Path) -> None:
    one = tmp_path / "calls-one.xml"
    two = tmp_path / "calls-two.xml"
    one.write_text(_xml([_row(contact_name="Synthetic Old")]), encoding="utf-8")
    two.write_text(_xml([_row(contact_name="Synthetic New")], backup_type="archive"), encoding="utf-8")
    first = parse_synctech_calllog_export(one, artifact_sha256="1" * 64)[0]
    second = parse_synctech_calllog_export(two, artifact_sha256="2" * 64)[0]
    assert first["canonical_call_id"] == second["canonical_call_id"]
    assert phone_call_id(provider_id=first["provider_id"], device_id="dev_00000000-0000-0000-0000-000000000001", source_row_id=first["canonical_call_id"]) == phone_call_id(
        provider_id=second["provider_id"], device_id="dev_00000000-0000-0000-0000-000000000001", source_row_id=second["canonical_call_id"]
    )

    bridge = XmlBridge([("device-a", one)])
    ingestor = Ingestor(tmp_path / "data", bridge=bridge)
    ingestor.ingest_calllog_exports()
    call_path = next(ingestor.paths["calls"].glob("*.json"))
    original_id = json.loads(call_path.read_text(encoding="utf-8"))["call_id"]
    bridge.sources = [("device-a", two)]
    ingestor.ingest_calllog_exports()
    updated = json.loads(call_path.read_text(encoding="utf-8"))
    assert updated["call_id"] == original_id
    assert updated["contact_name"] == "Synthetic New"


def test_gc_snapshot_fresh_stale_and_unknown_are_deterministic(tmp_path: Path) -> None:
    imported = "2026-08-30T00:00:00+00:00"
    fresh_epoch = int(datetime(2026, 8, 29, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
    stale_epoch = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp() * 1000)
    statuses = []
    for label, root in (
        ("fresh", {"backup_date": str(fresh_epoch)}),
        ("stale", {"backup_date": str(stale_epoch)}),
        ("unknown", {}),
    ):
        path = tmp_path / f"calls-{label}.xml"
        path.write_text(_xml([_row()], **root), encoding="utf-8")
        statuses.append(synctech_snapshot_metadata(
            path, artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), imported_at=imported,
            stale_after_seconds=48 * 3600, device_id="dev_00000000-0000-0000-0000-000000000001",
        )["freshness"])
    assert statuses == ["FRESH", "STALE", "UNKNOWN"]


def test_gc_snapshot_count_mismatch_never_claims_fresh_completeness(tmp_path: Path) -> None:
    backup_epoch = int(datetime.now(timezone.utc).timestamp() * 1000)
    xml = tmp_path / "calls-count-mismatch.xml"
    xml.write_text(_xml([_row()], count="2", backup_date=str(backup_epoch)), encoding="utf-8")
    ingestor = Ingestor(tmp_path / "data", bridge=XmlBridge([("device-a", xml)]))
    summary = ingestor.ingest_calllog_exports()
    call = json.loads(next(ingestor.paths["calls"].glob("*.json")).read_text(encoding="utf-8"))
    assert summary.snapshot_status == "UNKNOWN"
    assert call["snapshot"]["parse_status"] == "COUNT_MISMATCH"
    assert call["snapshot"]["freshness"] == "UNKNOWN"


def test_gc_calllog_first_publishes_call_without_recording_and_replays_ten_times(tmp_path: Path) -> None:
    xml = tmp_path / "calls-full.xml"
    xml.write_text(_xml([_row(call_type="3", duration="0")], backup_type="full"), encoding="utf-8")
    ingestor = Ingestor(tmp_path / "data", bridge=XmlBridge([("device-a", xml)]))
    for index in range(10):
        summary = ingestor.ingest_calllog_exports()
        assert summary.phone_calls_created == (1 if index == 0 else 0)
    calls = list(ingestor.paths["calls"].glob("*.json"))
    assert len(calls) == 1
    call = json.loads(calls[0].read_text(encoding="utf-8"))
    validate_phone_call(call)
    assert call["disposition"] == "missed"
    assert call["salesperson_attribution_status"] == "UNASSIGNED"
    assert not list(ingestor.paths["events"].glob("*.json"))
    assert not list(ingestor.paths["ready"].glob("*.m4a"))


def test_gc_calllog_first_then_recording_late_creates_one_call_and_one_link(tmp_path: Path) -> None:
    xml = tmp_path / "calls-late.xml"
    xml.write_text(_xml([_row()]), encoding="utf-8")
    ingestor = Ingestor(tmp_path / "data", bridge=XmlBridge([("device-a", xml)]))
    assert ingestor.ingest_calllog_exports().phone_calls_created == 1
    staged = _stage(ingestor)
    assert ingestor.ingest_staged_for_test(staged, _recording_source(staged)) == "imported"
    result = ingestor.ingest_calllog_exports()
    assert result.correlations_exact == 1
    assert len(list(ingestor.paths["calls"].glob("*.json"))) == 1
    links = list(ingestor.paths["call_links"].glob("*.json"))
    assert len(links) == 1
    link = json.loads(links[0].read_text(encoding="utf-8"))
    validate_call_recording_link(link)
    assert link["status"] == EXACT and link["call_id"] is not None


def test_gc_incoming_calllog_first_with_recording_and_normal_call_without_recording(tmp_path: Path) -> None:
    incoming_xml = tmp_path / "calls-incoming.xml"
    incoming_xml.write_text(_xml([_row(call_type="1")]), encoding="utf-8")
    incoming = Ingestor(tmp_path / "incoming", bridge=XmlBridge([("device-a", incoming_xml)]))
    assert incoming.ingest_calllog_exports().phone_calls_created == 1
    staged = _stage(incoming)
    incoming.ingest_staged_for_test(staged, _recording_source(staged))
    assert incoming.ingest_calllog_exports().correlations_exact == 1
    call = json.loads(next(incoming.paths["calls"].glob("*.json")).read_text(encoding="utf-8"))
    assert call["direction"] == "incoming" and call["disposition"] == "unknown"

    no_recording_xml = tmp_path / "calls-no-recording.xml"
    no_recording_xml.write_text(_xml([_row(call_type="2")]), encoding="utf-8")
    no_recording = Ingestor(tmp_path / "no-recording", bridge=XmlBridge([("device-b", no_recording_xml)]))
    no_recording.ingest_calllog_exports()
    assert len(list(no_recording.paths["calls"].glob("*.json"))) == 1
    assert not list(no_recording.paths["ready"].glob("*.m4a"))


def test_gc_rejected_call_is_published_without_media(tmp_path: Path) -> None:
    xml = tmp_path / "calls-rejected.xml"
    xml.write_text(_xml([_row(call_type="5", duration="0")]), encoding="utf-8")
    ingestor = Ingestor(tmp_path / "data", bridge=XmlBridge([("device-a", xml)]))
    ingestor.ingest_calllog_exports()
    call = json.loads(next(ingestor.paths["calls"].glob("*.json")).read_text(encoding="utf-8"))
    assert call["direction"] == "incoming" and call["disposition"] == "rejected"
    assert not list(ingestor.paths["events"].glob("*.json"))


def test_gc_recording_first_then_calllog_late_retains_one_of_each(tmp_path: Path) -> None:
    xml = tmp_path / "calls-late.xml"
    xml.write_text(_xml([_row()]), encoding="utf-8")
    bridge = XmlBridge([])
    ingestor = Ingestor(tmp_path / "data", bridge=bridge)
    staged = _stage(ingestor)
    assert ingestor.ingest_staged_for_test(staged, _recording_source(staged)) == "imported"
    assert ingestor.ingest_calllog_exports().correlations_no_match == 1
    first_link = json.loads(next(ingestor.paths["call_links"].glob("*.json")).read_text(encoding="utf-8"))
    assert first_link["status"] == NO_MATCH
    bridge.sources = [("device-a", xml)]
    assert ingestor.ingest_calllog_exports().correlations_exact == 1
    assert len(list(ingestor.paths["calls"].glob("*.json"))) == 1
    assert len(list(ingestor.paths["ready"].glob("*.m4a"))) == 1
    final_link = json.loads(next(ingestor.paths["call_links"].glob("*.json")).read_text(encoding="utf-8"))
    assert final_link["status"] == EXACT


def test_gc_unmatched_recording_and_ambiguous_candidates_are_not_hard_bound(tmp_path: Path) -> None:
    empty = tmp_path / "calls-empty.xml"
    empty.write_text(_xml([]), encoding="utf-8")
    ingestor = Ingestor(tmp_path / "unmatched", bridge=XmlBridge([("device-a", empty)]))
    staged = _stage(ingestor)
    ingestor.ingest_staged_for_test(staged, _recording_source(staged))
    assert ingestor.ingest_calllog_exports().correlations_no_match == 1
    link = json.loads(next(ingestor.paths["call_links"].glob("*.json")).read_text(encoding="utf-8"))
    assert link["status"] == NO_MATCH and link["call_id"] is None

    close = tmp_path / "calls-close.xml"
    close.write_text(_xml([_row(number="15500001002"), _row(number="15500001003")]), encoding="utf-8")
    ambiguous = Ingestor(tmp_path / "ambiguous", bridge=XmlBridge([("device-a", close)]))
    staged_two = _stage(ambiguous, content=b"other-synthetic-audio")
    ambiguous.ingest_staged_for_test(staged_two, _recording_source(staged_two))
    assert ambiguous.ingest_calllog_exports().correlations_ambiguous == 1
    link = json.loads(next(ambiguous.paths["call_links"].glob("*.json")).read_text(encoding="utf-8"))
    assert link["status"] == AMBIGUOUS and link["call_id"] is None and len(link["candidate_call_ids"]) == 2


def test_gc_full_incremental_archive_and_cross_device_identity(tmp_path: Path) -> None:
    files = []
    for mode in ("full", "incremental", "archive"):
        path = tmp_path / f"calls-{mode}.xml"
        path.write_text(_xml([_row()], backup_type=mode), encoding="utf-8")
        files.append(path)
    bridge = XmlBridge([("device-a", files[0])])
    ingestor = Ingestor(tmp_path / "data", bridge=bridge)
    for path in files:
        bridge.sources = [("device-a", path)]
        ingestor.ingest_calllog_exports()
    assert len(list(ingestor.paths["calls"].glob("*.json"))) == 1
    replayed_call = json.loads(next(ingestor.paths["calls"].glob("*.json")).read_text(encoding="utf-8"))
    assert len(replayed_call["source_snapshot_ids"]) == 3
    assert len(replayed_call["source_artifact_sha256s"]) == 3
    bridge.sources = [("device-a", files[0]), ("device-b", files[0])]
    ingestor.ingest_calllog_exports()
    calls = [json.loads(path.read_text(encoding="utf-8")) for path in ingestor.paths["calls"].glob("*.json")]
    assert len(calls) == 2
    assert len({call["device_id"] for call in calls}) == 2
    assert len({call["call_id"] for call in calls}) == 2


def test_gc_assignment_history_one_salesperson_many_devices_and_historical_owner() -> None:
    state: dict = {}
    registry = DeviceRegistry(state)
    device_a = registry.observe(observed_alias="a", display_name="A", vendor=None, model=None)
    device_b = registry.observe(observed_alias="b", display_name="B", vendor=None, model=None)
    registry.assign(
        device_id=device_a, salesperson_id="S001", salesperson_name="Synthetic A",
        effective_from="2026-01-01T00:00:00+00:00", effective_to="2026-10-01T00:00:00+00:00",
    )
    registry.assign(
        device_id=device_a, salesperson_id="S008", salesperson_name="Synthetic B",
        effective_from="2026-10-01T00:00:00+00:00",
    )
    registry.assign(
        device_id=device_b, salesperson_id="S001", salesperson_name="Synthetic A",
        effective_from="2026-01-01T00:00:00+00:00",
    )
    assert registry.attribution(device_id=device_a, occurred_at="2026-08-01T00:00:00+00:00")["salesperson_id"] == "S001"
    assert registry.attribution(device_id=device_a, occurred_at="2026-11-01T00:00:00+00:00")["salesperson_id"] == "S008"
    assert registry.attribution(device_id=device_b, occurred_at="2026-08-01T00:00:00+00:00")["salesperson_id"] == "S001"


def test_gc_assignment_overlap_is_rejected_and_unknown_device_is_unassigned() -> None:
    registry = DeviceRegistry({})
    device = registry.observe(observed_alias="a", display_name="A", vendor=None, model=None)
    registry.assign(
        device_id=device, salesperson_id="S001", salesperson_name="Synthetic A",
        effective_from="2026-01-01T00:00:00+00:00", effective_to="2026-10-01T00:00:00+00:00",
    )
    with pytest.raises(DeviceRegistryError, match="overlaps"):
        registry.assign(
            device_id=device, salesperson_id="S002", salesperson_name="Synthetic B",
            effective_from="2026-09-01T00:00:00+00:00",
        )
    assert registry.attribution(device_id=device, occurred_at="2025-01-01T00:00:00+00:00")["salesperson_attribution_status"] == "UNASSIGNED"


def test_gc_multiple_salespeople_and_devices_coexist_without_workstation_inference() -> None:
    registry = DeviceRegistry({})
    device_a = registry.observe(observed_alias="a", display_name="A", vendor=None, model=None)
    device_b = registry.observe(observed_alias="b", display_name="B", vendor=None, model=None)
    registry.assign(
        device_id=device_a, salesperson_id="S001", salesperson_name="Synthetic A",
        effective_from="2026-01-01T00:00:00+00:00",
    )
    registry.assign(
        device_id=device_b, salesperson_id="S002", salesperson_name="Synthetic B",
        effective_from="2026-01-01T00:00:00+00:00",
    )
    at = "2026-08-01T00:00:00+00:00"
    assert registry.attribution(device_id=device_a, occurred_at=at)["salesperson_id"] == "S001"
    assert registry.attribution(device_id=device_b, occurred_at=at)["salesperson_id"] == "S002"


def test_gc_unknown_new_device_never_inherits_legacy_workstation_salesperson(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sales_mobile_ingest.service.resolve_salesperson_identity",
        lambda: SalespersonIdentity("LEGACY", "Synthetic Legacy"),
    )
    ingestor = Ingestor(tmp_path / "data", bridge=XmlBridge([]))
    staged = _stage(ingestor)
    ingestor.ingest_staged_for_test(staged, _recording_source(staged, alias="new-unknown-device"))
    event = json.loads(next(ingestor.paths["events"].glob("*.json")).read_text(encoding="utf-8"))
    assert event["salesperson_id"] is None
    assert event["salesperson_identity_status"] == "UNCONFIGURED"


def test_gc_legacy_event_projection_tracks_effective_device_assignment(tmp_path: Path) -> None:
    ingestor = Ingestor(tmp_path / "data", bridge=XmlBridge([]))
    device_id = ingestor.device_registry.observe(
        observed_alias="device-a", display_name="Synthetic Android", vendor="Synthetic", model="Synthetic Android"
    )
    ingestor.assign_device(
        device_id=device_id, salesperson_id="S001", salesperson_name="Synthetic A",
        effective_from="2020-01-01T00:00:00+00:00",
    )
    staged = _stage(ingestor)
    ingestor.ingest_staged_for_test(staged, _recording_source(staged))
    event_path = next(ingestor.paths["events"].glob("*.json"))
    assert json.loads(event_path.read_text(encoding="utf-8"))["salesperson_id"] == "S001"
    ingestor.end_device_assignment(device_id=device_id, effective_to="2024-01-01T00:00:00+00:00")
    ingestor._reconcile_events()
    cleared = json.loads(event_path.read_text(encoding="utf-8"))
    assert cleared["salesperson_id"] is None
    assert cleared["salesperson_identity_status"] == "UNCONFIGURED"


def test_gc_malformed_xml_keeps_private_failure_evidence_and_publishes_no_call(tmp_path: Path) -> None:
    malformed = tmp_path / "calls-malformed.xml"
    malformed.write_text('<calls count="1"><call date="broken"', encoding="utf-8")
    ingestor = Ingestor(tmp_path / "data", bridge=XmlBridge([("device-a", malformed)]))
    summary = ingestor.ingest_calllog_exports()
    assert summary.snapshot_status == "MALFORMED"
    assert summary.schema_failures == 1
    assert not list(ingestor.paths["calls"].glob("*.json"))
    evidence = list(ingestor.paths["calllog_failed"].glob("*.failure.json"))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text(encoding="utf-8"))["parse_status"] == "MALFORMED"


def test_gc_state_v1_migration_is_atomic_backed_up_and_stable_after_reload(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    original = {
        "version": 1,
        "imports": {}, "sources": {}, "devices": {},
        "calllog_exports": {"sources": {}, "artifacts": {}, "rows": {}, "enrichments": {}},
        "cloud_handoff": {"publications": {}, "salesperson_directories": {}},
    }
    (state_dir / "ingest-state.json").write_text(json.dumps(original), encoding="utf-8")
    first = StateStore(state_dir)
    assert first.data["version"] == StateStore.CURRENT_VERSION
    assert first.migration_backup_path is not None and first.migration_backup_path.is_file()
    first.save()
    second = StateStore(state_dir)
    assert second.migration_backup_path is None
    assert second.data["device_registry"] == first.data["device_registry"]


def test_gc_legacy_identity_migrates_only_with_unique_historical_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "unique"
    paths = ensure_layout(data)
    state = {
        "version": 1,
        "imports": {}, "sources": {}, "devices": {"legacy-a": {"display_name": "Synthetic Android"}},
        "calllog_exports": {"sources": {}, "artifacts": {}, "enrichments": {}, "rows": {
            "legacy-a|row": {"device_key": "legacy-a", "occurred_at": "2026-01-01T00:00:00+00:00"}
        }},
        "cloud_handoff": {"publications": {}, "salesperson_directories": {}},
    }
    (paths["state"] / "ingest-state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        "sales_mobile_ingest.service.resolve_salesperson_identity",
        lambda: SalespersonIdentity("S001", "Synthetic Legacy"),
    )
    local_project = tmp_path / "local-project"
    local_project.mkdir()
    (local_project / "config.local.json").write_text(json.dumps({
        "salesperson_id": "S001", "salesperson_name": "Synthetic Legacy"
    }), encoding="utf-8")
    monkeypatch.setattr("sales_mobile_ingest.config.PROJECT_ROOT", local_project)
    unique = Ingestor(data, bridge=XmlBridge([]))
    assert unique.device_registry.data["migration"]["status"] == "MIGRATED"
    assert len(unique.device_registry.data["assignments"]) == 1
    backup_name = unique.device_registry.data["migration"]["config_backup"]
    assert backup_name and (unique.paths["migration_evidence"] / backup_name).is_file()

    ambiguous_data = tmp_path / "ambiguous-legacy"
    ambiguous_paths = ensure_layout(ambiguous_data)
    state["devices"]["legacy-b"] = {"display_name": "Synthetic Android B"}
    (ambiguous_paths["state"] / "ingest-state.json").write_text(json.dumps(state), encoding="utf-8")
    ambiguous = Ingestor(ambiguous_data, bridge=XmlBridge([]))
    assert ambiguous.device_registry.data["migration"]["status"] == "BLOCKED_AMBIGUOUS"
    assert not ambiguous.device_registry.data["assignments"]


def test_gc_restart_keeps_call_recording_and_link_identity(tmp_path: Path) -> None:
    xml = tmp_path / "calls-restart.xml"
    xml.write_text(_xml([_row()]), encoding="utf-8")
    data = tmp_path / "data"
    first = Ingestor(data, bridge=XmlBridge([("device-a", xml)]))
    staged = _stage(first)
    first.ingest_staged_for_test(staged, _recording_source(staged))
    first.ingest_calllog_exports()
    before = {
        path.relative_to(data).as_posix(): path.read_bytes()
        for directory in (first.paths["ready"], first.paths["calls"], first.paths["call_links"])
        for path in directory.glob("*.json")
    }
    second = Ingestor(data, bridge=XmlBridge([("device-a", xml)]))
    second.ingest_calllog_exports()
    after = {
        path.relative_to(data).as_posix(): path.read_bytes()
        for directory in (second.paths["ready"], second.paths["calls"], second.paths["call_links"])
        for path in directory.glob("*.json")
    }
    assert before == after


def test_gc_operator_cli_surface_does_not_require_json_editing() -> None:
    parser = build_parser()
    assert parser.parse_args(["list-devices", "--discover"]).command == "list-devices"
    parsed = parser.parse_args([
        "assign-device", "--device-id", "dev_00000000-0000-0000-0000-000000000001",
        "--salesperson-id", "S001", "--salesperson-name", "Synthetic",
        "--effective-from", "2026-01-01T00:00:00+00:00",
    ])
    assert parsed.command == "assign-device"
    assert parser.parse_args([
        "end-device-assignment", "--device-id", parsed.device_id,
        "--effective-to", "2026-10-01T00:00:00+00:00",
    ]).command == "end-device-assignment"


def test_data_root_priority_is_cli_then_local_config_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    configured = tmp_path / "configured"
    environment = tmp_path / "environment"
    cli = tmp_path / "cli"
    (project / "config.local.json").write_text(json.dumps({"data_root": str(configured)}), encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", project)
    monkeypatch.setenv("SALES_MOBILE_INGEST_DATA_ROOT", str(environment))
    assert config_module.resolve_data_root() == configured
    assert config_module.resolve_data_root(str(cli)) == cli


def test_gc_new_contract_examples_are_schema_valid() -> None:
    examples = Path(__file__).parents[1] / "contract" / "examples"
    validate_phone_call(json.loads((examples / "phone_call.call_only.example.json").read_text(encoding="utf-8")))
    validate_call_recording_link(json.loads((examples / "call_recording_link.example.json").read_text(encoding="utf-8")))


def test_gc_call_only_cloud_handoff_is_versioned_and_never_requires_audio(tmp_path: Path) -> None:
    xml = tmp_path / "calls-only.xml"
    xml.write_text(_xml([_row(call_type="3", duration="0")]), encoding="utf-8")
    data = tmp_path / "data"
    ingestor = Ingestor(data, bridge=XmlBridge([("device-a", xml)]))
    ingestor.ingest_calllog_exports()
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    publisher = CallFactHandoffPublisher(
        data_root=data, ready_calls=ingestor.paths["calls"], ready_links=ingestor.paths["call_links"],
        state=ingestor.state, cloud_handoff_root=cloud
    )
    first = publisher.publish()
    second = publisher.publish()
    assert first.published == 1 and first.failures == 0
    assert second.already_published == 1
    route_files = list((cloud / "_phone-call-facts-v1").glob("*.json"))
    assert len(route_files) == 1
    validate_phone_call(json.loads(route_files[0].read_text(encoding="utf-8")))
    assert not list(cloud.rglob("audio.*"))
    assert not list(cloud.rglob("event.json"))
