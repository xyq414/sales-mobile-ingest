# Acceptance harness

## Automated suite

Run `python -m pytest`. The suite contains the following deterministic Golden Cases plus legacy regressions:

| Golden Case | Automated assertion |
| --- | --- |
| GC-01/02 | incoming CallLog-first and outgoing Recording-first both converge to one call, one recording and one link |
| GC-03/04/05 | missed, rejected and normal no-recording calls publish `phone-call/v1` without media |
| GC-06 | permanently unmatched recording remains ready with NO_MATCH link |
| GC-07/08 | both late-arrival orders converge without duplicate objects |
| GC-09 | multiple plausible candidates remain AMBIGUOUS with `call_id=null` |
| GC-10 | subscription ID/component are retained; explicit dual-SIM rows do not collide; slot is not guessed |
| GC-11 | identical full XML replayed ten times has zero PhoneCall growth |
| GC-12/13 | full → incremental/archive snapshots keep stable call identity |
| GC-14 | same call fields on two devices produce distinct call IDs |
| GC-15/16 | one salesperson on multiple devices and multiple salespeople/devices coexist |
| GC-17 | historical call attribution follows effective time, not current holder |
| GC-18 | overlapping assignment is rejected; no evidence remains UNASSIGNED |
| GC-19/20 | legacy v1 unique-device migration succeeds; multi-device ambiguity is blocked |
| GC-21 | contact/snapshot enrichment leaves call ID stable |
| GC-22 | malformed XML keeps private failure evidence and emits no call |
| GC-23 | future call type is raw-preserved and standardized as unknown |
| GC-24/25 | stale and fresh backup timestamps are classified; missing timestamp is UNKNOWN |
| GC-26/27 | recording/event v1 and strict cloud three-file v1 validators remain green |
| GC-28 | call-only local/cloud artifact is schema-valid and needs no audio |
| GC-29 | late recording updates link stream without creating a second fact |
| GC-30 | state reload/replay preserves call, recording and link bytes/identities |

Also covered: schema examples, state backup, atomic publication, source/artifact dedupe, cloud conflicts, event enrichment, privacy-minimal summaries, CLI parser smoke, watcher single-cycle synthetic execution and drive-independent roots。

## Desktop Pilot automated matrix

Desktop tests inject a fake backend and separately exercise the real `Ingestor` preflight adapter. They cover no phone, unknown/known device, assignment boundaries, CallLog directory missing/no XML/FRESH/STALE/UNKNOWN/MALFORMED/count mismatch, scheduled evidence history, optional recording states, missing/invalid/confirmed cloud root, orchestration order, double-run exclusion, human error translation, privacy filtering, persistent config and packaged resource resolution。

`test_desktop_ui.py` creates a real Qt application with the offscreen Windows platform, renders the five home cards, drives button state, opens the first-run wizard/settings dialog, captures a window image and closes cleanly. This is a GUI smoke, not just an import check。

`scripts/build-desktop-release.ps1` builds the windowed PyInstaller one-folder release, copies it to a clean temporary directory outside the repository, launches the EXE twice with no system Python, runs actual no-phone preflight through the bundled MTP bridge, verifies schemas/resources, renders the UI, and proves the same user-writable config persists across launches。报告和 release 均不进入 Git。

## Physical acceptance

`probe` remains read-only and must not assume a drive letter. A physical device passes only after real MTP discovery, source-byte invariance, copy/size/hash, duplicate ingest and its provider-specific checks complete. Absence of a connected phone is `NOT_RUN`, not automated failure. Redmi Note 12 5G and Redmi Note 15 use the separate runbook in `docs/手机初始化与Redmi物理验收.md`; synthetic fixtures can never set `REAL_DEVICE_VERIFIED`。

The next OPPO Pilot acceptance starts from the packaged `SalesMobileIngest.exe`, not from source commands. Until that one-click physical run passes, its desktop evidence is `NOT_RUN` even though prior OPPO backend/MTP evidence remains valid。
