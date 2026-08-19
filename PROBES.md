# MTP capability probe

The production path uses the Windows Shell `Shell.Application` COM namespace because ordinary MTP phones do not need developer mode or ADB and commonly have no drive letter. `scripts/mtp_bridge.ps1` is the small probe/transport boundary.

Probe sequence:

1. enumerate `This PC` portable devices;
2. enumerate device storage roots;
3. inspect only bounded candidate directory names and their bounded audio listings;
4. classify candidates in Python; and
5. copy a selected file into an inbox staging directory with Shell `CopyHere`.

If a phone is unavailable, code and synthetic tests remain valid, but physical-device E2E remains explicitly unverified.

# Public CallLog XML export probe (current priority)

The next identity source is an explicitly user-created CallLog-only XML backup from SyncTech `SMS Backup & Restore`, accessed only as an ordinary public file through the same Windows Shell/MTP boundary. It is not an ADB source and it is not a replacement for the recording collector.

The intended probe is bounded: detect the currently connected portable device, inspect only the selected public backup directory, list only expected call-log XML candidates, and copy a selected XML only into gitignored `data_root/diagnostics/calllog-backup` for local schema inspection. Normal output may contain only root/field names and types; raw XML, phone numbers, contact names and full values are local diagnostic evidence only.

Observed on 2026-08-19 after the user reported creating a CallLog-only backup: the first MTP probe and three 5-second retries each returned `devices=0`. Therefore the terminal probe status is `WINDOWS_MTP_DEVICE_NOT_CURRENTLY_AVAILABLE`. No directory/file was enumerated, no XML was copied or parsed, and no parser/correlation/event enrichment was implemented from an assumed schema. This is not evidence about whether the phone-side backup exists or contains calls.

# Android CallLog feasibility probe

`android/calllog-probe` is deliberately separate from the production MTP collector. It is a local-only debug APK with one declared permission, `READ_CALL_LOG`, no `INTERNET`, and an app-private JSON result. The Windows helper builds it, checks ADB state, and only attempts install when exactly one already-authorized ADB device is listed.

Observed on 2026-08-19: JDK 17, Android SDK API 36, Build-Tools 36.0.0 and ADB were bootstrapped into ignored local tooling; APK build and static Manifest inspection passed. `adb devices -l` had no device rows, so the exact probe status is `AUTOMATED_ANDROID_APP_INSTALL_UNAVAILABLE_IN_CURRENT_DEVICE_STATE`. The closing MTP ingest check also had `devices=0`; this is an installation-channel / current-device-visibility result, not a conclusion about `READ_CALL_LOG`, package-manager restrictions, CallLog provider access, or call-recording correlation.
