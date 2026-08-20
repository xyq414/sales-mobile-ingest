# sales-mobile-ingest

Windows 本机将安卓电话录音以只读方式从 USB/MTP 采集到稳定的 `recording contract v1`，并自动发布供 Sales AI 消费的 `communication event contract v1`。完成销售身份和云端目录配置后，系统还会向坚果云客户端监控范围发布严格三文件的电话包。第一版不做转写、AI、CRM、微信、坚果云 API 调用或任何手机端删除；原始音频保持原始字节和扩展名。

## 三层边界

1. **手机采集层**：`scripts/mtp_bridge.ps1` 使用 Windows Portable Device/Shell 访问手机。它不需要 ADB、开发者模式或手机盘符。
2. **标准化层**：Python 先复制到 `inbox/recordings/.stage`，校验尺寸、计算 SHA-256、去重，再发布媒体和 JSON sidecar。
3. **本机事件与云端交付**：录音 JSON 在 `ready/recordings` 最后出现；随后才在 `ready/events` 发布通信事件 JSON。配置完成后，系统从这两个 canonical 对象构建独立的三文件云端电话包；下游无需理解 OPPO 路径、MTP 或 Windows 盘符。

真实录音、客户数据、本机配置、状态和日志绝不进入 GitHub。详见 [contract/接口说明.md](contract/接口说明.md)。

## 安装与运行

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
2. 环境变量 `SALES_MOBILE_INGEST_DATA_ROOT`
3. 项目本机 `config.local.json` 的 `data_root`
4. 当前 Windows 用户的 `Documents\SalesMobileIngestData`

复制 `config.example.json` 为 `config.local.json` 后可修改默认根目录；`config.local.json` 被 Git 忽略。业务代码没有 `C:`、`D:` 或 `E:` 的前提。

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

# 增量解析已验证的公共 XML；只在唯一 HIGH_CONFIDENCE / EXACT 匹配时原子 enrich event
.\.venv\Scripts\python.exe -m sales_mobile_ingest ingest-calllog-export --once

# 只读检查坚果云客户端、已配置 root 与候选同步目录；多个候选时不会猜测
.\.venv\Scripts\python.exe -m sales_mobile_ingest inspect-cloud-handoff

# 一次性设置明确的业务销售身份和用户确认的坚果云同步根（均写入 gitignored config.local.json）
.\.venv\Scripts\python.exe -m sales_mobile_ingest configure-salesperson --salesperson-id "S007" --salesperson-name "张三"
.\.venv\Scripts\python.exe -m sales_mobile_ingest configure-cloud-handoff --sync-root "<用户确认的坚果云同步根>"

# 从当前 ready recording/event 构建并发布完整三文件电话包；watch 也会自动执行
.\.venv\Scripts\python.exe -m sales_mobile_ingest publish-cloud-handoff --once
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
failed/recordings/         # 保留失败证据，不静默丢失
state/                     # 增量导入状态和成功目录缓存
logs/                      # 隐私最小化运行日志
diagnostics/probe-reports/ # gitignored 的脱敏本机诊断
diagnostics/calllog-backup/ # gitignored 的原始公共 XML、schema 与关联摘要
```

## 真实状态

参见 [docs/当前状态.md](docs/当前状态.md) 与 [docs/厂商适配矩阵.md](docs/厂商适配矩阵.md)。前者严格区分合成测试和本机真机验证；后者将 `REAL_DEVICE_VERIFIED`、`OFFICIAL_DOC_CANDIDATE`、`DOC_EVIDENCE_UNAVAILABLE` 与 generic heuristic 明确分开。

截至 2026-08-20，本开发机已用显式开发测试身份 `DEV-001 / 开发者测试` 完成一次真实三文件电话包的本地坚果云 handoff、重复发布去重和 watcher 重启验证；该身份与 handoff root 都只在 gitignored 本机配置中。已验证的是运行中的坚果云客户端对已注册本地 sync root 的交付边界，不等同于远端传播或云端消费者验收。

下游事件语义见 [contract/通信事件接口说明.md](contract/通信事件接口说明.md)。可选的 `salesperson_id` 只能写入本机 gitignored `config.local.json`；未配置时事件明确为 `UNCONFIGURED`，程序绝不从 Windows 或 Git 身份猜测。

正式云端消费者接口见 [docs/云端电话包接口.md](docs/云端电话包接口.md)：每通电话只有 `audio.<ext>`、`recording.json`、`event.json` 三个文件。`data_root` 的 state、日志、诊断和 XML 永不作为坚果云交付目录整体同步。

当前真实 OPPO 样本的电话身份能力边界见 [docs/通话身份解析能力.md](docs/通话身份解析能力.md)。它明确区分“录音自身没有号码证据”与“普通 MTP/WPD 不公开通话记录”，不会把任意长数字或文件修改时间伪装成客户身份。

## 公共 CallLog XML 导出（首个已验证外部导出器）

当前选择的下一条来源是用户主动在手机上使用 SyncTech `SMS Backup & Restore` 创建的 **CallLog-only 本地 XML 备份**，再经普通 USB/MTP 只读取得。它不是本项目的永久厂商依赖，也不需要 ADB、开发者模式或项目自带 Android App。

OPPO A6 Pro 5G 已通过普通 MTP 真机验证：仅在 `SMSBackupRestore` 中有界发现一个 `calls-*.xml`、以 staging → size → SHA-256 复制到 gitignored diagnostics，按真实 `calls/call` schema 解析。XML artifact、canonical row 与 event 均已验证重复运行不产生第二份对象。当前真实关联为 `HIGH_CONFIDENCE`，不会伪称 `EXACT`；原始 XML 不构成 downstream contract。设计边界、隐私规则和证据说明见 [docs/通话记录导出接口.md](docs/通话记录导出接口.md)。

## 历史 Android CallLog feasibility probe（非生产、非当前优先路径）

`android/calllog-probe` 是与 production MTP collector 隔离的最小 debug APK：只声明 `READ_CALL_LOG`，没有 `INTERNET`、录音、联系人、写入或后台权限。它只用于验证“正常 Android App 能否取得 CallLog 并与已导入录音关联”，不属于自动采集、不会修改 `ready/events`，也不会替换 Windows watcher。

本机已验证源码与 APK build；`adb devices -l` 没有任何可安装设备，因此尚未对真机取得或拒绝 `READ_CALL_LOG` 下结论。详细的权限边界、真实状态和未来 probe 清理规则见 [docs/通话身份解析能力.md](docs/通话身份解析能力.md)。
