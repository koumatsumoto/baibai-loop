"""Measure a production-size lake export, and what a one-month correction moves.

Both runs derive every partition. Timings and object reuse are measurements, not
fixed-cost guarantees. This diagnostic reports the effect of one synthetic row
correction; it does not replace export parity or production acceptance checks.
"""

from __future__ import annotations

import argparse
import json
import resource
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.foundation.filesystem import write_bytes_atomic
from baibai_engine.storage.lake.identity import verified_git_commit
from baibai_engine.storage.lake.sources import sha256_file
from baibai_engine.storage.lake.writer import LakeExportReport, export_market_store
from baibai_engine.storage.observations.lake_origin import read_lake_store_origin
from baibai_engine.storage.observations.snapshot import create_snapshot


def _report_build(build: LakeExportReport) -> dict[str, object]:
    return {
        name: {
            "rows": result.manifest.totals.rows,
            "objects": result.manifest.totals.objects,
            "bytes": result.manifest.totals.bytes,
            "partitions": result.partitions,
            "new_objects": result.new_objects,
            "manifest_sha256": sha256_file(result.manifest_path),
        }
        for name, result in sorted(build.datasets.items())
    }


def _object_inventory(build: LakeExportReport) -> dict[str, int]:
    return {
        item.key: item.bytes
        for result in build.datasets.values()
        for partition in result.manifest.partitions
        for item in partition.objects
    }


def _inject_one_month_correction(sqlite_path: Path) -> str:
    with closing(sqlite3.connect(sqlite_path)) as connection, connection:
        row = connection.execute(
            "SELECT ticker, traded_at FROM jquants_daily_bars "
            "WHERE close IS NOT NULL ORDER BY traded_at DESC, ticker DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("daily bars contain no correctable row")
        connection.execute(
            "UPDATE jquants_daily_bars SET close = close + 0.000001 "
            "WHERE ticker = ? AND traded_at = ?",
            row,
        )
    return str(row[1])[:7]


def benchmark(*, sqlite_path: Path, report_path: Path, producer_commit: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="baibai-lake-export-acceptance-") as temporary:
        root = Path(temporary)
        working_sqlite = root / "market.sqlite"
        mirror = root / "mirror"
        create_snapshot(sqlite_path, working_sqlite)
        source_sha256 = sha256_file(working_sqlite)
        source_bytes = working_sqlite.stat().st_size
        store_origin = read_lake_store_origin(working_sqlite)

        started = time.perf_counter()
        full = export_market_store(
            sqlite_path=working_sqlite,
            mirror_root=mirror,
            producer_git_commit=producer_commit,
            expected_store_origin=store_origin,
        )
        full_seconds = time.perf_counter() - started
        baseline_objects = _object_inventory(full)

        corrected_month = _inject_one_month_correction(working_sqlite)
        corrected_source_sha256 = sha256_file(working_sqlite)
        started = time.perf_counter()
        corrected = export_market_store(
            sqlite_path=working_sqlite,
            mirror_root=mirror,
            producer_git_commit=producer_commit,
            expected_store_origin=store_origin,
        )
        corrected_seconds = time.perf_counter() - started
        corrected_objects = _object_inventory(corrected)
        new_keys = set(corrected_objects) - set(baseline_objects)

        payload: dict[str, object] = {
            "kind": "lake_full_history_export_benchmark",
            "status": "measured",
            "recorded_at": datetime.now(UTC).isoformat(),
            "producer_git_commit": producer_commit,
            "source": {
                "sha256": source_sha256,
                "bytes": source_bytes,
            },
            "full": {
                "elapsed_seconds": full_seconds,
                "datasets": _report_build(full),
            },
            "correction": {
                "corrected_month": corrected_month,
                "source_sha256": corrected_source_sha256,
                "elapsed_seconds": corrected_seconds,
                "new_object_count": len(new_keys),
                "new_object_bytes": sum(corrected_objects[key] for key in new_keys),
                "datasets": _report_build(corrected),
            },
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
        }
        write_bytes_atomic(
            report_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    payload = benchmark(
        sqlite_path=args.sqlite,
        report_path=args.report,
        producer_commit=verified_git_commit(),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
