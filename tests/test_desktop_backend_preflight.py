from __future__ import annotations

from pathlib import Path

import pytest

from sales_mobile_ingest.service import Ingestor


def _xml(*, backup_ms: int | None, count: int = 1, malformed: bool = False) -> bytes:
    if malformed:
        return b"<calls><call"
    backup = f' backup_date="{backup_ms}"' if backup_ms is not None else ""
    return (
        f'<calls count="{count}"{backup}>'
        '<call date="1788084000000" duration="60" type="2" number="13812345678"/>'
        "</calls>"
    ).encode("utf-8")


class PreflightBridge:
    def __init__(self, content: bytes, *, directory: bool = True, include_xml: bool = True) -> None:
        self.content = content
        self.directory = directory
        self.include_xml = include_xml
        self.modified_at = "2026-08-30T10:00:00+00:00"

    def probe(self, cached_dirs: object = None, search_depth: int = 3) -> dict:
        return {
            "observation": "ok",
            "devices": [{
                "device_key": "synthetic-device-alias",
                "display_name": "OPPO A6 Pro 5G",
                "storage_roots": ["Internal shared storage"],
                "search_depth": search_depth,
                "candidates": [],
            }],
        }

    def discover_calllog_exports(self, **_: object) -> dict:
        directories = []
        if self.directory:
            files = []
            if self.include_xml:
                files.append({
                    "name": "calls-synthetic.xml",
                    "extension": ".xml",
                    "relative_path": "Internal shared storage/SMSBackupRestore/calls-synthetic.xml",
                    "size_bytes": len(self.content),
                    "modified_at": self.modified_at,
                })
            directories.append({"relative_path": "Internal shared storage/SMSBackupRestore", "files": files})
        return {
            "observation": "ok",
            "devices": [{
                "device_key": "synthetic-device-alias",
                "display_name": "OPPO A6 Pro 5G",
                "directories": directories,
            }],
        }

    def copy_to_staging(self, source: dict, destination_dir: Path) -> Path:
        destination = destination_dir / "calls-synthetic.xml"
        destination.write_bytes(self.content)
        return destination


def test_real_preflight_distinguishes_missing_directory_and_empty_directory(tmp_path: Path) -> None:
    missing = Ingestor(tmp_path / "missing", bridge=PreflightBridge(_xml(backup_ms=None), directory=False))
    assert missing.preflight_calllog_exports().status == "MISSING_DIRECTORY"
    empty = Ingestor(tmp_path / "empty", bridge=PreflightBridge(_xml(backup_ms=None), include_xml=False))
    assert empty.preflight_calllog_exports().status == "NO_XML"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (_xml(backup_ms=1788084000000), "FRESH"),
        (_xml(backup_ms=None), "UNKNOWN"),
        (_xml(backup_ms=1780000000000), "STALE"),
        (_xml(backup_ms=1788084000000, count=2), "COUNT_MISMATCH"),
        (_xml(backup_ms=None, malformed=True), "MALFORMED"),
    ],
)
def test_real_preflight_validates_parser_count_and_freshness(
    tmp_path: Path, content: bytes, expected: str
) -> None:
    ingestor = Ingestor(tmp_path / expected, bridge=PreflightBridge(content))
    result = ingestor.preflight_calllog_exports(observed_at="2026-08-30T10:10:00+00:00")
    assert result.status == expected
    assert list(ingestor.paths["calls"].glob("*.json")) == []


def test_real_preflight_records_only_later_snapshot_update_evidence(tmp_path: Path) -> None:
    first_backup = int(1788084000 * 1000)
    bridge = PreflightBridge(_xml(backup_ms=first_backup))
    ingestor = Ingestor(tmp_path / "data", bridge=bridge)
    first = ingestor.preflight_calllog_exports(observed_at="2026-08-30T10:10:00+00:00")
    assert first.scheduled_backup_evidence == "UNVERIFIED"

    bridge.content = _xml(backup_ms=first_backup + 60 * 60 * 1000)
    bridge.modified_at = "2026-08-30T11:00:00+00:00"
    second = ingestor.preflight_calllog_exports(observed_at="2026-08-30T11:10:00+00:00")
    assert second.scheduled_backup_evidence == "OBSERVED_UPDATE"
    assert second.estimated_new_calls == 1


def test_desktop_device_assignment_persists_across_backend_restart(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    bridge = PreflightBridge(_xml(backup_ms=None))
    first = Ingestor(data_root, bridge=bridge)
    device = first.discover_devices_for_desktop()[0]
    first.assign_device(
        device_id=device["device_id"],
        salesperson_id="S001",
        salesperson_name="Synthetic Sales",
        effective_from="2020-01-01T00:00:00+00:00",
    )
    restarted = Ingestor(data_root, bridge=bridge)
    observed = restarted.discover_devices_for_desktop()[0]
    assert observed["assignment_status"] == "ASSIGNED"
    assert observed["salesperson_id"] == "S001"
