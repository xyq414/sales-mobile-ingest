from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from .desktop_application import (
    BLOCKER,
    NEUTRAL,
    READY,
    WARNING,
    HumanActionRequired,
    ImportResult,
    ImportWorkflowService,
    PreflightStatus,
    StatusCard,
)


_COLORS = {
    READY: ("#E9F7EF", "#18794E", "#B8E4CB"),
    WARNING: ("#FFF7E6", "#9A6700", "#F4D58D"),
    BLOCKER: ("#FFF0F0", "#B42318", "#F1B7B4"),
    NEUTRAL: ("#F5F7FA", "#475467", "#D7DCE3"),
}


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    progress = Signal(str)
    finished = Signal()


class TaskWorker(QRunnable):
    def __init__(self, function: Callable[..., Any], *, with_progress: bool = False) -> None:
        super().__init__()
        self.function = function
        self.with_progress = with_progress
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            if self.with_progress:
                result = self.function(self.signals.progress.emit)
            else:
                result = self.function()
            self.signals.succeeded.emit(result)
        except Exception as exc:  # Qt worker boundary: UI translates below.
            self.signals.failed.emit(exc)
        finally:
            self.signals.finished.emit()


class StatusCardWidget(QFrame):
    def __init__(self, card: StatusCard, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(f"card_{card.key}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        self.title = QLabel(card.title)
        self.title.setObjectName("cardTitle")
        self.headline = QLabel()
        self.headline.setObjectName("cardHeadline")
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setObjectName("cardDetail")
        self.advice = QLabel()
        self.advice.setWordWrap(True)
        self.advice.setObjectName("cardAdvice")
        layout.addWidget(self.title)
        layout.addWidget(self.headline)
        layout.addWidget(self.detail)
        layout.addWidget(self.advice)
        self.apply(card)

    def apply(self, card: StatusCard) -> None:
        self.title.setText(card.title)
        self.headline.setText(card.headline)
        self.detail.setText(card.detail)
        self.advice.setText(card.advice)
        self.advice.setVisible(bool(card.advice))
        background, foreground, border = _COLORS[card.severity]
        self.setStyleSheet(
            f"QFrame#{self.objectName()} {{ background:{background}; border:1px solid {border}; border-radius:12px; }}"
            f"QFrame#{self.objectName()} QLabel {{ border:none; background:transparent; color:#344054; }}"
            f"QFrame#{self.objectName()} QLabel#cardTitle {{ color:#667085; font-size:13px; font-weight:600; }}"
            f"QFrame#{self.objectName()} QLabel#cardHeadline {{ color:{foreground}; font-size:17px; font-weight:700; }}"
            f"QFrame#{self.objectName()} QLabel#cardDetail {{ font-size:13px; }}"
            f"QFrame#{self.objectName()} QLabel#cardAdvice {{ color:{foreground}; font-size:12px; }}"
        )


class MainWindow(QMainWindow):
    def __init__(
        self,
        service: ImportWorkflowService,
        *,
        auto_refresh: bool = True,
        auto_wizard: bool = True,
    ) -> None:
        super().__init__()
        self.service = service
        self.auto_wizard = auto_wizard
        self.status: PreflightStatus | None = None
        self._busy_kind: str | None = None
        self._workers: set[TaskWorker] = set()
        self._wizard: FirstRunWizard | None = None
        self._wizard_shown_for: set[str] = set()
        self.setObjectName("mainWindow")
        self.setWindowTitle("销售手机导入")
        self.resize(980, 760)
        self.setMinimumSize(820, 640)
        self._build_ui()
        self._apply_app_style()
        self.timer = QTimer(self)
        self.timer.setInterval(15_000)
        self.timer.timeout.connect(self.refresh)
        if auto_refresh:
            self.timer.start()
            QTimer.singleShot(0, self.refresh)

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        header = QHBoxLayout()
        heading_box = QVBoxLayout()
        heading = QLabel("销售手机导入")
        heading.setObjectName("heading")
        subtitle = QLabel("解锁手机、连接数据线，然后检查并一键导入。")
        subtitle.setObjectName("subtitle")
        heading_box.addWidget(heading)
        heading_box.addWidget(subtitle)
        header.addLayout(heading_box)
        header.addStretch(1)
        self.settings_button = QPushButton("设置 / 诊断")
        self.settings_button.setObjectName("secondaryButton")
        self.settings_button.clicked.connect(self.open_settings)
        self.refresh_button = QPushButton("重新检查")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.settings_button)
        header.addWidget(self.refresh_button)
        outer.addLayout(header)

        self.overall_frame = QFrame()
        self.overall_frame.setObjectName("overall")
        overall_layout = QVBoxLayout(self.overall_frame)
        overall_layout.setContentsMargins(20, 16, 20, 16)
        self.overall_title = QLabel("正在检查…")
        self.overall_title.setObjectName("overallTitle")
        self.overall_detail = QLabel("正在读取当前手机和交付目录状态。")
        self.overall_detail.setWordWrap(True)
        overall_layout.addWidget(self.overall_title)
        overall_layout.addWidget(self.overall_detail)
        outer.addWidget(self.overall_frame)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        cards_host = QWidget()
        self.cards_layout = QGridLayout(cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setHorizontalSpacing(12)
        self.cards_layout.setVerticalSpacing(12)
        placeholders = (
            StatusCard("phone", "手机", "正在检查…", ""),
            StatusCard("calllog", "通话记录", "正在检查…", ""),
            StatusCard("schedule", "定时备份健康度", "正在检查…", ""),
            StatusCard("recording", "通话录音", "正在检查…", ""),
            StatusCard("cloud", "坚果云交付", "正在检查…", ""),
        )
        self.card_widgets: dict[str, StatusCardWidget] = {}
        positions = ((0, 0), (0, 1), (1, 0), (1, 1), (2, 0))
        for card, position in zip(placeholders, positions, strict=True):
            widget = StatusCardWidget(card)
            self.card_widgets[card.key] = widget
            column_span = 2 if card.key == "cloud" else 1
            self.cards_layout.addWidget(widget, position[0], position[1], 1, column_span)
        self.cards_layout.setRowStretch(3, 1)
        scroll.setWidget(cards_host)
        outer.addWidget(scroll, 1)

        preview = QFrame()
        preview.setObjectName("preview")
        preview_layout = QHBoxLayout(preview)
        preview_layout.setContentsMargins(16, 12, 16, 12)
        preview_label = QLabel("本次发现")
        preview_label.setObjectName("previewTitle")
        self.preview_calls = QLabel("预计新增电话：检查中")
        self.preview_recordings = QLabel("预计新增录音：导入后统计")
        preview_layout.addWidget(preview_label)
        preview_layout.addSpacing(18)
        preview_layout.addWidget(self.preview_calls)
        preview_layout.addSpacing(18)
        preview_layout.addWidget(self.preview_recordings)
        preview_layout.addStretch(1)
        outer.addWidget(preview)

        actions = QHBoxLayout()
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("progressLabel")
        actions.addWidget(self.progress_label, 1)
        self.import_button = QPushButton("一键导入到坚果云")
        self.import_button.setObjectName("primaryButton")
        self.import_button.setMinimumHeight(46)
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.start_import)
        actions.addWidget(self.import_button)
        outer.addLayout(actions)
        self.setCentralWidget(central)

    def _apply_app_style(self) -> None:
        self.setStyleSheet(
            "QMainWindow, QWidget { background:#FFFFFF; color:#1D2939; font-family:'Microsoft YaHei UI'; font-size:13px; }"
            "QLabel#heading { font-size:25px; font-weight:700; color:#101828; }"
            "QLabel#subtitle { color:#667085; }"
            "QFrame#overall { border-radius:12px; background:#F5F7FA; border:1px solid #D7DCE3; }"
            "QLabel#overallTitle { font-size:20px; font-weight:700; }"
            "QLabel#previewTitle { font-weight:700; }"
            "QFrame#preview { background:#F8FAFC; border:1px solid #EAECF0; border-radius:10px; }"
            "QPushButton { border-radius:8px; padding:9px 15px; font-weight:600; }"
            "QPushButton#secondaryButton { background:#FFFFFF; border:1px solid #D0D5DD; color:#344054; }"
            "QPushButton#secondaryButton:hover { background:#F9FAFB; }"
            "QPushButton#primaryButton { background:#175CD3; color:#FFFFFF; border:1px solid #175CD3; font-size:15px; }"
            "QPushButton#primaryButton:hover { background:#1849A9; }"
            "QPushButton#primaryButton:disabled { background:#D0D5DD; border-color:#D0D5DD; color:#FFFFFF; }"
            "QLabel#progressLabel { color:#475467; }"
        )

    @Slot()
    def refresh(self) -> None:
        if self._busy_kind is not None:
            return
        self._set_busy("preflight")
        self.progress_label.setText("正在检查手机和备份…")
        worker = TaskWorker(self.service.preflight)
        worker.signals.succeeded.connect(self.apply_preflight)
        worker.signals.failed.connect(self._show_worker_error)
        worker.signals.finished.connect(lambda: self._worker_finished(worker))
        self._workers.add(worker)
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def apply_preflight(self, status: PreflightStatus) -> None:
        self.status = status
        background, foreground, border = _COLORS[status.overall]
        self.overall_frame.setStyleSheet(
            f"QFrame#overall {{ background:{background}; border:1px solid {border}; border-radius:12px; }}"
            f"QFrame#overall QLabel {{ background:transparent; color:{foreground}; border:none; }}"
        )
        self.overall_title.setText(status.overall_title)
        self.overall_detail.setText(status.overall_detail)
        for card in status.cards:
            self.card_widgets[card.key].apply(card)
        self.preview_calls.setText(
            f"预计新增电话：{status.estimated_new_calls}"
            if status.estimated_new_calls is not None else "预计新增电话：导入后统计"
        )
        self.preview_recordings.setText("预计新增录音：导入后统计")
        self.import_button.setEnabled(status.can_import and self._busy_kind is None)
        self.progress_label.setText("就绪检查已完成")
        if self._wizard is not None:
            self._wizard.update_preflight(status)
        if (
            self.auto_wizard
            and status.requires_first_run
            and status.device_id
            and status.device_id not in self._wizard_shown_for
        ):
            self._wizard_shown_for.add(status.device_id)
            QTimer.singleShot(0, self.open_first_run)

    @Slot()
    def start_import(self) -> None:
        if self._busy_kind is not None or not self.status or not self.status.can_import:
            return
        self._set_busy("import")
        self.progress_label.setText("正在开始导入…")
        worker = TaskWorker(self.service.run_import, with_progress=True)
        worker.signals.progress.connect(self.progress_label.setText)
        worker.signals.succeeded.connect(self._show_import_result)
        worker.signals.failed.connect(self._show_worker_error)
        worker.signals.finished.connect(lambda: self._worker_finished(worker, refresh_after=True))
        self._workers.add(worker)
        QThreadPool.globalInstance().start(worker)

    @Slot()
    def open_first_run(self) -> None:
        if self.status is None or self.status.device_id is None or self._wizard is not None:
            return
        wizard = FirstRunWizard(self.status, self.service, self)
        wizard.recheck_requested.connect(self.refresh)
        self._wizard = wizard
        wizard.finished.connect(lambda _: self._wizard_finished(wizard))
        wizard.open()

    def _wizard_finished(self, wizard: "FirstRunWizard") -> None:
        if self._wizard is wizard:
            self._wizard = None
        self.refresh()

    @Slot()
    def open_settings(self) -> None:
        if self.status is None:
            QMessageBox.information(self, "请稍候", "首次检查完成后即可打开设置与诊断。")
            return
        dialog = SettingsDialog(self.status, self.service, self)
        dialog.cloud_root_changed.connect(self.refresh)
        dialog.exec()

    @Slot(object)
    def _show_import_result(self, result: ImportResult) -> None:
        dialog = ImportResultDialog(result, self)
        dialog.exec()

    @Slot(object)
    def _show_worker_error(self, error: Exception) -> None:
        if isinstance(error, HumanActionRequired):
            message = f"{error.title}\n\n{error.action}"
            if error.technical_detail:
                message += f"\n\n技术详情（已脱敏）：\n{error.technical_detail}"
        else:
            message = "操作没有完成。请保持手机解锁并连接，然后重新检查。"
        QMessageBox.warning(self, "销售手机导入", message)

    def _set_busy(self, kind: str | None) -> None:
        self._busy_kind = kind
        busy = kind is not None
        self.refresh_button.setEnabled(not busy)
        self.settings_button.setEnabled(kind != "import")
        self.import_button.setEnabled(not busy and bool(self.status and self.status.can_import))

    def _worker_finished(self, worker: TaskWorker, *, refresh_after: bool = False) -> None:
        self._workers.discard(worker)
        self._set_busy(None)
        if refresh_after:
            QTimer.singleShot(0, self.refresh)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.timer.stop()
        if self._busy_kind == "import":
            answer = QMessageBox.question(
                self,
                "导入仍在进行",
                "当前导入仍在安全处理本地数据。建议等待完成后再关闭。仍要关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)


class FirstRunWizard(QWizard):
    recheck_requested = Signal()

    def __init__(self, status: PreflightStatus, service: ImportWorkflowService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status = status
        self.service = service
        self.selected_cloud_root: Path | None = None
        self.setWindowTitle("首次设置 · 销售手机导入")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(620, 470)
        self._build_pages()

    def _build_pages(self) -> None:
        device_page = QWizardPage()
        device_page.setTitle("识别手机")
        device_layout = QVBoxLayout(device_page)
        device_name = QLabel(self.status.device_name or "Android 手机")
        device_name.setStyleSheet("font-size:20px; font-weight:700;")
        device_layout.addWidget(device_name)
        device_layout.addWidget(QLabel("文件传输 / MTP：正常"))
        device_layout.addStretch(1)
        self.addPage(device_page)

        assignment_page = QWizardPage()
        assignment_page.setTitle("绑定销售")
        assignment_page.setSubTitle("身份只保存在这台电脑，不会写回手机。")
        form = QFormLayout(assignment_page)
        self.salesperson_id = QLineEdit()
        self.salesperson_id.setPlaceholderText("例如：S001")
        self.salesperson_name = QLineEdit()
        self.salesperson_name.setPlaceholderText("例如：张三")
        self.historical_all = QCheckBox("这部手机当前可见的历史通话都属于该销售")
        self.effective_from = QDateTimeEdit()
        self.effective_from.setCalendarPopup(True)
        self.effective_from.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.effective_from.setDateTime(datetime.now())
        self.historical_all.toggled.connect(lambda checked: self.effective_from.setEnabled(not checked))
        form.addRow("销售编号", self.salesperson_id)
        form.addRow("销售姓名", self.salesperson_name)
        form.addRow("", self.historical_all)
        form.addRow("归属开始时间", self.effective_from)
        self.addPage(assignment_page)

        calllog_page = QWizardPage()
        calllog_page.setTitle("准备通话记录")
        calllog_layout = QVBoxLayout(calllog_page)
        self.calllog_state = QLabel()
        self.calllog_state.setWordWrap(True)
        calllog_layout.addWidget(self.calllog_state)
        instructions = QLabel(
            "如尚未准备好，请在手机：\n\n"
            "1. 打开 SMS Backup & Restore\n"
            "2. 创建本地备份，只选择 Call logs\n"
            "3. 保存到手机公共/shared storage\n"
            "4. 完成后回到这里重新检查"
        )
        instructions.setWordWrap(True)
        calllog_layout.addWidget(instructions)
        recheck = QPushButton("重新检查")
        recheck.clicked.connect(self.recheck_requested)
        calllog_layout.addWidget(recheck, 0, Qt.AlignmentFlag.AlignLeft)
        calllog_layout.addStretch(1)
        self.addPage(calllog_page)

        cloud_page = QWizardPage()
        cloud_page.setTitle("确认坚果云交付目录")
        cloud_layout = QVBoxLayout(cloud_page)
        self.cloud_state = QLabel()
        self.cloud_state.setWordWrap(True)
        cloud_layout.addWidget(self.cloud_state)
        choose = QPushButton("选择坚果云同步目录")
        choose.clicked.connect(self._choose_cloud_root)
        cloud_layout.addWidget(choose, 0, Qt.AlignmentFlag.AlignLeft)
        cloud_layout.addStretch(1)
        self.addPage(cloud_page)

        finish_page = QWizardPage()
        finish_page.setTitle("设置完成")
        finish_layout = QVBoxLayout(finish_page)
        finish_label = QLabel("以后只需解锁手机、插数据线并点击“一键导入到坚果云”。")
        finish_label.setWordWrap(True)
        finish_label.setStyleSheet("font-size:17px; font-weight:600;")
        finish_layout.addWidget(finish_label)
        finish_layout.addStretch(1)
        self.addPage(finish_page)
        self.update_preflight(self.status)

    def update_preflight(self, status: PreflightStatus) -> None:
        self.status = status
        calllog = status.card("calllog")
        self.calllog_state.setText(f"{calllog.headline}\n{calllog.detail}")
        if status.cloud_root:
            self.cloud_state.setText("坚果云交付目录已经配置并验证，可以直接继续。")
        elif self.selected_cloud_root:
            self.cloud_state.setText(f"已选择：{self.selected_cloud_root}")
        else:
            self.cloud_state.setText("请选择坚果云客户端中已经同步的根目录。程序会在其中创建专用的“销售通话数据”目录。")

    def _choose_cloud_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择坚果云同步目录")
        if selected:
            self.selected_cloud_root = Path(selected)
            self.cloud_state.setText(f"已选择：{self.selected_cloud_root}")

    def accept(self) -> None:
        try:
            if self.status.device_id is None:
                raise HumanActionRequired("手机已断开", "请重新连接手机并重新检查。")
            if self.status.cloud_root is None:
                if self.selected_cloud_root is None:
                    raise HumanActionRequired("尚未选择坚果云目录", "请选择坚果云客户端中已经同步的根目录。")
                self.service.configure_cloud_sync_root(self.selected_cloud_root)
            effective_from = self.effective_from.dateTime().toPython().astimezone().isoformat()
            self.service.assign_salesperson(
                device_id=self.status.device_id,
                salesperson_id=self.salesperson_id.text(),
                salesperson_name=self.salesperson_name.text(),
                historical_all_belongs=self.historical_all.isChecked(),
                effective_from=effective_from,
            )
        except HumanActionRequired as exc:
            QMessageBox.warning(self, exc.title, exc.action)
            return
        super().accept()


class SettingsDialog(QDialog):
    cloud_root_changed = Signal()

    def __init__(self, status: PreflightStatus, service: ImportWorkflowService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status = status
        self.service = service
        self.setWindowTitle("设置 / 诊断")
        self.resize(650, 430)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("当前设备", QLabel(status.device_name or "未连接"))
        salesperson = status.salesperson_name or "未绑定"
        if status.salesperson_id:
            salesperson = f"{status.salesperson_name}（{status.salesperson_id}）"
        form.addRow("当前销售", QLabel(salesperson))
        form.addRow("坚果云交付目录", _selectable_label(str(status.cloud_root) if status.cloud_root else "未配置"))
        form.addRow("本地数据目录", _selectable_label(str(status.data_root) if status.data_root else ""))
        form.addRow("最近导入", QLabel(_format_time(status.latest_import_at) if status.latest_import_at else "尚无"))
        form.addRow("最近 CallLog", QLabel(_format_time(status.backup_timestamp) if status.backup_timestamp else "无法确定时间"))
        form.addRow("备份更新证据", QLabel("已观察到后续更新" if status.scheduled_backup_evidence == "OBSERVED_UPDATE" else "尚待实际验证"))
        layout.addLayout(form)
        actions = QHBoxLayout()
        choose = QPushButton("重新选择坚果云目录")
        choose.clicked.connect(self._choose_cloud)
        open_data = QPushButton("打开本地数据目录")
        open_data.clicked.connect(self._open_data)
        diagnostic = QPushButton("保存脱敏诊断")
        diagnostic.clicked.connect(self._save_diagnostic)
        actions.addWidget(choose)
        actions.addWidget(open_data)
        actions.addWidget(diagnostic)
        layout.addLayout(actions)
        layout.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

    def _choose_cloud(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择坚果云同步目录")
        if not selected:
            return
        try:
            self.service.configure_cloud_sync_root(Path(selected))
        except HumanActionRequired as exc:
            QMessageBox.warning(self, exc.title, exc.action)
            return
        self.cloud_root_changed.emit()
        QMessageBox.information(self, "已保存", "坚果云交付目录已确认并保存。")

    def _open_data(self) -> None:
        if self.status.data_root:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.status.data_root)))

    def _save_diagnostic(self) -> None:
        suggested = str((self.status.data_root or Path.home()) / "desktop-diagnostic.json")
        destination, _ = QFileDialog.getSaveFileName(self, "保存脱敏诊断", suggested, "JSON (*.json)")
        if not destination:
            return
        path = self.service.write_safe_diagnostic(self.status, Path(destination))
        QMessageBox.information(self, "诊断已保存", f"已保存隐私最小化诊断：\n{path}")


class ImportResultDialog(QDialog):
    def __init__(self, result: ImportResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导入结果")
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        title = QLabel("导入完成，有提醒" if result.has_warnings else "导入完成")
        title.setStyleSheet("font-size:23px; font-weight:700; color:#18794E;")
        layout.addWidget(title)
        form = QFormLayout()
        form.addRow("新增电话", QLabel(str(result.new_calls)))
        form.addRow("新增录音", QLabel(str(result.new_recordings)))
        form.addRow("无录音电话", QLabel(str(result.calls_without_recording)))
        form.addRow("已关联录音", QLabel(str(result.linked_recordings)))
        form.addRow("未匹配录音", QLabel(str(result.unmatched_recordings)))
        form.addRow("歧义关联", QLabel(str(result.ambiguous_recordings)))
        form.addRow("历史重复", QLabel(f"{result.historical_duplicates}，已自动跳过"))
        form.addRow("CallLog 状态", QLabel(_freshness_text(result.freshness)))
        form.addRow("坚果云交付", QLabel(result.handoff_status))
        layout.addLayout(form)
        toggle = QPushButton("查看技术详情")
        details = QTextEdit()
        details.setReadOnly(True)
        details.setVisible(False)
        details.setPlainText("\n".join(f"{key}: {value}" for key, value in sorted(result.technical_summary.items())))
        toggle.clicked.connect(lambda: details.setVisible(not details.isVisible()))
        layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(details)
        close = QPushButton("完成")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)


def _selectable_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)
    return label


def _format_time(value: str | None) -> str:
    if not value:
        return "无法确定"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return "无法确定"
    today = datetime.now().astimezone().date()
    prefix = "今天" if parsed.date() == today else parsed.strftime("%Y-%m-%d")
    return f"{prefix} {parsed.strftime('%H:%M')}"


def _freshness_text(value: str) -> str:
    return {"FRESH": "最新", "STALE": "较旧", "UNKNOWN": "无法确认"}.get(value, "未完成")


def create_application() -> QApplication:
    application = QApplication.instance() or QApplication([])
    application.setApplicationName("销售手机导入")
    application.setOrganizationName("SalesMobileIngest")
    family = "Microsoft YaHei UI"
    windows_dir = Path(os.environ.get("WINDIR", "C:/Windows"))
    for candidate in (windows_dir / "Fonts" / "msyh.ttc", windows_dir / "Fonts" / "simsun.ttc"):
        if not candidate.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(candidate))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            family = families[-1]
            break
    application.setFont(QFont(family, 10))
    return application
