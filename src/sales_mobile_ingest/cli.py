from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .android_calllog_probe import summarize_private_result
from .bridge import BridgeError
from .cloud_handoff import (
    inspect_jianguoyun_environment,
    sanitize_windows_component,
    validate_cloud_handoff_root,
    validate_cloud_package,
)
from .config import (
    ConfigError,
    resolve_cloud_handoff_root,
    resolve_data_root,
    update_local_config,
)
from .service import Ingestor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sales-mobile-ingest")
    parser.add_argument("--data-root", help="Local data root; no drive letter is assumed by the application")
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe", help="Read-only portable-device and candidate-directory discovery")
    probe.add_argument("--save-report", action="store_true", help="Save a privacy-minimal local diagnostic report")
    probe.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    identity = subparsers.add_parser("investigate-identity", help="Read-only, privacy-preserving call identity investigation")
    identity.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    calllog_export = subparsers.add_parser(
        "inspect-calllog-export",
        help="Read one bounded public CallLog XML export into ignored diagnostics and emit a value-free schema summary",
    )
    calllog_export.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    calllog_ingest = subparsers.add_parser(
        "ingest-calllog-export",
        help="Incrementally parse a verified public CallLog XML export and enrich only unique recording matches",
    )
    calllog_ingest.add_argument("--once", action="store_true", required=True, help="Run one bounded CallLog export ingest pass")
    calllog_ingest.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    calllog_summary = subparsers.add_parser(
        "calllog-probe-summary",
        help="Create a safe local summary from an app-private Android CallLog probe result",
    )
    calllog_summary.add_argument("--raw-result", required=True, type=Path, help="Gitignored app-private result copied locally")
    calllog_summary.add_argument("--event", required=True, type=Path, help="Existing local phone_call event JSON")
    calllog_summary.add_argument("--safe-output", required=True, type=Path, help="Gitignored safe summary output path")
    configure_salesperson = subparsers.add_parser(
        "configure-salesperson", help="Set the explicit local business salesperson identity used for formal cloud packages",
    )
    configure_salesperson.add_argument("--salesperson-id", required=True)
    configure_salesperson.add_argument("--salesperson-name", required=True)
    configure_salesperson.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    configure_handoff = subparsers.add_parser(
        "configure-cloud-handoff", help="Set an explicitly chosen existing cloud client sync subdirectory",
    )
    handoff_root = configure_handoff.add_mutually_exclusive_group(required=True)
    handoff_root.add_argument("--root", help="Existing absolute cloud handoff directory; never guessed")
    handoff_root.add_argument("--sync-root", help="Explicitly confirmed existing sync root; a dedicated child is created once")
    configure_handoff.add_argument("--folder-name", default="销售通话数据", help="Dedicated child directory when --sync-root is used")
    configure_handoff.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    inspect_handoff = subparsers.add_parser(
        "inspect-cloud-handoff", help="Read-only Nutstore client and sync-root candidate inspection",
    )
    inspect_handoff.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    publish_handoff = subparsers.add_parser(
        "publish-cloud-handoff", help="Build and publish complete three-file cloud call packages",
    )
    publish_handoff.add_argument("--once", action="store_true", required=True)
    publish_handoff.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    validate_package = subparsers.add_parser(
        "validate-cloud-package", help="Validate one cloud call folder without reading phone or state data",
    )
    validate_package.add_argument("--package-dir", required=True, type=Path)
    ingest = subparsers.add_parser("ingest", help="Incrementally ingest call recordings")
    ingest.add_argument("--once", action="store_true", required=True, help="Run one bounded ingest pass")
    ingest.add_argument("--limit", type=int, help="Maximum source copy attempts for this one-shot pass")
    ingest.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    watch = subparsers.add_parser("watch", help="Poll for phones and run incremental ingestion")
    watch.add_argument("--interval", type=int, default=45, help="Seconds between passes (minimum: 10)")
    watch.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data_root = resolve_data_root(getattr(args, "command_data_root", None) or args.data_root)
        ingestor = Ingestor(data_root)
        if args.command == "probe":
            result = ingestor.probe()
            payload = {"data_root": str(data_root), **result}
            if args.save_report:
                payload["saved_report"] = str(ingestor.save_probe_report())
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.command == "ingest":
            summary = ingestor.ingest_once(limit=args.limit).as_dict()
            print(json.dumps({"data_root": str(data_root), **summary}, ensure_ascii=False))
            return 0
        if args.command == "investigate-identity":
            result = ingestor.investigate_identity()
            print(json.dumps({
                "data_root": str(data_root),
                "saved_summary": str(result["summary_path"]),
                "direct_recording_identity": result["direct_recording_identity"],
                "call_log_transport": result["call_log_transport"],
                "event": result["event"],
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "inspect-calllog-export":
            summary = ingestor.inspect_calllog_exports().as_dict()
            print(json.dumps({"data_root": str(data_root), **summary}, ensure_ascii=False))
            return 0
        if args.command == "ingest-calllog-export":
            summary = ingestor.ingest_calllog_exports().as_dict()
            print(json.dumps({"data_root": str(data_root), **summary}, ensure_ascii=False))
            return 0
        if args.command == "calllog-probe-summary":
            summary = summarize_private_result(args.raw_result, args.event, args.safe_output)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.command == "configure-salesperson":
            salesperson_id = args.salesperson_id.strip()
            salesperson_name = args.salesperson_name.strip()
            if not salesperson_id or not salesperson_name:
                raise ConfigError("salesperson_id and salesperson_name must both be non-empty")
            update_local_config({"salesperson_id": salesperson_id, "salesperson_name": salesperson_name})
            print(json.dumps({"status": "SALESPERSON_IDENTITY_CONFIGURED", "config_path": "config.local.json"}, ensure_ascii=False))
            return 0
        if args.command == "configure-cloud-handoff":
            root = resolve_cloud_handoff_root(args.root or args.sync_root)
            if root is None:
                raise ConfigError("cloud_handoff_root is required")
            if args.sync_root:
                sync_root = validate_cloud_handoff_root(data_root, root)
                folder_name = sanitize_windows_component(args.folder_name)
                candidate = sync_root / folder_name
                candidate.mkdir(exist_ok=True)
                validated = validate_cloud_handoff_root(data_root, candidate)
            else:
                validated = validate_cloud_handoff_root(data_root, root)
            update_local_config({"cloud_handoff_root": str(validated)})
            print(json.dumps({"status": "CLOUD_HANDOFF_ROOT_CONFIGURED", "config_path": "config.local.json"}, ensure_ascii=False))
            return 0
        if args.command == "inspect-cloud-handoff":
            print(json.dumps(inspect_jianguoyun_environment(resolve_cloud_handoff_root()), ensure_ascii=False, indent=2))
            return 0
        if args.command == "publish-cloud-handoff":
            summary = ingestor.publish_cloud_handoff().as_dict()
            print(json.dumps({"data_root": str(data_root), **summary}, ensure_ascii=False))
            return 0
        if args.command == "validate-cloud-package":
            result = validate_cloud_package(args.package_dir)
            print(json.dumps({
                "status": "CLOUD_PACKAGE_VALID",
                "event_id": result.event_id,
                "recording_id": result.recording_id,
                "media_filename": result.media_filename,
            }, ensure_ascii=False))
            return 0
        interval = max(10, args.interval)
        while True:
            try:
                summary = ingestor.ingest_once().as_dict()
                print(json.dumps({"mode": "watch", "data_root": str(data_root), **summary}, ensure_ascii=False))
            except BridgeError as exc:
                ingestor._log("bridge_error", {"message": str(exc)[:300]})
            time.sleep(interval)
    except (ConfigError, BridgeError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
