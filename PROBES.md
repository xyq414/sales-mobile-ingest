# MTP capability probe

The production path uses the Windows Shell `Shell.Application` COM namespace because ordinary MTP phones do not need developer mode or ADB and commonly have no drive letter. `scripts/mtp_bridge.ps1` is the small probe/transport boundary.

Probe sequence:

1. enumerate `This PC` portable devices;
2. enumerate device storage roots;
3. inspect only bounded candidate directory names and their bounded audio listings;
4. classify candidates in Python; and
5. copy a selected file into an inbox staging directory with Shell `CopyHere`.

If a phone is unavailable, code and synthetic tests remain valid, but physical-device E2E remains explicitly unverified.
