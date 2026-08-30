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

## Identity layers

- `recording_id`: unchanged v1 media-content identity.
- provider row ID: provider + stable call fields, including reliable subscription ID; excludes contact and snapshot metadata.
- `call_id`: contract version + provider + local `device_id` + provider row ID. Different devices never merge; providers are not fuzzily merged.
- `link_id`: one stable reconciliation decision per `recording_id`; its status can move from NO_MATCH/AMBIGUOUS to EXACT/HIGH_CONFIDENCE without creating another recording or call.

## Device and salesperson model

Shell `device_key` is an observed alias only. The gitignored registry stores its SHA-256 fingerprint and a random local enrollment `device_id`; only one exact alias match is automatically reclaimed. A new/changed alias creates an unresolved enrollment and is never merged by display name, Windows user, Git identity, phone number or machine name。

Salesperson assignments are non-overlapping `[effective_from, effective_to)` intervals. Attribution uses `PhoneCall.occurred_at`, so late historical imports retain the historical owner. Missing or conflicting evidence produces UNASSIGNED/AMBIGUOUS。

## Durable and compatibility boundaries

State v2 is an atomically replaced local JSON store. Loading v1 creates a private backup before migration. Contract files are written through temporary files, fsync and `os.replace`; JSON appears as the commit signal. Existing recording/event IDs and published three-file v1 packages are not rewritten。

`ready/calls` is the canonical call downstream boundary; `ready/call-links` connects calls to recordings. Configured sync roots additionally receive `_phone-call-facts-v1` and `_call-recording-links-v1`. These are separate from legacy per-salesperson three-file directories。
