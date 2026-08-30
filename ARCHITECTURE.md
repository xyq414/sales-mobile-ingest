# Architecture

```text
Windows Shell/MTP bridge (phone read-only)
  ├─ recording discovery -> stage/size/SHA-256 -> RecordingAsset v1 -> legacy Event v1
  └─ public CallLog XML -> artifact/snapshot -> provider row -> PhoneCall v1
                                                       ↕
                                          CallRecordingLink v1
                                                       |
                            ready/calls + ready/call-links
                                                       |
                  versioned call-fact/link handoff (audio optional)

Legacy cloud v1: RecordingAsset + Event + audio -> immutable strict three-file package
```

桌面 Pilot 是这条链路之上的薄入口：`PySide View → ImportWorkflowService → Ingestor/providers/publishers`。预检会安全复制并解析公共 CallLog snapshot 以形成就绪证据，但不会提前发布 PhoneCall；一键导入与 CLI 复用同一 `Ingestor.ingest_once` call-first orchestration。Qt worker 只负责把阻塞 MTP 工作移出 UI event loop，不包含第二套 parser、identity、dedupe 或 publisher。

## Identity layers

- `recording_id`: unchanged v1 media-content identity.
- provider row ID: provider + stable call fields, including reliable subscription ID; excludes contact and snapshot metadata.
- `call_id`: contract version + provider + local `device_id` + provider row ID. Different devices never merge; providers are not fuzzily merged.
- `link_id`: one stable reconciliation decision per `recording_id`; its status can move from NO_MATCH/AMBIGUOUS to EXACT/HIGH_CONFIDENCE without creating another recording or call.

## Device and salesperson model

Shell `device_key` is an observed alias only. The gitignored registry stores its SHA-256 fingerprint and a random local enrollment `device_id`; only one exact alias match is automatically reclaimed. A new/changed alias creates an unresolved enrollment and is never merged by display name, Windows user, Git identity, phone number or machine name。

Salesperson assignments are non-overlapping `[effective_from, effective_to)` intervals. Attribution uses `PhoneCall.occurred_at`, so late historical imports retain the historical owner. Missing or conflicting evidence produces UNASSIGNED/AMBIGUOUS。

## Durable and compatibility boundaries

State v3 is an atomically replaced local JSON store. Loading older versions creates a private backup before migration. V3 adds privacy-minimal desktop import summaries and cross-time CallLog snapshot observations；它不保存 App 内部 schedule 设置，也不以单个 XML 证明定时任务。Contract files are written through temporary files, fsync and `os.replace`; JSON appears as the commit signal. Existing recording/event IDs and published three-file v1 packages are not rewritten。

`ready/calls` is the canonical call downstream boundary; `ready/call-links` connects calls to recordings. Configured sync roots additionally receive `_phone-call-facts-v1` and `_call-recording-links-v1`. These are separate from legacy per-salesperson three-file directories。

## Desktop runtime boundary

- source CLI 默认继续读 repository-local `config.local.json`；packaged desktop 使用稳定的 per-user `%LOCALAPPDATA%\SalesMobileIngest\config.json`。
- `resources.resource_path` 在源码模式指向项目资源，在 PyInstaller one-folder 中指向 `_MEIPASS`；MTP bridge 与 schema 不依赖 Git checkout 或 current working directory。
- 正式发布是 PyInstaller windowed one-folder；EXE、Qt runtime、build cache、release ZIP 和 smoke reports 全部 gitignored。
- 首页只显示业务安全字段；raw alias、serial、`device_id`、号码、XML、手机绝对路径和 stack trace 不进入普通页面。
