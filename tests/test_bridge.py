from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from sales_mobile_ingest import bridge as bridge_module
from sales_mobile_ingest.bridge import BridgeError, MtpBridge


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "mtp_bridge.ps1"
    path.write_text("# synthetic bridge placeholder\n", encoding="utf-8")
    return path


def test_probe_uses_documented_paths_without_recursive_storage_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        payload = json.loads(base64.b64decode(command[-1]).decode("utf-8"))
        observed.update(payload)
        return subprocess.CompletedProcess(command, 0, stdout='{"ok":true,"devices":[]}', stderr="")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)
    MtpBridge(_script(tmp_path)).probe([])
    assert observed["search_depth"] == 0
    assert observed["audio_search_depth"] == 0
    paths = [str(value).casefold() for value in observed["candidate_paths"]]  # type: ignore[index]
    assert "miui/sound_recorder/call_rec" in paths
    assert "music/recordings/call recordings" in paths


def test_timeout_error_never_exposes_encoded_command_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "private-device-alias-and-path"

    def timed_out(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 45))

    monkeypatch.setattr(bridge_module.subprocess, "run", timed_out)
    bridge = MtpBridge(_script(tmp_path))
    with pytest.raises(BridgeError) as raised:
        bridge.probe([{"device_key": secret, "device_name": "Synthetic", "relative_path": secret}])
    message = str(raised.value)
    assert message == "MTP bridge timed out after 150 seconds"
    assert secret not in message
    assert "InputJsonBase64" not in message
