# Windows 桌面 Pilot

`销售手机导入` 是当前单销售 Pilot 的正式 Human Entry。发布包是 PyInstaller one-folder，普通 Windows 用户无需 repository、Python、Codex、PowerShell 或开发依赖。

## 普通用户第一次使用

1. 解压 `SalesMobileIngest-Pilot-win64.zip`，双击目录中的 `SalesMobileIngest.exe`。不要只把 EXE 单独移出目录；旁边的 `_internal` 是程序运行时的一部分。
2. 手机解锁，USB 选择“文件传输 / MTP”。首页自动检查，也可点“重新检查”。
3. 未知手机会打开首次向导。填写销售编号和姓名；只有勾选“当前可见历史都属于该销售”时，系统才使用可验证的最早 CallLog 时间作为历史边界，否则使用用户指定的开始时间。
4. 如 CallLog 未准备好，在手机 `SMS Backup & Restore` 创建本地备份，只选择 `Call logs`，保存到公共/shared storage；回到电脑重新检查，不选择 XML 文件路径。
5. 选择坚果云客户端中已同步的根目录。程序在其中建立专用“销售通话数据”目录，本地 canonical data 仍始终保留。
6. 完成后首页显示“可以导入”或“可以导入，但有提醒”，点击“一键导入到坚果云”。

以后只需解锁手机、选择 MTP、打开程序、点击一次导入。没有新录音、没有录音的 missed/rejected call、没有联系人或 schedule 尚待验证都不会阻止可靠 CallLog 导入。

连接检查与录音读取是两个阶段：首页只枚举设备、存储根和顶层 CallLog 导出目录，录音卡会显示“将在导入时定点检查”；点击导入后才按厂商登记路径和既有成功目录查找录音。这样 Redmi/HyperOS 的慢速目录枚举不会让首页误报连接失败。

## 首页证据语义

- 红色：手机/MTP、设备绑定、可安全解析的 CallLog 或坚果云交付根缺失，不能开始正式导入。
- 黄色：snapshot 较旧/时间未知、schedule 尚待观察、无录音等；PhoneCall 仍可导入。
- “通话记录：正常”只证明公共 `calls-*.xml` 能复制、schema/count/parser 通过；不表示程序读取了 App 私有设置。
- “已观察到后续备份更新”来自不同时间观察到更晚 snapshot 的本地历史；单个 XML 或用户口述不能升级证据。
- “已写入坚果云同步目录”只证明本机已确认目录中的原子落盘与 validator 通过，不证明远端传播或另一台机器已下载。

## 持久数据与隐私

- 桌面配置：`%LOCALAPPDATA%\SalesMobileIngest\config.json`；UI 负责写入，用户不编辑 JSON。
- 默认 canonical data：当前用户 `Documents\SalesMobileIngestData`；可由既有受支持配置覆盖，不依赖 EXE 所在目录。
- 设置/诊断可打开本地数据目录并保存 privacy-minimal report。普通页面和 UI 诊断不含完整号码、联系人、raw XML、手机 alias/serial/device ID、手机绝对路径或 stack trace。
- 程序只通过 Windows Shell/MTP 读取和复制；不使用 ADB、USB debugging、Accessibility 或 companion app，不修改手机文件。

## 开发与发布

开发者仍可使用 README 中的 source CLI。桌面发布由 `scripts/build-desktop-release.ps1` 生成：

- `dist/SalesMobileIngest/`：PyInstaller 原始 one-folder；
- `release/SalesMobileIngest-Pilot-win64/`：可直接双击目录；
- `release/SalesMobileIngest-Pilot-win64.zip`：交付 ZIP。

脚本从 `SalesMobileIngest.spec` bundling `scripts/mtp_bridge.ps1` 与 contract schemas，随后把 release 复制到 repository 外的干净临时目录，直接运行 windowed EXE 两次。Smoke 必须验证 no-phone preflight、主窗口/5 卡/向导/设置、历史归属卡的真实鼠标点击及明确选中反馈、资源 lookup、用户可写配置跨启动持久化和 clean close。旧版程序仍在运行、默认目录无法替换时，可用 `-ReleaseName` 生成并验证一个独立的文件名安全版本目录；脚本不会强制终止用户正在运行的程序。所有 build/dist/release 输出均 gitignored。

当前证据：source GUI smoke `PASS`；packaged clean-directory double-launch + 历史归属真实点击 smoke `PASS`；2026-08-31 真实 OPPO packaged UI 首次绑定与一键导入链路 `PASS`。本机隐私最小化复核证明 local contracts、媒体哈希、state 计数、归属边界和坚果云 call-fact/link 路由一致；未匹配录音仍保持 `NO_MATCH`，没有硬绑。该次使用暴露的历史归属控件视觉反馈不足已在后续 release 修复，修复后真机目视复核仍为 `NOT_RUN`。Redmi Note 15 的 MTP 设备/存储根和定点录音候选枚举已部分 `PASS`，修复后的源码桌面预检约两秒完成；真实录音 copy/hash/dedupe 与 CallLog XML 仍未通过，因此整体继续保持 `PHYSICAL_DEVICE_PENDING`。
