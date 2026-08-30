from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .phone_calls import validate_call_recording_link, validate_phone_call
from .state import StateStore
from .cloud_handoff import validate_cloud_handoff_root


@dataclass
class CallFactPublishSummary:
    status: str
    published: int = 0
    already_published: int = 0
    updated: int = 0
    failures: int = 0
    links_published: int = 0
    links_already_published: int = 0
    links_updated: int = 0

    def as_dict(self) -> dict[str, int | str]:
        return {
            "status": self.status,
            "published": self.published,
            "already_published": self.already_published,
            "updated": self.updated,
            "failures": self.failures,
            "links_published": self.links_published,
            "links_already_published": self.links_already_published,
            "links_updated": self.links_updated,
        }


class CallFactHandoffPublisher:
    """Independent call-only stream; it never changes strict recording package v1 directories."""

    ROUTE_DIRECTORY = "_phone-call-facts-v1"
    LINK_ROUTE_DIRECTORY = "_call-recording-links-v1"

    def __init__(
        self, *, data_root: Path | None = None, ready_calls: Path, ready_links: Path | None = None, state: StateStore,
        cloud_handoff_root: Path | None,
    ) -> None:
        self.ready_calls = ready_calls
        self.data_root = data_root or ready_calls.parents[1]
        self.ready_links = ready_links
        self.state = state
        self.cloud_handoff_root = cloud_handoff_root

    def publish(self) -> CallFactPublishSummary:
        calls = sorted(self.ready_calls.glob("pc_*.json"))
        if self.cloud_handoff_root is None:
            return CallFactPublishSummary(status="CALL_FACT_HANDOFF_ROOT_UNCONFIGURED")
        root = validate_cloud_handoff_root(self.data_root, self.cloud_handoff_root)
        route = root / self.ROUTE_DIRECTORY
        route.mkdir(parents=True, exist_ok=True)
        summary = CallFactPublishSummary(status="CALL_FACT_HANDOFF_READY")
        publications = self.state.data.setdefault("call_fact_handoff", {}).setdefault("publications", {})
        for source in calls:
            try:
                value = json.loads(source.read_text(encoding="utf-8"))
                validate_phone_call(value)
                rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                destination = route / f"{value['call_id']}.json"
                existed = destination.exists()
                if existed and destination.read_text(encoding="utf-8") == rendered:
                    summary.already_published += 1
                else:
                    descriptor, temporary_name = tempfile.mkstemp(prefix=".call-fact-", suffix=".json", dir=route)
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                            handle.write(rendered)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary_name, destination)
                    finally:
                        if os.path.exists(temporary_name):
                            os.unlink(temporary_name)
                    if existed:
                        summary.updated += 1
                    else:
                        summary.published += 1
                publications[value["call_id"]] = {
                    "relative_path": f"{self.ROUTE_DIRECTORY}/{destination.name}",
                    "status": "LOCAL_SYNC_ROOT_PUBLISHED",
                }
            except Exception:
                summary.failures += 1
        if self.ready_links is not None:
            link_route = root / self.LINK_ROUTE_DIRECTORY
            link_route.mkdir(parents=True, exist_ok=True)
            for source in sorted(self.ready_links.glob("lnk_*.json")):
                try:
                    value = json.loads(source.read_text(encoding="utf-8"))
                    validate_call_recording_link(value)
                    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                    destination = link_route / f"{value['link_id']}.json"
                    existed = destination.exists()
                    if existed and destination.read_text(encoding="utf-8") == rendered:
                        summary.links_already_published += 1
                        continue
                    descriptor, temporary_name = tempfile.mkstemp(prefix=".call-link-", suffix=".json", dir=link_route)
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                            handle.write(rendered)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary_name, destination)
                    finally:
                        if os.path.exists(temporary_name):
                            os.unlink(temporary_name)
                    if existed:
                        summary.links_updated += 1
                    else:
                        summary.links_published += 1
                except Exception:
                    summary.failures += 1
        return summary
