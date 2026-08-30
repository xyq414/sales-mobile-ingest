# sales-mobile-ingest

Windows 本机以只读 USB/MTP 采集 Android CallLog 公共导出与可选电话录音。核心链路是 `CallLog → PhoneCall v1 → optional RecordingAsset → CallRecordingLink v1`：电话存在不再以录音存在为前提。既有 recording/event contract v1 与严格三文件云包继续兼容；新的 call-fact/link 流独立承载无录音、missed、rejected 和迟到录音电话。

## 四层边界

1. **手机采集层**：`scripts/mtp_bridge.ps1` 使用 Windows Portable Device/Shell 访问手机。它不需要 ADB、开发者模式或手机盘符。
2. **Call-first 标准化层**：公共 XML 经 artifact/snapshot、replaceable provider parser 与稳定 row identity，原子发布 `ready/calls`；snapshot freshness 不等同于历史完整性。
3. **可选录音与关系层**：原有 staging、size、SHA-256、recording sidecar/event 不变；`ready/call-links` 独立表达 EXACT、HIGH_CONFIDENCE、AMBIGUOUS、NO_MATCH 和 late reconciliation。
4. **下游交付层**：本机 `ready/calls`/`ready/call-links` 是正式 schema-validated boundary；配置同步根后发布独立 `_phone-call-facts-v1`/`_call-recording-links-v1`。旧三文件电话包保持严格、不可变。

真实录音、客户数据、本机配置、状态和日志绝不进入 GitHub。详见 [contract/接口说明.md](contract/接口说明.md)。

## Windows Pilot 正式入口

普通销售使用的是发布包中的 `SalesMobileIngest.exe`，不需要 repository、Python、Codex、PowerShell 或 JSON 配置：

1. 解压 `SalesMobileIngest-Pilot-win64.zip`，双击 `SalesMobileIngest.exe`。
2. 解锁手机并在 USB 选项中选择“文件传输 / MTP”；首页会自动检查，也可点“重新检查”。
3. 新手机第一次在向导中填写销售编号/姓名，并明确选择历史归属边界。
4. 第一次确认坚果云客户端中已经同步的根目录；程序创建独立“销售通话数据”子目录。
5. 如首页提示 CallLog 未准备好，只需在手机 `SMS Backup & Restore` 立即备份 `Call logs` 到公共存储，再回到 UI 检查。
6. 首页显示“可以导入”或“可以导入，但有提醒”后，点“一键导入到坚果云”。

桌面程序始终先保留 local canonical state，再写入已确认的坚果云同步目录。UI 只会声称“已写入坚果云同步目录”，不会把本机落盘冒充成远端同步成功。定时备份“已观察到后续更新”也只来自跨时间的公共 snapshot 证据；程序无法读取或认证 App 内部全部设置。

发布、状态语义和首次使用说明见 [docs/Windows桌面Pilot.md](docs/Windows桌面Pilot.md)。当前 packaged UI 已完成 synthetic/no-phone smoke；尚未用本 UI 对真实 OPPO 执行一键导入，因此物理 UI 验收仍为 `NOT_RUN`。

## 开发者安装与 CLI

在项目根目录 PowerShell 中：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m sales_mobile_ingest probe
.\.venv\Scripts\python.exe -m sales_mobile_ingest ingest --once
.\.venv\Scripts\python.exe -m pytest
```

数据根目录优先级（高到低）：

1. `--data-root <path>`
2. 项目本机 `config.local.json` 的 `data_root`
3. 环境变量 `SALES_MOBILE_INGEST_DATA_ROOT`
4. 当前 Windows 用户的 `Documents\SalesMobileIngestData`

源码 CLI 仍兼容项目内 gitignored `config.local.json`。桌面程序使用 `%LOCALAPPDATA%\SalesMobileIngest\config.json`，首次源码 GUI 启动可非破坏地复制既有 legacy config；用户不需编辑任何配置文件。业务代码没有 `C:`、`D:` 或 `E:` 的前提。

常用命令：

```powershell
# 仅读发现设备、存储及录音候选，不复制手机文件
.\.venv\Scripts\python.exe -m sales_mobile_ingest probe

# 同时生成本机 gitignored 的脱敏诊断报告（不含文件名、号码、联系人或序列号）
.\.venv\Scripts\python.exe -m sales_mobile_ingest probe --save-report

# 对唯一 ready 录音执行只读的号码/通话记录能力调查；原始敏感证据只保存在本机 diagnostics
.\.venv\Scripts\python.exe -m sales_mobile_ingest investigate-identity

# 仅读取得受限公共 XML，并输出不含字段值的真实 schema 摘要
.\.venv\Scripts\python.exe -m sales_mobile_ingest inspect-calllog-export

# 增量解析公共 XML；每条可靠 row 建立 PhoneCall，再保守关联录音
.\.venv\Scripts\python.exe -m sales_mobile_ingest ingest-calllog-export --once

# 发现/查看本机设备 enrollment，不显示 raw Shell alias
.\.venv\Scripts\python.exe -m sales_mobile_ingest list-devices --discover
.\.venv\Scripts\python.exe -m sales_mobile_ingest show-device --device-id "<dev_...>"

# 建立/结束有效期销售归属；时间必须带时区，区间不允许 overlap
.\.venv\Scripts\python.exe -m sales_mobile_ingest assign-device --device-id "<dev_...>" --salesperson-id "S007" --salesperson-name "张三" --effective-from "2026-01-01T00:00:00+08:00"
.\.venv\Scripts\python.exe -m sales_mobile_ingest end-device-assignment --device-id "<dev_...>" --effective-to "2026-10-01T00:00:00+08:00"

# 只读检查坚果云客户端、已配置 root 与候选同步目录；多个候选时不会猜测
.\.venv\Scripts\python.exe -m sales_mobile_ingest inspect-cloud-handoff

# legacy 单销售兼容入口；新部署使用 per-device assignment
.\.venv\Scripts\python.exe -m sales_mobile_ingest configure-salesperson --salesperson-id "S007" --salesperson-name "张三"

# 用户确认的坚果云同步根仍写入 gitignored config.local.json
.\.venv\Scripts\python.exe -m sales_mobile_ingest configure-cloud-handoff --sync-root "<用户确认的坚果云同步根>"

# 从当前 ready recording/event 构建并发布完整三文件电话包；watch 也会自动执行
.\.venv\Scripts\python.exe -m sales_mobile_ingest publish-cloud-handoff --once
.\.venv\Scripts\python.exe -m sales_mobile_ingest publish-call-facts --once
.\.venv\Scripts\python.exe -m sales_mobile_ingest validate-cloud-package --package-dir "<一个电话文件夹>"

# 一次增量采集
.\.venv\Scripts\python.exe -m sales_mobile_ingest ingest --once --data-root "F:\CompanyData\SalesMobile"

# 第一次真机冒烟：最多复制一个候选源文件
.\.venv\Scripts\python.exe -m sales_mobile_ingest ingest --once --limit 1

# 前台观察模式（自动任务实际调用的命令）
.\.venv\Scripts\python.exe -m sales_mobile_ingest watch --interval 45

# 在当前用户登录后自动进入 watch；可传入新的数据根目录
.\scripts\install-autostart.ps1
.\scripts\uninstall-autostart.ps1
```

`probe` 只扫描限定深度内的候选目录名，然后只在候选目录中查看有限数量的音频项目。`Recordings` 之类的名称不是成功依据：OPPO v1 还要求音频和命名/时间特征；通用适配器还要求明确的 call/phone/通话特征，因此不会把普通 Music 目录当作通话录音。

部分 Android/WPD 实现会把 Shell `Name` 作为不带扩展名的显示名。bridge 会优先读取 `System.FileName` 与 `System.FileExtension`，因此不会因该显示差异漏掉真实 `.mp3`、`.m4a` 等录音；路径解析仍使用 Shell 的原始相对节点名。

`ingest --once` 默认处理所有符合条件且尚未导入的源文件；`--limit N` 只限制该次命令的源文件复制尝试，适合首次真机 smoke test，不会改变长期的正常增量策略。

自动启动优先注册用户登录时的 `SalesMobileIngest` Task Scheduler 任务；若公司 Windows 策略拒绝普通用户注册任务，安装脚本会改用当前用户 Startup 目录中的可卸载隐藏 `pythonw` 启动项，并明确输出该结果。两种方式都不依赖 Codex 持续运行。

## 本机数据目录

在可配置 `data_root` 下：

```text
inbox/recordings/.stage/   # 尚未完成校验的本地暂存
ready/recordings/          # 下游消费的媒体 + 同名 JSON
ready/events/              # 录音 pair 完整后发布的 communication event JSON
ready/calls/               # canonical PhoneCall v1；无需录音
ready/call-links/          # recording-centric CallRecordingLink v1
failed/recordings/         # 保留失败证据，不静默丢失
state/                     # 增量导入状态和成功目录缓存
logs/                      # 隐私最小化运行日志
diagnostics/probe-reports/ # gitignored 的脱敏本机诊断
diagnostics/calllog-backup/ # gitignored 的原始公共 XML、schema 与关联摘要
diagnostics/migrations/    # legacy migration 私有备份/evidence
diagnostics/desktop/       # UI 生成的隐私最小化诊断
```

## 真实状态

参见 [docs/当前状态.md](docs/当前状态.md) 与 [docs/厂商适配矩阵.md](docs/厂商适配矩阵.md)。前者严格区分合成测试和本机真机验证；后者将 `REAL_DEVICE_VERIFIED`、`OFFICIAL_DOC_CANDIDATE`、`DOC_EVIDENCE_UNAVAILABLE` 与 generic heuristic 明确分开。

2026-08-20 的既有 OPPO 验证曾使用仅存于 gitignored config 的显式开发测试身份完成真实三文件本地 sync-root handoff、重复发布去重和 watcher 重启。该证据只到本机 registered sync root，不等同于远端传播或云端消费者验收；本轮工作区没有 legacy config/state。

Call-first 语义见 [contract/Call-first接口说明.md](contract/Call-first接口说明.md)。新 PhoneCall 的销售归属只来自 effective-dated Device Assignment；未知设备不会继承本机 legacy salesperson。旧 event v1 的 `configure-salesperson` 仅保留兼容投影和受限迁移。

正式云端消费者接口见 [docs/云端电话包接口.md](docs/云端电话包接口.md)：旧 recording-backed 电话目录仍严格只有 `audio.<ext>`、`recording.json`、`event.json`；call-only JSON 使用独立版本目录。`data_root` 的 state、日志、诊断和 XML 永不整体同步。

当前真实 OPPO 样本的电话身份能力边界见 [docs/通话身份解析能力.md](docs/通话身份解析能力.md)。它明确区分“录音自身没有号码证据”与“普通 MTP/WPD 不公开通话记录”，不会把任意长数字或文件修改时间伪装成客户身份。

## 公共 CallLog XML 导出（首个已验证外部导出器）

当前选择的下一条来源是用户主动在手机上使用 SyncTech `SMS Backup & Restore` 创建的 **CallLog-only 本地 XML 备份**，再经普通 USB/MTP 只读取得。它不是本项目的永久厂商依赖，也不需要 ADB、开发者模式或项目自带 Android App。

OPPO A6 Pro 5G 已通过普通 MTP 真机验证既有 XML/录音闭环；当前真实关联仍是 `HIGH_CONFIDENCE`，不会伪称 `EXACT`。本轮新增 PhoneCall、双卡、多人多机、freshness 与 late-arrival 只完成 synthetic automated validation。Redmi Note 12 5G / Note 15 没有真机证据，保持 physical pending。设计与证据边界见 [docs/通话记录导出接口.md](docs/通话记录导出接口.md) 和 [docs/手机初始化与Redmi物理验收.md](docs/手机初始化与Redmi物理验收.md)。

## 历史 Android CallLog feasibility probe（非生产、非当前优先路径）

`android/calllog-probe` 是与 production MTP collector 隔离的最小 debug APK：只声明 `READ_CALL_LOG`，没有 `INTERNET`、录音、联系人、写入或后台权限。它只用于验证“正常 Android App 能否取得 CallLog 并与已导入录音关联”，不属于自动采集、不会修改 `ready/events`，也不会替换 Windows watcher。

本机已验证源码与 APK build；`adb devices -l` 没有任何可安装设备，因此尚未对真机取得或拒绝 `READ_CALL_LOG` 下结论。详细的权限边界、真实状态和未来 probe 清理规则见 [docs/通话身份解析能力.md](docs/通话身份解析能力.md)。
