from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any


class DeviceRegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise DeviceRegistryError("effective timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise DeviceRegistryError("effective timestamp must include a timezone")
    return parsed


class DeviceRegistry:
    """Gitignored local enrollment identities; observed Shell paths are aliases, not physical IDs."""

    def __init__(self, state_data: dict[str, Any]) -> None:
        self.data = state_data.setdefault("device_registry", {})
        self.data.setdefault("devices", {})
        self.data.setdefault("alias_index", {})
        self.data.setdefault("assignments", {})
        self.data.setdefault("migration", {})

    @staticmethod
    def alias_fingerprint(observed_alias: str) -> str:
        if not observed_alias:
            raise DeviceRegistryError("an observed device alias is required")
        return hashlib.sha256(observed_alias.encode("utf-8")).hexdigest()

    def observe(
        self,
        *,
        observed_alias: str,
        display_name: str | None,
        vendor: str | None,
        model: str | None,
        observed_at: str | None = None,
    ) -> str:
        observed_at = observed_at or _now()
        _timestamp(observed_at)
        fingerprint = self.alias_fingerprint(observed_alias)
        indexed = self.data["alias_index"].get(fingerprint)
        if isinstance(indexed, list) and len(indexed) != 1:
            raise DeviceRegistryError("observed device alias is ambiguous")
        if isinstance(indexed, str):
            device_id = indexed
        elif isinstance(indexed, list) and len(indexed) == 1:
            device_id = indexed[0]
        else:
            device_id = f"dev_{uuid.uuid4()}"
            self.data["alias_index"][fingerprint] = device_id
            self.data["devices"][device_id] = {
                "device_id": device_id,
                "display_name": display_name,
                "vendor": vendor,
                "model": model,
                "enrollment_status": "UNASSIGNED",
                "alias_fingerprints": [fingerprint],
                "first_seen": observed_at,
                "last_seen": observed_at,
            }
        device = self.data["devices"].get(device_id)
        if not isinstance(device, dict):
            raise DeviceRegistryError("device alias index points to a missing enrollment")
        aliases = device.setdefault("alias_fingerprints", [])
        if fingerprint not in aliases:
            aliases.append(fingerprint)
        device.update({
            "display_name": display_name or device.get("display_name"),
            "vendor": vendor or device.get("vendor"),
            "model": model or device.get("model"),
            "last_seen": observed_at,
        })
        return device_id

    def devices(self) -> list[dict[str, Any]]:
        return [dict(value) for _, value in sorted(self.data["devices"].items()) if isinstance(value, dict)]

    def assignments_for(self, device_id: str) -> list[dict[str, Any]]:
        return sorted(
            [dict(item) for item in self.data["assignments"].values() if item.get("device_id") == device_id],
            key=lambda item: (item["effective_from"], item["assignment_id"]),
        )

    def assign(
        self,
        *,
        device_id: str,
        salesperson_id: str,
        salesperson_name: str,
        effective_from: str,
        effective_to: str | None = None,
        source: str = "operator_cli",
    ) -> dict[str, Any]:
        if device_id not in self.data["devices"]:
            raise DeviceRegistryError("unknown device_id; run list-devices --discover first")
        if not salesperson_id.strip() or not salesperson_name.strip():
            raise DeviceRegistryError("salesperson id and name must both be non-empty")
        start = _timestamp(effective_from)
        end = _timestamp(effective_to) if effective_to else None
        if end is not None and end <= start:
            raise DeviceRegistryError("effective_to must be later than effective_from")
        for existing in self.assignments_for(device_id):
            existing_start = _timestamp(existing["effective_from"])
            existing_end = _timestamp(existing["effective_to"]) if existing.get("effective_to") else None
            if (end is None or existing_start < end) and (existing_end is None or start < existing_end):
                raise DeviceRegistryError("salesperson assignment overlaps an existing effective interval")
        assignment_id = f"asg_{uuid.uuid4()}"
        now = datetime.now(timezone.utc)
        if start > now:
            status = "SCHEDULED"
        elif end is not None and end <= now:
            status = "ENDED"
        else:
            status = "ACTIVE"
        assignment = {
            "assignment_id": assignment_id,
            "device_id": device_id,
            "salesperson_id": salesperson_id.strip(),
            "salesperson_name": salesperson_name.strip(),
            "effective_from": start.isoformat(),
            "effective_to": end.isoformat() if end else None,
            "source": source,
            "status": status,
            "created_at": _now(),
        }
        self.data["assignments"][assignment_id] = assignment
        self._refresh_device_status(device_id)
        return dict(assignment)

    def end_assignment(self, *, device_id: str, effective_to: str) -> dict[str, Any]:
        end = _timestamp(effective_to)
        active = [item for item in self.assignments_for(device_id) if item.get("effective_to") is None]
        if len(active) != 1:
            raise DeviceRegistryError("device must have exactly one open assignment to end")
        assignment = self.data["assignments"][active[0]["assignment_id"]]
        if end <= _timestamp(assignment["effective_from"]):
            raise DeviceRegistryError("effective_to must be later than effective_from")
        assignment["effective_to"] = end.isoformat()
        assignment["status"] = "ENDED" if end <= datetime.now(timezone.utc) else "ACTIVE"
        self._refresh_device_status(device_id)
        return dict(assignment)

    def attribution(self, *, device_id: str, occurred_at: str) -> dict[str, Any]:
        instant = _timestamp(occurred_at)
        matches = []
        for assignment in self.assignments_for(device_id):
            start = _timestamp(assignment["effective_from"])
            end = _timestamp(assignment["effective_to"]) if assignment.get("effective_to") else None
            if start <= instant and (end is None or instant < end):
                matches.append(assignment)
        if len(matches) == 1:
            match = matches[0]
            return {
                "salesperson_id": match["salesperson_id"],
                "salesperson_name": match["salesperson_name"],
                "salesperson_assignment_id": match["assignment_id"],
                "salesperson_attribution_status": "ASSIGNED",
            }
        return {
            "salesperson_id": None,
            "salesperson_name": None,
            "salesperson_assignment_id": None,
            "salesperson_attribution_status": "AMBIGUOUS" if len(matches) > 1 else "UNASSIGNED",
        }

    def _refresh_device_status(self, device_id: str) -> None:
        device = self.data["devices"][device_id]
        current = self.attribution(device_id=device_id, occurred_at=_now())
        device["enrollment_status"] = current["salesperson_attribution_status"]
