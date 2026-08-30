# Sales mobile ingest Call-first scope

## Outcome

建立可部署于一台 Windows、多销售、多 Android 手机的只读电话事实边界：

```text
Android CallLog public export -> canonical PhoneCall -> optional RecordingAsset
                                      |                    |
                                      +-- CallRecordingLink+
                                      -> downstream call-fact v1
```

PhoneCall 是一级事实；missed、rejected、无录音、录音迟到或无法安全匹配的电话都不能消失。RecordingAsset 可先到、后到或永久 unmatched。已有 recording/event/cloud 三文件 v1 保持兼容和不可变。

## In scope

- 无盘符 Windows Shell/MTP 只读发现与复制；不要求 ADB。
- replaceable CallLog public-export provider；当前真机验证 provider 为 SyncTech CallLog-only XML。
- `phone-call/v1`、`call-recording-link/v1`、snapshot freshness、stable identity、重放幂等与 failure evidence。
- 本机 Device Registry、精确 observed-alias enrollment、effective-dated Salesperson Assignment 和 CLI operator flow。
- 独立 call-only downstream/cloud handoff；旧 recording-backed v1 不变。
- crash-safe JSON state migration/backup、原子 contract publication、自动 Golden Cases。

## Out of scope

SMS/MMS/RCS 正文、微信/企业微信、ASR、LLM、CRM matching、Data Hub、ADB production dependency、手机端 companion agent、手机文件写入/删除、坚果云 API/WebDAV，以及未经真机验证的厂商 adapter 扩张。

## Evidence boundary

- OPPO A6 Pro 5G 的既有 MTP/录音/SyncTech XML/旧 v1 handoff 证据仍为 `REAL_DEVICE_VERIFIED`。
- 本轮 Call-first、多设备、双卡、迁移、late-arrival 与新 handoff 为 deterministic synthetic automated validation。
- Redmi Note 12 5G / Redmi Note 15 仍为 `OFFICIAL_DOC_CANDIDATE` / `PHYSICAL_DEVICE_PENDING`，只有分别完成真实物理 Golden Case 才可升级。
