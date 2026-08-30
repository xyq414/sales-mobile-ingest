# 手机一次性初始化与 Redmi 物理验收

状态：2026-08-30 runbook ready；Redmi Note 12 5G / Redmi Note 15 均 `PHYSICAL_DEVICE_PENDING`。本文是未来真机输入，不是已执行证据。

## 一次性手机初始化 contract

对每部销售手机分别执行并记录设备型号/OS 版本；不要复制另一型号的结论。

1. 从可信应用商店安装 SyncTech `SMS Backup & Restore`。本项目不自动安装 App。
2. 只为本项目配置 **Call logs** 本地 backup；不要把 SMS/MMS/RCS 正文纳入项目范围。
3. 授予 App 完成 CallLog-only export 实际要求的最小权限。权限页面/措辞随 Android 版本变化，按当前真实 UI 留 evidence；不启用项目 Android companion APK。
4. 将本地 export 目录设为 Windows MTP 可读取的 shared-storage `SMSBackupRestore`（或 App 实际创建且 probe 发现的公共目录）。不得写入 App-private 或需 ADB 的位置。
5. 手动创建一次 CallLog-only XML，确认 root/row schema 后再配置 recurring schedule。
6. 配置业务允许的 recurring interval。Android 13+ 若真实 UI 要求 alarms/reminders 权限，显式授予并记录；不得仅凭文档声称计划已运行。
7. Xiaomi/HyperOS 如真实 UI 提供 autostart、后台运行或电池策略，将该 App 设为允许自启动/不受限，并记录实际菜单路径和 OS build。菜单不存在时记录 NOT_PRESENT，不猜测。
8. 正常日常路径应为：销售正常使用 → App scheduled export → 解锁手机 → USB 选择文件传输/MTP → Windows watcher 只读 ingest。项目不删除、移动、改名、回写手机文件。
9. Windows 执行 `list-devices --discover`，使用返回的 local `device_id` 显式创建 effective-dated assignment；不得编辑 config/state JSON，也不得按电脑当前用户猜销售。

## 每台 Redmi 的 bounded physical Golden Case

为 Redmi Note 12 5G 和 Redmi Note 15 分别建立 gitignored evidence 记录；每项只允许 PASS、FAIL、NOT_RUN。完整号码、联系人、XML、录音、设备 dump、alias/序列号、销售映射和真实路径不得进入 Git。

1. `probe --save-report` 能通过 Windows Shell/MTP 识别真实设备；记录 storage root 数量，不公开序列号。
2. 枚举真实 storage roots，确认无盘符假设。
3. 在限定深度内确认真实 call recording directory；官方候选路径只作搜索输入。
4. 使用 `ingest --once --limit 1` 复制一个真实 call recording；手机端保持只读。
5. 校验 staging 完成、source/local size 与 SHA-256；ready media/sidecar 完整。
6. 第二次同样 ingest 为 duplicate，ready recording 数不增长。
7. 有界发现真实 `SMSBackupRestore/calls-*.xml` 目录。
8. `inspect-calllog-export` 验证真实 XML schema；常规输出不得含字段值。
9. `ingest-calllog-export --once` 导入 full call list，并形成 schema-valid PhoneCall；无录音电话也必须存在。
10. 如设备实际为双 SIM，确认 `subscription_id/component` 真实字段和 provenance；slot 只有 exporter 明确提供才填写。单 SIM/无字段记录 NOT_AVAILABLE，不伪造。
11. 产生或选择一个真实 incoming call，确认 direction/raw type。
12. 产生或选择一个真实 outgoing call，确认 direction/raw type。
13. 产生一个真实 missed call，确认 disposition=missed 且不要求录音。
14. 在业务允许且方便时产生 rejected call，确认 disposition=rejected；否则 NOT_RUN，不用 synthetic 代替。
15. 对有录音电话核对 EXACT/HIGH_CONFIDENCE 唯一关联；多个候选必须 AMBIGUOUS，不硬绑。
16. 记录 schedule 设置和 XML backup timestamp，等待至少一个真实 schedule interval；确认 artifact 实际更新并正确分类 freshness。手动 export 不能代替此项。
17. 停止并重启 watcher，再运行一轮；PhoneCall、Recording、Link 与旧 event/package 均不重复。
18. 结束后重新检查手机端源文件数量、大小/modified evidence；确认项目未删除、移动、改名或回写。

## 认证门槛

只有某一具体型号完成上述真实流程、关键项 PASS、隐私检查通过，才可将该型号适配状态从 `OFFICIAL_DOC_CANDIDATE` 升为 `REAL_DEVICE_VERIFIED`。Note 12 的结果不能认证 Note 15；手动 backup 通过不能认证 scheduled backup；本机 sync-root 文件出现不能认证远端坚果云传播。
