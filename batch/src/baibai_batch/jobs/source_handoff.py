"""Private original-receipt handoffs; publishers replay onto freshly hydrated stores."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from baibai_batch.observability.discord import write_json_atomic
from baibai_batch.storage.economic_staging import load_handoff, save_handoff
from baibai_batch.storage.lake_publish import Boto3R2Store
from baibai_engine.batch_api import (
    MARKET_DB_PATH,
    TradingViewHandoff,
    export_tradingview_handoff,
    import_tradingview_handoff,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "stage", "download", "replay"))
    parser.add_argument("--source", choices=("tradingview",), required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--stage-id")
    args = parser.parse_args(argv)
    directory = args.directory
    try:
        if args.command in {"stage", "download"}:
            if args.stage_id is None:
                raise ValueError("--stage-id is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            if args.command == "stage":
                save_handoff(store, args.stage_id, directory, market=True)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print(f"handoff={args.stage_id}", file=stream)
            else:
                load_handoff(store, args.stage_id, directory, market=True)
        elif args.command == "export":
            if args.asof is None or args.since is None:
                raise ValueError("--asof and --since are required")
            directory.mkdir(parents=True, exist_ok=True)
            handoff = export_tradingview_handoff(
                args.database, directory / "receipts.sqlite", day=args.asof, since=args.since
            )
            write_json_atomic(directory / "result.json", {"source": args.source, **handoff.to_json()})
        else:
            report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if report["source"] != args.source:
                raise ValueError("handoff source differs from the publication plan")
            result = import_tradingview_handoff(
                directory / "receipts.sqlite", args.database, TradingViewHandoff.from_json(report)
            )
            write_json_atomic(directory / "replayed.json", result)
            print(json.dumps(result, sort_keys=True))
    except Exception as error:
        # Raw provider payload and credentials must not enter cross-job output.
        print(f"source handoff failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
