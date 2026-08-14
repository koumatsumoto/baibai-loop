"""Measure a production-size fixed-release projection against explicit budgets."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import shutil
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from baibai_engine.market.lake.datasets import PILOT_DATASETS
from baibai_engine.market.lake.objects import open_lake
from baibai_engine.market.lake.projection import ProjectionError, build_projection
from baibai_engine.market.lake.reader import LakeReadError, resolve_release

MIN_BASELINE_ROWS = {
    "jquants.daily_bars": 10_136_873,
    "jquants.short_sale_reports": 1_412_135,
}
MAX_COLD_SECONDS = 1_800.0
MAX_REUSE_SECONDS = 300.0
MAX_PEAK_RSS_BYTES = 4 * 1024**3
MIN_FREE_DISK_BYTES = 2 * 1024**3
MAX_PROJECTED_OUTPUT_BYTES = 8 * 1024**3
MAX_QUERY_P95_MS = 100.0
THREE_YEAR_GROWTH_FACTOR = 1.5


@dataclass
class ResourceSamples:
    parent: Path
    stop: threading.Event
    thread: threading.Thread | None = None
    peak_rss_bytes: int = 0
    minimum_free_disk_bytes: int = 2**63 - 1

    def __enter__(self) -> ResourceSamples:
        self._sample()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_error: object) -> None:
        self.stop.set()
        assert self.thread is not None
        self.thread.join()
        self._sample()

    def _run(self) -> None:
        while not self.stop.wait(0.05):
            self._sample()

    def _sample(self) -> None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self.peak_rss_bytes = max(self.peak_rss_bytes, int(usage.ru_maxrss) * 1024)
        self.minimum_free_disk_bytes = min(
            self.minimum_free_disk_bytes,
            shutil.disk_usage(self.parent).free,
        )


def _timed(operation: Callable[[], Any], *, parent: Path) -> tuple[Any, float, ResourceSamples]:
    samples = ResourceSamples(parent=parent, stop=threading.Event())
    started = time.perf_counter()
    with samples:
        result = operation()
    return result, time.perf_counter() - started, samples


def _p95_ms(samples: list[float]) -> float:
    return sorted(samples)[max(0, (len(samples) * 95 + 99) // 100 - 1)] * 1_000


def _query_evidence(path: Path) -> dict[str, object]:
    queries = {
        "daily_bar_point": (
            "SELECT close FROM jquants_daily_bars WHERE ticker = ? AND traded_at = ?",
            "SELECT ticker, traded_at FROM jquants_daily_bars LIMIT 1",
        ),
        "daily_bar_date": (
            "SELECT COUNT(*) FROM jquants_daily_bars WHERE traded_at = ?",
            "SELECT traded_at FROM jquants_daily_bars LIMIT 1",
        ),
        "short_sale_ticker": (
            "SELECT COUNT(*) FROM jquants_short_sale_reports WHERE ticker = ?",
            "SELECT ticker FROM jquants_short_sale_reports LIMIT 1",
        ),
    }
    evidence: dict[str, object] = {}
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        for name, (statement, parameter_query) in queries.items():
            parameters = connection.execute(parameter_query).fetchone()
            if parameters is None:
                raise RuntimeError(f"representative query has no input row: {name}")
            plan = " ".join(
                str(row[3])
                for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)
            )
            if "INDEX" not in plan:
                raise RuntimeError(f"representative query does not use an index: {name}")
            durations: list[float] = []
            for _ in range(20):
                started = time.perf_counter()
                connection.execute(statement, parameters).fetchall()
                durations.append(time.perf_counter() - started)
            evidence[name] = {"p95_ms": _p95_ms(durations), "plan": plan}
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        rows = {
            name: int(
                connection.execute(f"SELECT COUNT(*) FROM {dataset.sqlite_table}").fetchone()[0]
            )  # nosec B608
            for name, dataset in sorted(PILOT_DATASETS.items())
        }
        stats = int(connection.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0])
    if quick_check != ("ok",):
        raise RuntimeError("projection quick_check failed")
    return {"queries": evidence, "rows": rows, "sqlite_stat_rows": stats}


def _acceptance(payload: dict[str, Any]) -> dict[str, bool]:
    cold = float(payload["cold_seconds"])
    reuse = float(payload["reuse_seconds"])
    output = int(payload["output_bytes"])
    rows = dict(payload["rows"])
    queries = dict(payload["queries"])
    return {
        "baseline_rows": all(
            int(rows.get(dataset, 0)) >= minimum for dataset, minimum in MIN_BASELINE_ROWS.items()
        ),
        "cold_build": cold <= MAX_COLD_SECONDS,
        "warm_reuse": reuse <= MAX_REUSE_SECONDS,
        "peak_rss": int(payload["peak_rss_bytes"]) <= MAX_PEAK_RSS_BYTES,
        "free_disk": int(payload["minimum_free_disk_bytes"]) >= MIN_FREE_DISK_BYTES,
        "three_year_cold": cold * THREE_YEAR_GROWTH_FACTOR <= MAX_COLD_SECONDS,
        "three_year_reuse": reuse * THREE_YEAR_GROWTH_FACTOR <= MAX_REUSE_SECONDS,
        "three_year_output": output * THREE_YEAR_GROWTH_FACTOR <= MAX_PROJECTED_OUTPUT_BYTES,
        "query_latency": all(
            float(dict(item)["p95_ms"]) <= MAX_QUERY_P95_MS for item in queries.values()
        ),
        "analyze_statistics": int(payload["sqlite_stat_rows"]) > 0,
    }


def _implementation_sha256() -> str:
    """Bind evidence to every lake module and to this report generator."""
    repo_root = Path(__file__).resolve().parents[2]
    paths = sorted((repo_root / "engine/src/baibai_engine/market/lake").glob("*.py"))
    paths.append(Path(__file__).resolve())
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(repo_root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark_lake_projection", description=__doc__)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--bucket")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.projection.parent.mkdir(parents=True, exist_ok=True)
        with open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache):
            release = resolve_release(
                cache.source,
                args.release,
                manifest_sha256=args.manifest_sha256,
            )
            commits = {
                manifest.producer_git_commit for manifest in release.dataset_manifests.values()
            }
            if len(commits) != 1:
                raise RuntimeError("release datasets do not share one producer git commit")
            producer_git_commit = next(iter(commits))
            cold, cold_seconds, cold_resources = _timed(
                lambda: build_projection(
                    session,
                    release=release,
                    cache=cache,
                    destination=args.projection,
                    dataset_names=tuple(sorted(PILOT_DATASETS)),
                    producer_git_commit=producer_git_commit,
                    force=True,
                ),
                parent=args.projection.parent,
            )
            warm, reuse_seconds, warm_resources = _timed(
                lambda: build_projection(
                    session,
                    release=release,
                    cache=cache,
                    destination=args.projection,
                    dataset_names=tuple(sorted(PILOT_DATASETS)),
                    producer_git_commit=producer_git_commit,
                ),
                parent=args.projection.parent,
            )
        if warm.reused is not True:
            raise RuntimeError("unchanged projection was not reused")
        query = _query_evidence(args.projection)
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "lake_projection_scale_acceptance",
            "source_release_id": cold.identity.source_release_id,
            "source_release_manifest_sha256": cold.identity.source_release_manifest_sha256,
            "source_producer_git_commit": cold.identity.producer_git_commit,
            "projection_contract_version": cold.identity.projection_contract_version,
            "projection_fingerprint": cold.identity.projection_fingerprint,
            "benchmark_implementation_sha256": _implementation_sha256(),
            "platform": platform.platform(),
            "duckdb_version": duckdb.__version__,
            "sqlite_version": sqlite3.sqlite_version,
            "cold_seconds": cold_seconds,
            "reuse_seconds": reuse_seconds,
            "peak_rss_bytes": max(cold_resources.peak_rss_bytes, warm_resources.peak_rss_bytes),
            "minimum_free_disk_bytes": min(
                cold_resources.minimum_free_disk_bytes,
                warm_resources.minimum_free_disk_bytes,
            ),
            "output_bytes": args.projection.stat().st_size,
            **query,
        }
        acceptance = _acceptance(payload)
        payload["budgets"] = {
            "max_cold_seconds": MAX_COLD_SECONDS,
            "max_reuse_seconds": MAX_REUSE_SECONDS,
            "max_peak_rss_bytes": MAX_PEAK_RSS_BYTES,
            "minimum_free_disk_bytes": MIN_FREE_DISK_BYTES,
            "max_projected_output_bytes": MAX_PROJECTED_OUTPUT_BYTES,
            "max_query_p95_ms": MAX_QUERY_P95_MS,
            "three_year_growth_factor": THREE_YEAR_GROWTH_FACTOR,
        }
        payload["acceptance"] = acceptance
        payload["status"] = "passed" if all(acceptance.values()) else "failed"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (LakeReadError, ProjectionError, OSError, RuntimeError, ValueError) as error:
        print(f"projection benchmark failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
