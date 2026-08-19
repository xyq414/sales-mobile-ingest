# sales-mobile-ingest

Windows 本机将安卓电话录音以只读方式从 USB/MTP 采集到稳定的 `recording contract v1`。第一版不做转写、AI、CRM、微信、云上传或任何手机端删除；原始音频保持原始字节和扩展名。

## 三层边界

1. **手机采集层**：`scripts/mtp_bridge.ps1` 使用 Windows Portable Device/Shell 访问手机。它不需要 ADB、开发者模式或手机盘符。
2. **标准化层**：Python 先复制到 `inbox/recordings/.stage`，校验尺寸、计算 SHA-256、去重，再发布媒体和 JSON sidecar。
3. **下游 contract**：下游只读取 `ready/recordings` 中的媒体和同名 JSON；JSON 是最终 commit signal，看到 JSON 时其 `media_filename` 已存在且完整。

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

# 一次增量采集
.\.venv\Scripts\python.exe -m sales_mobile_ingest ingest --once --data-root "F:\CompanyData\SalesMobile"

# 前台观察模式（自动任务实际调用的命令）
.\.venv\Scripts\python.exe -m sales_mobile_ingest watch --interval 45

# 在当前用户登录后自动进入 watch；可传入新的数据根目录
.\scripts\install-autostart.ps1
.\scripts\uninstall-autostart.ps1
```

`probe` 只扫描限定深度内的候选目录名，然后只在候选目录中查看有限数量的音频项目。`Recordings` 之类的名称不是成功依据：OPPO v1 还要求音频和命名/时间特征；通用适配器还要求明确的 call/phone/通话特征，因此不会把普通 Music 目录当作通话录音。

自动启动优先注册用户登录时的 `SalesMobileIngest` Task Scheduler 任务；若公司 Windows 策略拒绝普通用户注册任务，安装脚本会改用当前用户 Startup 目录中的可卸载隐藏 `pythonw` 启动项，并明确输出该结果。两种方式都不依赖 Codex 持续运行。

## 本机数据目录

在可配置 `data_root` 下：

```text
inbox/recordings/.stage/   # 尚未完成校验的本地暂存
ready/recordings/          # 下游消费的媒体 + 同名 JSON
failed/recordings/         # 保留失败证据，不静默丢失
state/                     # 增量导入状态和成功目录缓存
logs/                      # 隐私最小化运行日志
```

## 真实状态

参见 [docs/当前状态.md](docs/当前状态.md)。该文件严格区分合成测试和本机真机验证。
