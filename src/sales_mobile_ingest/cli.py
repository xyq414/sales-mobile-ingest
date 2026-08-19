from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .bridge import BridgeError
from .config import ConfigError, resolve_data_root
from .service import Ingestor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sales-mobile-ingest")
    parser.add_argument("--data-root", help="Local data root; no drive letter is assumed by the application")
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe", help="Read-only portable-device and candidate-directory discovery")
    probe.add_argument("--save-report", action="store_true", help="Save a privacy-minimal local diagnostic report")
    probe.add_argument("--data-root", dest="command_data_root", help=argparse.SUPPRESS)
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
