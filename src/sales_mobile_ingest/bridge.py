from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_SCRIPT = PROJECT_ROOT / "scripts" / "mtp_bridge.ps1"


class BridgeError(RuntimeError):
    pass


class MtpBridge:
    """Windows Shell bridge. It never requests ADB and only invokes MTP reads/copies."""

    def __init__(self, script_path: Path = BRIDGE_SCRIPT) -> None:
        self.script_path = script_path

    def probe(self, cached_dirs: list[dict[str, str]] | None = None, search_depth: int = 3) -> dict[str, Any]:
        return self._run({
            "operation": "probe",
            "cached_dirs": cached_dirs or [],
            "search_depth": search_depth,
        })

    def copy_to_staging(self, source: dict[str, Any], destination_dir: Path) -> Path:
        response = self._run({
            "operation": "copy",
            "source": source,
            "destination_dir": str(destination_dir),
            "timeout_seconds": 120,
        }, timeout=150)
        if not response.get("ok"):
            raise BridgeError(response.get("error", "MTP copy failed"))
        destination = Path(response["destination_path"])
        try:
            destination.resolve().relative_to(destination_dir.resolve())
        except ValueError as exc:
            raise BridgeError("MTP bridge returned a destination outside staging") from exc
        return destination

    def _run(self, payload: dict[str, Any], timeout: int = 45) -> dict[str, Any]:
        if not self.script_path.exists():
            raise BridgeError(f"MTP bridge script is missing: {self.script_path}")
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
        command = [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(self.script_path), "-InputJsonBase64", encoded,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            command, text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=timeout, check=False, creationflags=creationflags,
        )
        stdout = completed.stdout.strip()
        if completed.returncode != 0:
            message = completed.stderr.strip() or stdout or f"PowerShell bridge exit {completed.returncode}"
            raise BridgeError(message[:1000])
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"PowerShell bridge returned invalid JSON: {stdout[:500]}") from exc
        if not isinstance(response, dict):
            raise BridgeError("PowerShell bridge returned a non-object response")
        return response
