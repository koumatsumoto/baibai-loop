"""固定L1だけからEDINET metricsの比較用候補を再生成する。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing, redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import read_edinet_input
from baibai_engine.machine_analysis.observations.financials.edinet_integration import (
    extract_saved_edinet,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.storage.filesystem import fsync_directory
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.integration.edinet_list_native import materialize_lists


def rebuild_fixed_edinet_metrics(
    lists: FixedEdinetLists,
    archives: FixedEdinetArchives,
    output: Path,
    *,
    asof: date,
    lookback_days: int = 540,
) -> dict[str, object]:
    """Produce a new candidate, never read an old market or replace its snapshot.

    Fresh list projection supplies retained origins and eligibility evidence.
    Only selected CSV ZIPs are read from the fixed owner; no ZIPs, previous L2,
    facts scope or extraction-success history enter the candidate database.
    """
    if lookback_days < 0:
        raise ValueError("EDINET metric lookback must not be negative")
    if output.exists() or output.is_symlink():
        raise FileExistsError("EDINET metric rebuild requires a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="edinet-metrics-", dir=output.parent) as temporary:
        database = Path(temporary) / "candidate.sqlite"
        list_report = materialize_lists(lists, database)
        metrics = extract_saved_edinet(
            database, asof=asof, lookback_days=lookback_days, metric_archives=archives
        )["edinet-metrics"]
        output_written = metrics["status"] in {"completed", "partial"}
        if output_written:
            os.link(database, output)
            fsync_directory(output.parent)
    return {
        "list_identity": lists.identity,
        "archive_identity": archives.identity,
        "requested_asof": asof.isoformat(),
        "lookback_days": lookback_days,
        "extractor_revision": EDINET_METRIC_REVISION,
        "output_written": output_written,
        "edinet-list": list_report,
        "edinet-metrics": metrics,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs rebuild-edinet-metrics", description=__doc__)
    parser.add_argument(
        "--saved-input", type=Path, required=True, help="saved EDINET InputSnapshot JSON"
    )
    parser.add_argument("--output", type=Path, required=True, help="new candidate SQLite path")
    parser.add_argument("--asof", type=date.fromisoformat, required=True)
    parser.add_argument("--lookback-days", type=int, default=540)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        saved = InputSnapshot.model_validate_json(args.saved_input.read_bytes())
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            fixed = read_edinet_input(store, saved)
            with redirect_stdout(sys.stderr):
                report = rebuild_fixed_edinet_metrics(
                    fixed.lists,
                    fixed.archives,
                    args.output,
                    asof=args.asof,
                    lookback_days=args.lookback_days,
                )
        report["edinet_input"] = fixed.identity.model_dump(mode="json")
        report["selected_at"] = fixed.selected_at.isoformat()
        print(json.dumps(report, sort_keys=True))
    except Exception as error:
        print(f"EDINET saved metric rebuild failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0 if report["output_written"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
