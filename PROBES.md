# MTP capability probe

The production path uses the Windows Shell `Shell.Application` COM namespace because ordinary MTP phones do not need developer mode or ADB and commonly have no drive letter. `scripts/mtp_bridge.ps1` is the small probe/transport boundary.

Probe sequence:

1. enumerate `This PC` portable devices;
2. enumerate device storage roots;
3. inspect only bounded candidate directory names and their bounded audio listings;
4. classify candidates in Python; and
5. copy a selected file into an inbox staging directory with Shell `CopyHere`.

If a phone is unavailable, code and synthetic tests remain valid, but physical-device E2E remains explicitly unverified.

The Shell item `Path` emitted as `device_key` is only an observed connection alias. It is not claimed to be a permanent physical-device identifier. The local registry hashes it, reclaims an enrollment only on one exact match, and creates an unresolved enrollment for a new or ambiguous alias. The project never writes an identity file to the phone。

# Public CallLog XML export probe (OPPO real-device verified)

The next identity source is an explicitly user-created CallLog-only XML backup from SyncTech `SMS Backup & Restore`, accessed only as an ordinary public file through the same Windows Shell/MTP boundary. It is not an ADB source and it is not a replacement for the recording collector.

The probe is bounded: detect the currently connected portable device, inspect only the selected public backup directory, list only expected `calls-*.xml` candidates, and copy a selected XML only into gitignored `data_root/diagnostics/calllog-backup` for local schema inspection. Normal output may contain only root/field names and types; raw XML, phone numbers, contact names and full values are local diagnostic evidence only.

Observed later on 2026-08-19 after the phone was reconnected: Windows Shell/MTP found one OPPO A6 Pro 5G, one configured export directory and one XML candidate. The copied 656-byte artifact produced the actual `calls/call` schema; a SyncTech-specific parser, artifact/canonical-row dedupe and one `HIGH_CONFIDENCE` recording correlation passed. The event was atomically enriched, and repeated manual plus watcher runs made no duplicate recording, XML artifact, canonical row or event. The prior `WINDOWS_MTP_DEVICE_NOT_CURRENTLY_AVAILABLE` result remains an earlier physical-connection observation, not a parser failure.

As of 2026-08-30, parser rows additionally publish canonical PhoneCall, retain subscription fields and raw call types, record snapshot freshness, and reconcile through independent link objects. These additions are synthetic-automated for multi-device/dual-SIM/late-arrival scenarios; they do not retroactively expand the 2026-08-19 OPPO sample evidence。

# Redmi physical probe status

Redmi Note 12 5G and Redmi Note 15 are both `PHYSICAL_DEVICE_PENDING`. Candidate Xiaomi directories come only from official documentation. Run the bounded, per-device acceptance in `docs/手机初始化与Redmi物理验收.md` when each real device is present. Do not promote status based on code paths, fixtures or documentation alone。

# Android CallLog feasibility probe

`android/calllog-probe` is deliberately separate from the production MTP collector. It is a local-only debug APK with one declared permission, `READ_CALL_LOG`, no `INTERNET`, and an app-private JSON result. The Windows helper builds it, checks ADB state, and only attempts install when exactly one already-authorized ADB device is listed.

Observed on 2026-08-19: JDK 17, Android SDK API 36, Build-Tools 36.0.0 and ADB were bootstrapped into ignored local tooling; APK build and static Manifest inspection passed. `adb devices -l` had no device rows, so the exact probe status is `AUTOMATED_ANDROID_APP_INSTALL_UNAVAILABLE_IN_CURRENT_DEVICE_STATE`. The closing MTP ingest check also had `devices=0`; this is an installation-channel / current-device-visibility result, not a conclusion about `READ_CALL_LOG`, package-manager restrictions, CallLog provider access, or call-recording correlation.
