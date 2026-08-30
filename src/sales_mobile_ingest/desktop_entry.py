from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sales_mobile_ingest.config import (
    CONFIG_PATH_ENV,
    active_config_path,
    desktop_config_path,
    local_config,
    migrate_legacy_config_to_desktop,
    update_local_config,
    use_desktop_config,
)
from sales_mobile_ingest.desktop_application import ImportWorkflowService
from sales_mobile_ingest.resources import resource_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="SalesMobileIngest")
    parser.add_argument("--smoke-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.smoke_report:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if CONFIG_PATH_ENV not in os.environ:
        migrate_legacy_config_to_desktop()
        use_desktop_config()
    if args.smoke_report:
        return _run_smoke(args.smoke_report, args.screenshot)

    from sales_mobile_ingest.desktop_ui import MainWindow, create_application

    application = create_application()
    window = MainWindow(ImportWorkflowService())
    window.show()
    return application.exec()


def _run_smoke(report_path: Path, screenshot_path: Path | None) -> int:
    from sales_mobile_ingest.desktop_ui import FirstRunWizard, MainWindow, SettingsDialog, create_application

    config = local_config()
    launch_count = int(config.get("smoke_launch_count", 0)) + 1
    update_local_config({"smoke_launch_count": launch_count})
    service = ImportWorkflowService()
    status = service.preflight()
    application = create_application()
    window = MainWindow(service, auto_refresh=False, auto_wizard=False)
    window.apply_preflight(status)
    window.show()
    application.processEvents()

    wizard = FirstRunWizard(status, service, window)
    wizard.show()
    application.processEvents()
    wizard.close()
    settings = SettingsDialog(status, service, window)
    settings.show()
    application.processEvents()
    settings.close()

    screenshot_saved = False
    if screenshot_path:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_saved = window.grab().save(str(screenshot_path))
    window.close()
    application.processEvents()

    resources = {
        "mtp_bridge": resource_path("scripts", "mtp_bridge.ps1"),
        "recording_schema": resource_path("contract", "recording.schema.json"),
        "phone_call_schema": resource_path("contract", "phone_call.schema.json"),
        "call_recording_link_schema": resource_path("contract", "call_recording_link.schema.json"),
    }
    report = {
        "status": "PASS" if all(path.is_file() for path in resources.values()) else "FAIL",
        "resources": {key: path.is_file() for key, path in resources.items()},
        "config_path": str(active_config_path()),
        "default_desktop_config_path": str(desktop_config_path()),
        "config_persisted_launch_count": launch_count,
        "preflight_completed": True,
        "preflight_overall": status.overall,
        "main_window_rendered": True,
        "home_cards_rendered": len(window.card_widgets) == 5,
        "first_run_wizard_opened": True,
        "settings_opened": True,
        "window_closed_cleanly": not window.isVisible(),
        "screenshot_saved": screenshot_saved if screenshot_path else None,
        "system_python_required": False,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
