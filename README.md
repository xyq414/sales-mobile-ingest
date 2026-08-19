# sales-mobile-ingest

Windows 本机将安卓电话录音以只读方式从 USB/MTP 采集到稳定的 `recording contract v1`，并自动发布供 Sales AI 消费的 `communication event contract v1`。第一版不做转写、AI、CRM、微信、云上传或任何手机端删除；原始音频保持原始字节和扩展名。

## 三层边界

1. **手机采集层**：`scripts/mtp_bridge.ps1` 使用 Windows Portable Device/Shell 访问手机。它不需要 ADB、开发者模式或手机盘符。
2. **标准化层**：Python 先复制到 `inbox/recordings/.stage`，校验尺寸、计算 SHA-256、去重，再发布媒体和 JSON sidecar。
3. **下游 contract**：录音 JSON 在 `ready/recordings` 最后出现；随后才在 `ready/events` 发布通信事件 JSON。事件出现时引用的媒体和录音 sidecar 均已经完整。下游无需理解 OPPO 路径、MTP 或 Windows 盘符。

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
```

## 真实状态

参见 [docs/当前状态.md](docs/当前状态.md) 与 [docs/厂商适配矩阵.md](docs/厂商适配矩阵.md)。前者严格区分合成测试和本机真机验证；后者将 `REAL_DEVICE_VERIFIED`、`OFFICIAL_DOC_CANDIDATE`、`DOC_EVIDENCE_UNAVAILABLE` 与 generic heuristic 明确分开。

下游事件语义见 [contract/通信事件接口说明.md](contract/通信事件接口说明.md)。可选的 `salesperson_id` 只能写入本机 gitignored `config.local.json`；未配置时事件明确为 `UNCONFIGURED`，程序绝不从 Windows 或 Git 身份猜测。
