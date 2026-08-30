# Call-first contract 接口说明

## Canonical 对象

`ready/calls/<call_id>.json` 是 `phone-call/v1` 的提交信号。每条可靠解析的 CallLog 行都会形成一个 PhoneCall；录音、号码、联系人、SIM 或销售归属缺失都不会阻止发布。

`call_id` 由 contract version、provider、local `device_id` 与稳定 provider row identity 计算。row identity 使用发生时间、duration、raw call type、号码及可靠的 `subscription_id`，不使用联系人名、artifact hash、snapshot mode 或导入时间。因此 full/incremental/archive 重放与联系人后到不会改变 ID，不同设备不会合并。不同 provider 默认保持 provenance boundary，不做模糊跨 provider 合并。

`direction` 与 `disposition` 分开：incoming/outgoing 只表达方向；missed、rejected、voicemail、blocked、answered externally 表达 provider 已证明的类型。1/2 类型的 disposition 保持 `unknown`，不会根据 duration 猜接通状态。未知未来类型保留在 `raw_call_type` 并标准化为 unknown。

`subscription` 保留 exporter 明确提供的 ID、component 和 slot。字符串不会被猜成 SIM slot，运营商显示名不参与 call identity。

## Recording 关系

`ready/call-links/<link_id>.json` 是独立的 `call-recording-link/v1`。link identity 由 `recording_id` 稳定计算，状态为 `EXACT`、`HIGH_CONFIDENCE`、`AMBIGUOUS` 或 `NO_MATCH`。后到 CallLog/录音会原子更新同一 link；多个合理候选时 `call_id=null` 并保留 candidate call IDs，禁止硬绑。

旧 `recording-contract/v1` 与 `communication-event/v1` 保持 recording-backed compatibility projection，ID 公式不变。link 通过 `recording_id` 将旧 event/recording 与新 PhoneCall 关联。

## 下游和云端边界

本机下游直接消费 schema-validated `ready/calls` 与 `ready/call-links`。配置 cloud handoff root 后，新流独立发布到 `_phone-call-facts-v1` 与 `_call-recording-links-v1`；无录音 PhoneCall 只需一个 JSON。旧云端电话目录仍严格只有 `audio.<ext>`、`recording.json`、`event.json` 三文件，不放宽、不重写。写入本机 sync root 只证明本地 handoff，不代表远端同步或消费完成。

## Snapshot freshness

每个 PhoneCall 带 source snapshot ID、artifact SHA-256、root count、backup mode、backup/import timestamp 与 `FRESH|STALE|UNKNOWN`。默认 freshness 阈值 48 小时，可通过 gitignored `calllog_freshness_seconds` 调整。旧或未知 snapshot 仍可导入已有历史，但不得解释为“截至当前完整”。malformed artifact 不产生 PhoneCall，并在 private diagnostics 保留 failure evidence。
