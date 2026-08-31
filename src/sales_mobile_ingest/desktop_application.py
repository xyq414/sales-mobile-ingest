from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .cloud_handoff import sanitize_windows_component, validate_cloud_handoff_root
from .config import (
    ConfigError,
    resolve_cloud_handoff_root,
    resolve_data_root,
    update_local_config,
)
from .contract import iso_now
from .service import CallLogPreflightSummary, IngestSummary, Ingestor


BLOCKER = "blocker"
WARNING = "warning"
READY = "ready"
NEUTRAL = "neutral"


class DesktopBackend(Protocol):
    data_root: Path

    def discover_devices_for_desktop(self) -> list[dict[str, Any]]: ...
    def preflight_calllog_exports(self, *, observed_at: str | None = None) -> CallLogPreflightSummary: ...
    def assign_device(self, **kwargs: Any) -> dict[str, Any]: ...
    def ingest_once(
        self, limit: int | None = None, progress: Callable[[str], None] | None = None
    ) -> IngestSummary: ...
    def desktop_business_snapshot(self) -> dict[str, Any]: ...
    def remember_desktop_import_run(self, summary: dict[str, Any]) -> None: ...
    def latest_desktop_import_run(self) -> dict[str, Any] | None: ...


class HumanActionRequired(RuntimeError):
    def __init__(self, title: str, action: str, *, technical_detail: str | None = None) -> None:
        super().__init__(title)
        self.title = title
        self.action = action
        self.technical_detail = privacy_minimal_text(technical_detail or "")


@dataclass(frozen=True)
class StatusCard:
    key: str
    title: str
    headline: str
    detail: str
    advice: str = ""
    severity: str = NEUTRAL


@dataclass(frozen=True)
class PreflightStatus:
    overall: str
    overall_title: str
    overall_detail: str
    can_import: bool
    requires_first_run: bool
    cards: tuple[StatusCard, ...]
    device_id: str | None = None
    device_name: str | None = None
    salesperson_id: str | None = None
    salesperson_name: str | None = None
    earliest_call_at: str | None = None
    calllog_status: str = "NOT_RUN"
    calllog_freshness: str = "UNKNOWN"
    backup_timestamp: str | None = None
    scheduled_backup_evidence: str = "UNVERIFIED"
    estimated_new_calls: int | None = None
    cloud_root: Path | None = None
    data_root: Path | None = None
    latest_import_at: str | None = None
    technical_codes: tuple[str, ...] = field(default_factory=tuple)

    def card(self, key: str) -> StatusCard:
        return next(card for card in self.cards if card.key == key)


@dataclass(frozen=True)
class ImportResult:
    completed_at: str
    new_calls: int
    new_recordings: int
    calls_without_recording: int
    linked_recordings: int
    unmatched_recordings: int
    ambiguous_recordings: int
    historical_duplicates: int
    freshness: str
    handoff_status: str
    has_warnings: bool
    technical_summary: dict[str, Any]


class ImportWorkflowService:
    """Thin desktop application boundary over the canonical ingest service."""

    def __init__(
        self,
        backend: DesktopBackend | None = None,
        *,
        cloud_root_resolver: Callable[[], Path | None] = resolve_cloud_handoff_root,
        config_updater: Callable[[dict[str, Any]], None] = update_local_config,
        now: Callable[[], str] = iso_now,
    ) -> None:
        self.backend = backend or Ingestor(resolve_data_root())
        self.cloud_root_resolver = cloud_root_resolver
        self.config_updater = config_updater
        self.now = now
        self._last_preflight: PreflightStatus | None = None
        self._import_lock = threading.Lock()

    def preflight(self) -> PreflightStatus:
        technical_codes: list[str] = []
        try:
            devices = self.backend.discover_devices_for_desktop()
        except Exception as exc:
            raise HumanActionRequired(
                "无法检查手机连接",
                "请确认手机已解锁并选择“文件传输 / MTP”，然后重新检查。",
                technical_detail=str(exc),
            ) from exc

        device_card, device, requires_first_run = self._device_card(devices)
        cards = [device_card]
        calllog = CallLogPreflightSummary(status="NOT_RUN")
        if device is not None and device.get("mtp_usable") and len(devices) == 1:
            try:
                calllog = self.backend.preflight_calllog_exports(observed_at=self.now())
            except Exception as exc:
                technical_codes.append("CALLLOG_PREFLIGHT_ERROR")
                calllog = CallLogPreflightSummary(status="MALFORMED", parse_status="CHECK_FAILED", failures=1)
                technical_codes.append(privacy_minimal_text(str(exc)))
        cards.append(self._calllog_card(calllog, device is not None))
        cards.append(self._schedule_card(calllog, device is not None))
        cards.append(self._recording_card(device))
        cloud_card, cloud_root, cloud_code = self._cloud_card()
        cards.append(cloud_card)
        if cloud_code:
            technical_codes.append(cloud_code)

        has_blocker = any(card.severity == BLOCKER for card in cards)
        has_warning = any(card.severity == WARNING for card in cards)
        if has_blocker:
            overall = BLOCKER
            overall_title = "暂时无法完整导入"
            overall_detail = "请先完成下方红色项目，然后重新检查。"
        elif has_warning:
            overall = WARNING
            overall_title = "可以导入，但有提醒"
            overall_detail = "提醒不会阻止通话记录导入；建议按提示补齐。"
        else:
            overall = READY
            overall_title = "可以导入"
            overall_detail = "手机、通话记录和坚果云交付目录均已就绪。"

        latest = self.backend.latest_desktop_import_run()
        status = PreflightStatus(
            overall=overall,
            overall_title=overall_title,
            overall_detail=overall_detail,
            can_import=not has_blocker,
            requires_first_run=requires_first_run,
            cards=tuple(cards),
            device_id=str(device["device_id"]) if device else None,
            device_name=str(device.get("display_name") or device.get("model") or "Android 手机") if device else None,
            salesperson_id=str(device.get("salesperson_id")) if device and device.get("salesperson_id") else None,
            salesperson_name=str(device.get("salesperson_name")) if device and device.get("salesperson_name") else None,
            earliest_call_at=calllog.earliest_call_at,
            calllog_status=calllog.status,
            calllog_freshness=calllog.freshness,
            backup_timestamp=calllog.backup_timestamp,
            scheduled_backup_evidence=calllog.scheduled_backup_evidence,
            estimated_new_calls=calllog.estimated_new_calls,
            cloud_root=cloud_root,
            data_root=self.backend.data_root,
            latest_import_at=str(latest.get("completed_at")) if latest and latest.get("completed_at") else None,
            technical_codes=tuple(item for item in technical_codes if item),
        )
        self._last_preflight = status
        return status

    def assign_salesperson(
        self,
        *,
        device_id: str,
        salesperson_id: str,
        salesperson_name: str,
        historical_all_belongs: bool,
        effective_from: str | None,
    ) -> dict[str, Any]:
        salesperson_id = salesperson_id.strip()
        salesperson_name = salesperson_name.strip()
        if not salesperson_id or not salesperson_name:
            raise HumanActionRequired("销售身份未填写完整", "请填写销售编号和销售姓名。")
        if historical_all_belongs:
            cached = self._last_preflight
            if cached is None or cached.device_id != device_id or not cached.earliest_call_at:
                raise HumanActionRequired(
                    "还不能确认历史归属",
                    "请先生成并重新检查通话记录备份，或取消“历史通话都属于该销售”并指定开始时间。",
                )
            boundary = cached.earliest_call_at
        else:
            if not effective_from:
                raise HumanActionRequired("归属开始时间未填写", "请指定这部手机从何时开始归属于该销售。")
            boundary = effective_from
        return self.backend.assign_device(
            device_id=device_id,
            salesperson_id=salesperson_id,
            salesperson_name=salesperson_name,
            effective_from=boundary,
        )

    def configure_cloud_sync_root(self, selected_root: Path, *, folder_name: str = "销售通话数据") -> Path:
        try:
            sync_root = validate_cloud_handoff_root(self.backend.data_root, selected_root)
            handoff = sync_root / sanitize_windows_component(folder_name)
            handoff.mkdir(exist_ok=True)
            validated = validate_cloud_handoff_root(self.backend.data_root, handoff)
            self._prove_writable(validated)
        except (ConfigError, OSError, ValueError) as exc:
            raise HumanActionRequired(
                "坚果云目录不可用",
                "请选择坚果云客户端中已经同步、可写的根目录。",
                technical_detail=str(exc),
            ) from exc
        self.config_updater({
            "cloud_handoff_root": str(validated),
            "cloud_handoff_confirmation": "operator_selected_sync_root",
            "cloud_handoff_confirmed_at": self.now(),
        })
        return validated

    def run_import(self, progress: Callable[[str], None] | None = None) -> ImportResult:
        if not self._import_lock.acquire(blocking=False):
            raise HumanActionRequired("导入正在进行", "请等待当前导入完成，不要重复点击。")
        try:
            self._progress(progress, "正在执行最终检查…")
            status = self.preflight()
            if not status.can_import:
                raise HumanActionRequired("当前还不能导入", "请先完成首页红色项目，然后重新检查。")
            before = self.backend.desktop_business_snapshot()
            summary = self.backend.ingest_once(progress=progress)
            self._progress(progress, "正在验证交付结果…")
            after = self.backend.desktop_business_snapshot()
            result = self._build_import_result(summary, before, after)
            self.backend.remember_desktop_import_run({
                "completed_at": result.completed_at,
                "new_calls": result.new_calls,
                "new_recordings": result.new_recordings,
                "calls_without_recording": result.calls_without_recording,
                "linked_recordings": result.linked_recordings,
                "unmatched_recordings": result.unmatched_recordings,
                "ambiguous_recordings": result.ambiguous_recordings,
                "handoff_status": result.handoff_status,
            })
            return result
        except HumanActionRequired:
            raise
        except Exception as exc:
            raise HumanActionRequired(
                "导入没有完成",
                "手机上的文件不会被修改。请确认手机仍已解锁并连接，然后重新检查后再试。",
                technical_detail=str(exc),
            ) from exc
        finally:
            self._import_lock.release()

    def write_safe_diagnostic(self, status: PreflightStatus, destination: Path | None = None) -> Path:
        directory = self.backend.data_root / "diagnostics" / "desktop"
        directory.mkdir(parents=True, exist_ok=True)
        if destination is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            destination = directory / f"desktop-diagnostic-{stamp}.json"
        payload = {
            "diagnostic_version": "desktop-pilot-safe-diagnostic/v1",
            "created_at": self.now(),
            "overall": status.overall,
            "can_import": status.can_import,
            "device_connected": status.device_id is not None,
            "device_assigned": status.salesperson_name is not None,
            "calllog_status": status.calllog_status,
            "calllog_freshness": status.calllog_freshness,
            "scheduled_backup_evidence": status.scheduled_backup_evidence,
            "cloud_root_configured": status.cloud_root is not None,
            "cards": {card.key: card.severity for card in status.cards},
            "technical_codes": [privacy_minimal_text(item) for item in status.technical_codes],
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".desktop-diagnostic-", suffix=".json", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return destination

    def _device_card(self, devices: list[dict[str, Any]]) -> tuple[StatusCard, dict[str, Any] | None, bool]:
        if not devices:
            return StatusCard(
                "phone", "手机", "未连接手机", "请连接并解锁销售手机。",
                "在手机 USB 选项中选择“文件传输 / MTP”，再点击重新检查。", BLOCKER,
            ), None, False
        if len(devices) > 1:
            return StatusCard(
                "phone", "手机", "检测到多部手机", "当前 Pilot 每次只处理一部销售手机。",
                "请只保留要导入的那部手机连接。", BLOCKER,
            ), None, False
        device = devices[0]
        name = str(device.get("display_name") or device.get("model") or "Android 手机")
        if not device.get("mtp_usable"):
            return StatusCard(
                "phone", "手机", name, "已检测到手机，但文件传输不可用。",
                "请解锁手机，并在 USB 选项中选择“文件传输 / MTP”。", BLOCKER,
            ), device, False
        if device.get("assignment_status") != "ASSIGNED":
            return StatusCard(
                "phone", "手机", f"{name} · 已连接", "这是一部尚未绑定销售的手机。",
                "完成一次简短初始化后，以后会自动识别。", BLOCKER,
            ), device, True
        salesperson_name = str(device.get("salesperson_name") or "")
        return StatusCard(
            "phone", "手机", f"{name} · 已连接", f"销售：{salesperson_name}", severity=READY
        ), device, False

    @staticmethod
    def _calllog_card(calllog: CallLogPreflightSummary, phone_connected: bool) -> StatusCard:
        if not phone_connected or calllog.status == "NOT_RUN":
            return StatusCard("calllog", "通话记录", "等待手机连接", "连接后会自动检查公共备份文件。")
        if calllog.status == "MISSING_DIRECTORY":
            return StatusCard(
                "calllog", "通话记录", "未找到通话记录备份",
                "请确认手机已使用 SMS Backup & Restore 创建 Call logs 本地备份。",
                "备份需保存到手机公共/shared storage。", BLOCKER,
            )
        if calllog.status == "NO_XML":
            return StatusCard(
                "calllog", "通话记录", "已找到备份目录，但还没有通话记录文件",
                "请在手机执行一次“立即备份”，并只选择 Call logs。",
                "完成后回到电脑点击重新检查。", BLOCKER,
            )
        if calllog.status in {"MALFORMED", "COUNT_MISMATCH"}:
            detail = "文件中的记录数量不完整。" if calllog.status == "COUNT_MISMATCH" else "文件无法通过安全解析检查。"
            return StatusCard(
                "calllog", "通话记录", "通话记录文件无法安全读取", detail,
                "请在手机重新生成一次 Call logs 备份。", BLOCKER,
            )
        if calllog.status == "STALE":
            return StatusCard(
                "calllog", "通话记录", "通话记录备份较旧",
                "当前仍可导入已有历史，但最近电话可能尚未包含。",
                "建议在 SMS Backup & Restore 中立即备份 Call logs。", WARNING,
            )
        if calllog.status == "UNKNOWN":
            return StatusCard(
                "calllog", "通话记录", "通话记录可读取，但无法确认是否最新",
                "文件结构和内容可以安全解析，备份时间没有可靠证据。",
                "如刚发生过电话，建议先在手机立即备份 Call logs。", WARNING,
            )
        return StatusCard(
            "calllog", "通话记录", "通话记录：正常", "已找到并安全读取 SMS Backup CallLog。", severity=READY
        )

    @staticmethod
    def _schedule_card(calllog: CallLogPreflightSummary, phone_connected: bool) -> StatusCard:
        if not phone_connected or calllog.status in {"NOT_RUN", "MISSING_DIRECTORY", "NO_XML", "MALFORMED", "COUNT_MISMATCH"}:
            return StatusCard("schedule", "定时备份健康度", "当前无法判断", "需要先有一份可读取的 Call logs 备份。")
        if calllog.status == "STALE":
            return StatusCard(
                "schedule", "定时备份健康度", "近期可能未更新",
                "当前备份已经较旧，无法证明定时备份仍在持续工作。", severity=WARNING,
            )
        if calllog.scheduled_backup_evidence == "OBSERVED_UPDATE":
            return StatusCard(
                "schedule", "定时备份健康度", "已观察到后续备份更新",
                "桌面端在不同时间观察到了更新的公共备份产物。", severity=READY,
            )
        return StatusCard(
            "schedule", "定时备份健康度", "尚待实际验证",
            "目前只有可用快照，还没有足够的后续更新历史；这不会阻止手动导入。", severity=WARNING,
        )

    @staticmethod
    def _recording_card(device: dict[str, Any] | None) -> StatusCard:
        if device is None:
            return StatusCard("recording", "通话录音", "等待手机连接", "录音是可选项，不影响通话记录导入。")
        if device.get("recording_check_status") == "DEFERRED_TO_IMPORT":
            return StatusCard(
                "recording", "通话录音", "将在导入时定点检查",
                "为避免慢速 MTP 全盘扫描，程序只在导入时读取厂商录音候选目录。",
                "录音是可选项，不会阻止通话记录导入。", WARNING,
            )
        if not device.get("recording_directory_found"):
            return StatusCard(
                "recording", "通话录音", "未发现通话录音",
                "通话记录仍可导入；未接、拒接或没有录音的电话不会丢失。", severity=WARNING,
            )
        if int(device.get("recording_file_count") or 0) == 0:
            return StatusCard(
                "recording", "通话录音", "已发现录音目录", "当前没有发现录音文件；这不是错误。", severity=READY
            )
        return StatusCard(
            "recording", "通话录音", "通话录音：正常", "已发现通话录音目录。", severity=READY
        )

    def _cloud_card(self) -> tuple[StatusCard, Path | None, str | None]:
        try:
            root = self.cloud_root_resolver()
        except Exception:
            root = None
        if root is None:
            return StatusCard(
                "cloud", "坚果云交付", "尚未配置坚果云交付目录",
                "第一次请选择坚果云客户端中已同步的目录。", severity=BLOCKER,
            ), None, "CLOUD_HANDOFF_ROOT_UNCONFIGURED"
        try:
            validated = validate_cloud_handoff_root(self.backend.data_root, root)
            self._prove_writable(validated)
        except Exception:
            return StatusCard(
                "cloud", "坚果云交付", "坚果云交付目录不可用",
                "目录不存在、不可写，或与本地数据目录不安全地重叠。",
                "请重新选择坚果云客户端中的同步目录。", BLOCKER,
            ), None, "CLOUD_HANDOFF_ROOT_INVALID"
        return StatusCard(
            "cloud", "坚果云交付", "坚果云交付：正常",
            "已确认本机同步目录可写；这里只证明已写入同步目录，不代表远端已同步。", severity=READY,
        ), validated, None

    @staticmethod
    def _prove_writable(root: Path) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".sales-mobile-ingest-write-check-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _build_import_result(
        self, summary: IngestSummary, before: dict[str, Any], after: dict[str, Any]
    ) -> ImportResult:
        before_calls = set(before.get("calls", set()))
        after_calls = set(after.get("calls", set()))
        new_calls = after_calls - before_calls
        before_recordings = set(before.get("recordings", set()))
        after_recordings = set(after.get("recordings", set()))
        new_recordings = after_recordings - before_recordings
        links = after.get("links", {}) if isinstance(after.get("links", {}), dict) else {}
        matched_call_ids = {
            str(value.get("call_id"))
            for value in links.values()
            if isinstance(value, dict) and value.get("status") in {"EXACT", "HIGH_CONFIDENCE"} and value.get("call_id")
        }
        new_link_values = [
            value for recording_id, value in links.items()
            if recording_id in new_recordings and isinstance(value, dict)
        ]
        unmatched = sum(1 for value in new_link_values if value.get("status") == "NO_MATCH")
        ambiguous = sum(1 for value in new_link_values if value.get("status") == "AMBIGUOUS")
        linked = sum(1 for value in new_link_values if value.get("status") in {"EXACT", "HIGH_CONFIDENCE"})
        handoff_ok = (
            summary.calllog_failures == 0
            and summary.calllog_snapshot_status in {"FRESH", "STALE", "UNKNOWN"}
            and summary.call_fact_handoff_status == "CALL_FACT_HANDOFF_READY"
            and summary.call_fact_failures == 0
            and summary.cloud_packages_failures == 0
            and summary.cloud_packages_conflicts == 0
        )
        if not handoff_ok:
            raise HumanActionRequired(
                "本地导入已完成，但交付验证未通过",
                "本地数据已安全保留。请检查坚果云目录后重新执行一键导入，系统会自动去重。",
                technical_detail=json.dumps(summary.as_dict(), ensure_ascii=False),
            )
        wrote = bool(
            summary.call_facts_published
            or summary.call_facts_updated
            or summary.cloud_packages_published
        )
        handoff_status = "已写入坚果云同步目录" if wrote else "坚果云同步目录无新增内容"
        has_warnings = bool(
            summary.failures
            or summary.event_failures
            or summary.cloud_packages_blocked
            or summary.cloud_packages_immutable_enrichment_pending
            or summary.calllog_snapshot_status in {"STALE", "UNKNOWN"}
            or unmatched
            or ambiguous
        )
        return ImportResult(
            completed_at=self.now(),
            new_calls=len(new_calls),
            new_recordings=len(new_recordings),
            calls_without_recording=len(new_calls - matched_call_ids),
            linked_recordings=linked,
            unmatched_recordings=unmatched,
            ambiguous_recordings=ambiguous,
            historical_duplicates=summary.phone_calls_existing + summary.duplicates,
            freshness=summary.calllog_snapshot_status,
            handoff_status=handoff_status,
            has_warnings=has_warnings,
            technical_summary=summary.as_dict(),
        )

    @staticmethod
    def _progress(callback: Callable[[str], None] | None, message: str) -> None:
        if callback:
            callback(message)


_PHONE_LIKE = re.compile(r"(?<!\d)\+?\d(?:[\s().-]*\d){6,}(?!\d)")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\r\n\"']+")
_XML_CONTENT = re.compile(r"<[^>]+>")
_ENCODED_ARGUMENT = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/])")


def privacy_minimal_text(value: str) -> str:
    """Remove customer-number, path and raw-markup shaped content from UI diagnostics."""
    text = _PHONE_LIKE.sub("[号码已隐藏]", str(value))
    text = _WINDOWS_PATH.sub("[路径已隐藏]", text)
    text = _XML_CONTENT.sub("[XML内容已隐藏]", text)
    text = _ENCODED_ARGUMENT.sub("[编码参数已隐藏]", text)
    return text[:1000]
