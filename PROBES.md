# MTP capability probe

The production path uses the Windows Shell `Shell.Application` COM namespace because ordinary MTP phones do not need developer mode or ADB and commonly have no drive letter. `scripts/mtp_bridge.ps1` is the small probe/transport boundary.

Probe sequence:

1. enumerate `This PC` portable devices;
2. enumerate device storage roots;
3. inspect only bounded candidate directory names and their bounded audio listings;
4. classify candidates in Python; and
5. copy a selected file into an inbox staging directory with Shell `CopyHere`.

If a phone is unavailable, code and synthetic tests remain valid, but physical-device E2E remains explicitly unverified.

# Android CallLog feasibility probe

`android/calllog-probe` is deliberately separate from the production MTP collector. It is a local-only debug APK with one declared permission, `READ_CALL_LOG`, no `INTERNET`, and an app-private JSON result. The Windows helper builds it, checks ADB state, and only attempts install when exactly one already-authorized ADB device is listed.

Observed on 2026-08-19: JDK 17, Android SDK API 36, Build-Tools 36.0.0 and ADB were bootstrapped into ignored local tooling; APK build and static Manifest inspection passed. `adb devices -l` had no device rows, so the exact probe status is `AUTOMATED_ANDROID_APP_INSTALL_UNAVAILABLE_IN_CURRENT_DEVICE_STATE`. The closing MTP ingest check also had `devices=0`; this is an installation-channel / current-device-visibility result, not a conclusion about `READ_CALL_LOG`, package-manager restrictions, CallLog provider access, or call-recording correlation.
