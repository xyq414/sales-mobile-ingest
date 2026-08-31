from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from sales_mobile_ingest import config as config_module
from sales_mobile_ingest.desktop_application import (
    BLOCKER,
    READY,
    WARNING,
    HumanActionRequired,
    ImportWorkflowService,
    privacy_minimal_text,
)
from sales_mobile_ingest.resources import resource_path
from sales_mobile_ingest.service import CallLogPreflightSummary, IngestSummary, Ingestor
from sales_mobile_ingest.state import StateStore


def _device(*, assigned: bool = True, recording_directory: bool = True, recording_files: int = 2) -> dict[str, Any]:
    return {
        "device_id": "dev_synthetic",
        "display_name": "OPPO A6 Pro 5G",
        "vendor": "OPPO",
        "model": "A6 Pro 5G",
        "mtp_usable": True,
        "salesperson_id": "S001" if assigned else None,
        "salesperson_name": "张三" if assigned else None,
        "assignment_status": "ASSIGNED" if assigned else "UNASSIGNED",
        "recording_directory_found": recording_directory,
        "recording_file_count": recording_files,
    }


def _calllog(status: str = "FRESH", **overrides: Any) -> CallLogPreflightSummary:
    values = {
        "status": status,
        "directories_scanned": 1,
        "xml_candidates": 1,
        "parsed_rows": 3,
        "root_count": 3,
        "backup_timestamp": "2026-08-30T14:18:00+00:00",
        "freshness": status if status in {"FRESH", "STALE", "UNKNOWN"} else "UNKNOWN",
        "parse_status": "PARSED",
        "device_id": "dev_synthetic",
        "earliest_call_at": "2025-01-01T00:00:00+00:00",
        "estimated_new_calls": 2,
        "scheduled_backup_evidence": "OBSERVED_UPDATE",
    }
    values.update(overrides)
    return CallLogPreflightSummary(**values)


class FakeDesktopBackend:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.devices: list[dict[str, Any]] = [_device()]
        self.calllog = _calllog()
        self.summary = IngestSummary(
            calllog_snapshot_status="FRESH",
            call_fact_handoff_status="CALL_FACT_HANDOFF_READY",
            cloud_handoff_status="CLOUD_HANDOFF_READY",
        )
        self.before = {"calls": set(), "recordings": set(), "links": {}}
        self.after = {
            "calls": {"pc_new", "pc_without_recording"},
            "recordings": {"rec_new"},
            "links": {"rec_new": {"status": "EXACT", "call_id": "pc_new"}},
        }
        self.snapshot_calls = 0
        self.assignments: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.raise_on_discover: Exception | None = None
        self.progress_messages: list[str] = []
        self.started: threading.Event | None = None
        self.release: threading.Event | None = None

    def discover_devices_for_desktop(self) -> list[dict[str, Any]]:
        if self.raise_on_discover:
            raise self.raise_on_discover
        return self.devices

    def preflight_calllog_exports(self, *, observed_at: str | None = None) -> CallLogPreflightSummary:
        return self.calllog

    def assign_device(self, **kwargs: Any) -> dict[str, Any]:
        self.assignments.append(dict(kwargs))
        return dict(kwargs)

    def ingest_once(self, limit: int | None = None, progress: Any = None) -> IngestSummary:
        for message in (
            "正在读取通话记录…",
            "正在导入新增录音…",
            "正在关联电话与录音…",
            "正在去重…",
            "正在写入坚果云交付目录…",
            "正在验证…",
        ):
            self.progress_messages.append(message)
            if progress:
                progress(message)
        if self.started:
            self.started.set()
        if self.release:
            self.release.wait(timeout=5)
        return self.summary

    def desktop_business_snapshot(self) -> dict[str, Any]:
        self.snapshot_calls += 1
        return self.before if self.snapshot_calls % 2 else self.after

    def remember_desktop_import_run(self, summary: dict[str, Any]) -> None:
        self.runs.append(summary)

    def latest_desktop_import_run(self) -> dict[str, Any] | None:
        return self.runs[-1] if self.runs else None


def _service(tmp_path: Path, backend: FakeDesktopBackend | None = None, *, cloud: bool = True) -> tuple[ImportWorkflowService, FakeDesktopBackend, Path]:
    backend = backend or FakeDesktopBackend(tmp_path / "data")
    cloud_root = tmp_path / "nutstore-sync" / "销售通话数据"
    if cloud:
        cloud_root.mkdir(parents=True)
    service = ImportWorkflowService(
        backend,
        cloud_root_resolver=lambda: cloud_root if cloud else None,
        config_updater=lambda _: None,
        now=lambda: "2026-08-30T14:30:00+00:00",
    )
    return service, backend, cloud_root


def test_no_phone_is_a_human_readable_blocker(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.devices = []
    status = service.preflight()
    assert status.overall == BLOCKER
    assert status.card("phone").headline == "未连接手机"
    assert not status.can_import


def test_unknown_device_enters_first_run_without_inheriting_salesperson(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.devices = [_device(assigned=False)]
    status = service.preflight()
    assert status.requires_first_run
    assert status.salesperson_name is None
    assert "尚未绑定" in status.card("phone").detail


def test_known_device_shows_business_name_not_internal_identity(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    status = service.preflight()
    assert status.card("phone").detail == "销售：张三"
    visible = " ".join((status.card("phone").headline, status.card("phone").detail))
    assert "dev_synthetic" not in visible


@pytest.mark.parametrize(
    ("calllog", "headline"),
    [
        (_calllog("MISSING_DIRECTORY", directories_scanned=0, xml_candidates=0), "未找到通话记录备份"),
        (_calllog("NO_XML", xml_candidates=0), "已找到备份目录，但还没有通话记录文件"),
        (_calllog("MALFORMED", parse_status="MALFORMED"), "通话记录文件无法安全读取"),
        (_calllog("COUNT_MISMATCH", parse_status="COUNT_MISMATCH"), "通话记录文件无法安全读取"),
    ],
)
def test_unsafe_calllog_states_are_blockers(
    tmp_path: Path, calllog: CallLogPreflightSummary, headline: str
) -> None:
    service, backend, _ = _service(tmp_path)
    backend.calllog = calllog
    status = service.preflight()
    assert status.card("calllog").severity == BLOCKER
    assert status.card("calllog").headline == headline
    assert not status.can_import


def test_fresh_calllog_is_green_and_scheduled_history_is_separate(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.calllog = _calllog(scheduled_backup_evidence="UNVERIFIED")
    status = service.preflight()
    assert status.card("calllog").severity == READY
    assert status.card("schedule").headline == "尚待实际验证"
    assert status.card("schedule").severity == WARNING
    assert status.can_import


def test_stale_calllog_warns_but_allows_import(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.calllog = _calllog("STALE")
    status = service.preflight()
    assert status.card("calllog").severity == WARNING
    assert status.can_import


def test_unknown_freshness_never_claims_latest(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.calllog = _calllog("UNKNOWN", backup_timestamp=None)
    status = service.preflight()
    assert "无法确认" in status.card("calllog").headline
    assert status.calllog_freshness == "UNKNOWN"
    assert status.can_import


def test_observed_newer_snapshot_is_not_the_same_as_reading_app_settings(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first = {"snapshot_id": "snp_a", "backup_timestamp": "2026-08-30T00:00:00+00:00"}
    second = {"snapshot_id": "snp_b", "backup_timestamp": "2026-08-30T01:00:00+00:00"}
    assert store.observe_calllog_snapshot(
        device_id="dev_a", snapshot=first, observed_at="2026-08-30T00:05:00+00:00"
    ) == "UNVERIFIED"
    assert store.observe_calllog_snapshot(
        device_id="dev_a", snapshot=second, observed_at="2026-08-30T01:05:00+00:00"
    ) == "OBSERVED_UPDATE"
    store.save()
    assert StateStore(tmp_path / "state").data["desktop"]["calllog_observations"]["dev_a"]["observed_update_count"] == 1


def test_missing_recording_directory_is_only_a_warning(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.devices = [_device(recording_directory=False)]
    status = service.preflight()
    assert status.card("recording").severity == WARNING
    assert status.can_import


def test_empty_recording_directory_is_not_an_error(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.devices = [_device(recording_files=0)]
    status = service.preflight()
    assert status.card("recording").severity == READY
    assert "不是错误" in status.card("recording").detail


def test_deferred_recording_discovery_is_a_non_blocking_slow_mtp_warning(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.devices = [{**_device(), "recording_check_status": "DEFERRED_TO_IMPORT"}]
    status = service.preflight()
    assert status.card("recording").severity == WARNING
    assert status.card("recording").headline == "将在导入时定点检查"
    assert status.can_import


def test_missing_or_invalid_cloud_root_blocks_formal_import(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path, cloud=False)
    assert service.preflight().card("cloud").severity == BLOCKER
    backend = FakeDesktopBackend(tmp_path / "data2")
    invalid = tmp_path / "not-a-directory"
    invalid.write_text("x", encoding="utf-8")
    service = ImportWorkflowService(backend, cloud_root_resolver=lambda: invalid)
    status = service.preflight()
    assert status.card("cloud").severity == BLOCKER
    assert not status.can_import


def test_confirmed_cloud_root_is_writable_and_worded_within_evidence(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    card = service.preflight().card("cloud")
    assert card.severity == READY
    assert "不代表远端已同步" in card.detail
    assert "云端已同步成功" not in card.detail


def test_first_run_assignment_uses_explicit_history_boundary(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    status = service.preflight()
    service.assign_salesperson(
        device_id=status.device_id or "",
        salesperson_id=" S002 ",
        salesperson_name=" 李四 ",
        historical_all_belongs=True,
        effective_from=None,
    )
    assert backend.assignments[0]["effective_from"] == status.earliest_call_at
    assert backend.assignments[0]["salesperson_id"] == "S002"


def test_history_option_never_silently_backdates_without_calllog_evidence(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.calllog = _calllog(earliest_call_at=None)
    status = service.preflight()
    with pytest.raises(HumanActionRequired, match="还不能确认历史归属"):
        service.assign_salesperson(
            device_id=status.device_id or "", salesperson_id="S1", salesperson_name="张三",
            historical_all_belongs=True, effective_from=None,
        )
    assert backend.assignments == []


def test_one_click_import_reports_business_counts_and_canonical_progress_order(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.summary.phone_calls_created = 2
    backend.summary.new_imports = 1
    backend.summary.call_facts_published = 2
    observed: list[str] = []
    result = service.run_import(observed.append)
    assert result.new_calls == 2
    assert result.new_recordings == 1
    assert result.calls_without_recording == 1
    assert result.linked_recordings == 1
    assert result.handoff_status == "已写入坚果云同步目录"
    expected = [
        "正在读取通话记录…", "正在导入新增录音…", "正在关联电话与录音…",
        "正在去重…", "正在写入坚果云交付目录…", "正在验证…",
    ]
    positions = [observed.index(message) for message in expected]
    assert positions == sorted(positions)


def test_one_click_prevents_concurrent_double_run(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.started = threading.Event()
    backend.release = threading.Event()
    errors: list[Exception] = []

    def first() -> None:
        try:
            service.run_import()
        except Exception as exc:  # pragma: no cover - evidence captured below
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    assert backend.started.wait(timeout=3)
    with pytest.raises(HumanActionRequired, match="导入正在进行"):
        service.run_import()
    backend.release.set()
    thread.join(timeout=3)
    assert not errors


def test_calllog_failure_after_final_preflight_never_reports_success(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.summary.calllog_failures = 1
    backend.summary.calllog_snapshot_status = "NOT_RUN"
    with pytest.raises(HumanActionRequired, match="本地导入已完成，但交付验证未通过"):
        service.run_import()


def test_backend_error_is_translated_and_privacy_minimized(tmp_path: Path) -> None:
    service, backend, _ = _service(tmp_path)
    backend.raise_on_discover = RuntimeError(
        r"C:\phone\calls-13812345678.xml <call number='13812345678'>"
    )
    with pytest.raises(HumanActionRequired) as raised:
        service.preflight()
    detail = raised.value.technical_detail
    assert "13812345678" not in detail
    assert "calls-" not in detail
    assert "<call" not in detail


def test_privacy_filter_removes_number_path_and_xml() -> None:
    filtered = privacy_minimal_text(r"C:\private\calls-13812345678.xml <call number='13812345678'/>")
    assert "13812345678" not in filtered
    assert "C:\\private" not in filtered
    assert "<call" not in filtered


def test_privacy_filter_removes_long_encoded_command_arguments() -> None:
    encoded = "eyJvcGVyYXRpb24iOiJwcm9iZSIsImNhY2hlZF9kaXJzIjpbXX0" * 3
    filtered = privacy_minimal_text(f"-InputJsonBase64 {encoded}")
    assert encoded not in filtered
    assert "[编码参数已隐藏]" in filtered


def test_packaged_resources_are_declared_from_one_resolver() -> None:
    required = (
        resource_path("scripts", "mtp_bridge.ps1"),
        resource_path("contract", "recording.schema.json"),
        resource_path("contract", "phone_call.schema.json"),
        resource_path("contract", "call_recording_link.schema.json"),
    )
    assert all(path.is_file() for path in required)


def test_desktop_config_is_per_user_and_legacy_migration_is_non_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    legacy = project / "config.local.json"
    legacy.write_text(json.dumps({"data_root": "D:/legacy-data"}), encoding="utf-8")
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setattr(config_module, "PROJECT_ROOT", project)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv(config_module.CONFIG_PATH_ENV, raising=False)
    assert config_module.migrate_legacy_config_to_desktop()
    destination = config_module.desktop_config_path()
    assert destination.is_file()
    assert legacy.is_file()
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(destination))
    assert config_module.local_config()["data_root"] == "D:/legacy-data"


def test_relative_desktop_paths_resolve_from_persistent_config_not_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "persistent" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"data_root": "data", "cloud_handoff_root": "handoff"}), encoding="utf-8")
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV, str(config_path))
    assert config_module.resolve_data_root() == config_path.parent / "data"
    assert config_module.resolve_cloud_handoff_root() == config_path.parent / "handoff"


def test_safe_diagnostic_contains_no_customer_data(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    status = service.preflight()
    contaminated = replace(status, technical_codes=(r"C:\phone\calls-13812345678.xml <call/>",))
    output = service.write_safe_diagnostic(contaminated)
    text = output.read_text(encoding="utf-8")
    assert "13812345678" not in text
    assert "C:\\phone" not in text
    assert "<call" not in text
