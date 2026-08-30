from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from sales_mobile_ingest.desktop_application import (
    BLOCKER,
    READY,
    PreflightStatus,
    StatusCard,
)
from sales_mobile_ingest.desktop_ui import FirstRunWizard, MainWindow, SettingsDialog, create_application


class StaticService:
    def __init__(self, status: PreflightStatus) -> None:
        self.status = status

    def preflight(self) -> PreflightStatus:
        return self.status


def _status(*, ready: bool, first_run: bool = False, data_root: Path | None = None) -> PreflightStatus:
    severity = READY if ready else BLOCKER
    cards = (
        StatusCard("phone", "手机", "OPPO A6 Pro 5G · 已连接", "销售：张三", severity=severity),
        StatusCard("calllog", "通话记录", "通话记录：正常", "已安全读取。", severity=severity),
        StatusCard("schedule", "定时备份健康度", "尚待实际验证", "不阻止导入。"),
        StatusCard("recording", "通话录音", "通话录音：正常", "已发现目录。", severity=READY),
        StatusCard("cloud", "坚果云交付", "坚果云交付：正常", "已确认本机目录。", severity=severity),
    )
    return PreflightStatus(
        overall=severity,
        overall_title="可以导入" if ready else "暂时无法完整导入",
        overall_detail="状态说明",
        can_import=ready,
        requires_first_run=first_run,
        cards=cards,
        device_id="dev_synthetic",
        device_name="OPPO A6 Pro 5G",
        salesperson_id=None if first_run else "S001",
        salesperson_name=None if first_run else "张三",
        earliest_call_at="2025-01-01T00:00:00+00:00",
        estimated_new_calls=6,
        data_root=data_root,
    )


def test_main_window_renders_five_business_cards_and_button_state(tmp_path: Path) -> None:
    app = create_application()
    status = _status(ready=True, data_root=tmp_path)
    window = MainWindow(StaticService(status), auto_refresh=False, auto_wizard=False)  # type: ignore[arg-type]
    window.apply_preflight(status)
    window.show()
    app.processEvents()
    assert len(window.card_widgets) == 5
    assert window.import_button.isEnabled()
    assert window.preview_calls.text() == "预计新增电话：6"
    screenshot = tmp_path / "source-gui-smoke.png"
    assert window.grab().save(str(screenshot))
    assert screenshot.stat().st_size > 0
    window.close()
    app.processEvents()
    assert not window.isVisible()


def test_first_run_and_settings_dialogs_open_without_internal_ids(tmp_path: Path) -> None:
    app = create_application()
    status = _status(ready=False, first_run=True, data_root=tmp_path)
    service = StaticService(status)
    window = MainWindow(service, auto_refresh=False, auto_wizard=False)  # type: ignore[arg-type]
    window.apply_preflight(status)
    wizard = FirstRunWizard(status, service, window)  # type: ignore[arg-type]
    wizard.show()
    settings = SettingsDialog(status, service, window)  # type: ignore[arg-type]
    settings.show()
    app.processEvents()
    assert wizard.isVisible()
    assert settings.isVisible()
    assert "dev_synthetic" not in wizard.windowTitle()
    settings.close()
    wizard.close()
    window.close()
    app.processEvents()
