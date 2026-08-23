"""Measure production-size L1 full export and one-month correction reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.foundation.filesystem import write_bytes_atomic
from baibai_engine.market.lake.identity import verified_git_commit
from baibai_engine.market.lake.objects import sha256_file
from baibai_engine.market.lake.writer import LakeExportReport, export_lake_legacy
from baibai_engine.market.sqlite.snapshot import create_snapshot


def _implementation_sha256() -> str:
    root = Path(__file__).resolve().parents[2]
    paths = (
        Path(__file__).resolve(),
        root / "engine/src/baibai_engine/market/lake/writer.py",
        root / "engine/src/baibai_engine/market/lake/models.py",
        root / "engine/src/baibai_engine/market/lake/immutable.py",
        root / "engine/src/baibai_engine/market/sqlite/snapshot.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _report_build(build: LakeExportReport) -> dict[str, object]:
    return {
        name: {
            "rows": result.manifest.totals.rows,
            "objects": result.manifest.totals.objects,
            "bytes": result.manifest.totals.bytes,
            "partitions": len(result.manifest.partitions),
            "changed_partitions": list(result.changed_partitions),
            "reused_partitions": list(result.reused_partitions),
            "created_objects": result.created_objects,
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
    with sqlite3.connect(sqlite_path) as connection:
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
    with tempfile.TemporaryDirectory(prefix="baibai-l1-export-acceptance-") as temporary:
        root = Path(temporary)
        working_sqlite = root / "market.sqlite"
        mirror = root / "mirror"
        create_snapshot(sqlite_path, working_sqlite)
        source_sha256 = sha256_file(working_sqlite)

        started = time.perf_counter()
        full = export_lake_legacy(
            sqlite_path=working_sqlite,
            mirror_root=mirror,
            producer_git_commit=producer_commit,
            expected_store_origin=None,
        )
        full_seconds = time.perf_counter() - started
        baseline_objects = _object_inventory(full)

        corrected_month = _inject_one_month_correction(working_sqlite)
        incremental_source_sha256 = sha256_file(working_sqlite)
        started = time.perf_counter()
        incremental = export_lake_legacy(
            sqlite_path=working_sqlite,
            mirror_root=mirror,
            producer_git_commit=producer_commit,
            expected_store_origin=None,
            base_manifest_paths={
                name: result.manifest_path for name, result in full.datasets.items()
            },
        )
        incremental_seconds = time.perf_counter() - started
        incremental_objects = _object_inventory(incremental)
        new_keys = set(incremental_objects) - set(baseline_objects)

        payload: dict[str, object] = {
            "kind": "l1_full_history_export_acceptance",
            "status": "passed",
            "recorded_at": datetime.now(UTC).isoformat(),
            "producer_git_commit": producer_commit,
            "implementation_sha256": _implementation_sha256(),
            "source": {
                "sha256": source_sha256,
                "bytes": working_sqlite.stat().st_size,
            },
            "full": {
                "elapsed_seconds": full_seconds,
                "datasets": _report_build(full),
            },
            "incremental_correction": {
                "corrected_month": corrected_month,
                "source_sha256": incremental_source_sha256,
                "elapsed_seconds": incremental_seconds,
                "new_object_count": len(new_keys),
                "new_object_bytes": sum(incremental_objects[key] for key in new_keys),
                "datasets": _report_build(incremental),
            },
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
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
