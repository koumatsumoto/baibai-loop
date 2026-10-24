"""One scheduled canonical integration, independent of acquisition completion."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404
import sys
from datetime import date
from pathlib import Path

from baibai_batch.jobs.source_handoff import _command, _replay, complete
from baibai_batch.observability.discord import write_json_atomic
from baibai_batch.storage.economic_receipts import accept_rechecks
from baibai_batch.storage.lake_publish import Boto3R2Store
from baibai_batch.storage.merge_economic_store import merge_stores, verify_stores
from baibai_batch.storage.publication_lease import _load_handle, verify_held
from baibai_batch.storage.publish_market_lake import _serving_pointer
from baibai_batch.storage.source_inputs import publish_inputs
from baibai_batch.storage.source_receipts import available_handoffs, download_fixed, seal_integrated
from baibai_engine.batch_api import DEFAULT_ECONOMIC_DB_PATH, MARKET_DB_PATH


def apply(
    store: Boto3R2Store, directory: Path, *, market: Path, economic: Path, asof: date,
) -> dict[str, object]:
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    outcomes = []
    # Every ready input was frozen before hydrate. New arrivals wait for the next
    # scheduled integration; no source job or matrix is a prerequisite.
    for index, entry in enumerate(plan["handoffs"]):
        packet = directory / f"receipt-{index:05d}"
        download_fixed(store, entry, packet)
        if entry["market"]:
            result = _replay(entry["source"], market, packet, extract=False)
            recovery = not complete(result)
        else:
            accepted = packet / "accepted.sqlite"
            result = accept_rechecks(packet / "receipts.sqlite", economic, accepted)
            merge_stores(accepted, economic)
            recovery = bool(result["late_recovery_required"])
        outcomes.append({"entry": entry, "result": result, "recovery": recovery})
    extractions = {}
    for source in ("edinet-metrics", "edinet-facts"):
        result = subprocess.run(  # nosec B603
            _command(source, asof, offline=True), check=False, text=True, capture_output=True,
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        extractions[source] = {
            "exit_code": result.returncode,
            "status": "skipped_not_initialized" if "skipped=not_initialized" in result.stdout else "completed" if result.returncode == 0 else "failed",
        }
    report = {"asof": asof.isoformat(), "handoffs": outcomes, "extractions": extractions}
    write_json_atomic(directory / "applied.json", report)
    return report


def canonical_generations() -> dict[str, str]:
    stores = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
    pointer = _serving_pointer(stores).pointer
    if pointer is None:
        raise ValueError("common integration requires a published lake release")
    generation = Path(os.environ.get("R2_GENERATION_DIR") or "stores/.r2-generations") / "indicators.sqlite.etag"
    return {
        "lake_release_id": pointer.release_id, "lake_manifest_sha256": pointer.manifest_sha256,
        "economic_etag": generation.read_text(encoding="utf-8").strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "verify", "seal", "inputs", "check"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--economic", type=Path, default=DEFAULT_ECONOMIC_DB_PATH)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--identity")
    parser.add_argument("--lease-handle", type=Path)
    args = parser.parse_args(argv)
    try:
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        directory = args.directory
        directory.mkdir(parents=True, exist_ok=True)
        if args.command == "plan":
            if (directory / "plan.json").exists():
                raise FileExistsError("integration inputs are already fixed")
            write_json_atomic(directory / "plan.json", {"handoffs": available_handoffs(store)})
        elif args.command == "apply":
            if args.asof is None:
                raise ValueError("--asof is required")
            report = apply(store, directory, market=args.market, economic=args.economic, asof=args.asof)
            print(json.dumps(report, sort_keys=True))
        elif args.command == "check":
            report = json.loads((directory / "applied.json").read_text())
            return int(any(item["recovery"] for item in report["handoffs"]) or any(item["exit_code"] for item in report["extractions"].values()))
        elif args.command == "verify":
            report = json.loads((directory / "applied.json").read_text(encoding="utf-8"))
            for index, outcome in enumerate(report["handoffs"]):
                if not outcome["entry"]["market"]:
                    verify_stores(directory / f"receipt-{index:05d}" / "accepted.sqlite", args.economic)
        else:
            if args.lease_handle is None:
                raise ValueError("common integration writes require the publication lease")
            verify_held(store, _load_handle(args.lease_handle))
            generations = canonical_generations()
            if args.command == "inputs":
                if args.identity is None or args.asof is None:
                    raise ValueError("--identity and --asof are required")
                publish_inputs(store, market=args.market, economic=args.economic,
                               identity=args.identity, asof=args.asof, generations=generations)
            else:
                report = json.loads((directory / "applied.json").read_text(encoding="utf-8"))
                for outcome in report["handoffs"]:
                    seal_integrated(store, outcome["entry"], generations=generations, recovery=outcome["recovery"])
                report["canonical_generations"] = generations
                write_json_atomic(directory / "applied.json", report)
    except Exception as error:
        print(f"common source integration failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
