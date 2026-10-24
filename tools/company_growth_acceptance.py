"""CI専用の有限実データ受入。原数値は非公開R2の新規検証keyだけへ保存する。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import cast

from baibai_batch.storage.lake_publish import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.company_growth.models import CompanyGrowth, InputRelease
from baibai_engine.company_growth.query import company_growth
from baibai_engine.foundation.json import canonical_json
from baibai_engine.research.company_growth_projection import project_company_growth
from baibai_engine.storage.lake.datasets import JQUANTS_FIN_SUMMARIES, JQUANTS_MASTER_SNAPSHOTS
from baibai_engine.storage.lake.objects import LakeObjectCache
from baibai_engine.storage.lake.reader import (
    accepted_dataset,
    materialize_partitions,
    materialize_release_metadata,
    resolve_current_release,
    selected_partitions,
)
from baibai_engine.storage.lake.sources import sha256_file
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.observations.schema import open_connection
from tools.company_growth_sample import Sample, SampleInput, measure_samples

_TICKERS = ("4502", "7201", "6758", "8306", "9983")


class _StoreSource:
    """Use the existing authenticated R2 transport for the existing lake reader/cache."""

    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def read_bytes(self, key: str) -> bytes:
        return self.store.get_bytes(key)

    def download_file(self, key: str, destination: Path, *, max_bytes: int) -> None:
        self.store.download_file(key, destination, expect_bytes=max_bytes)

    def uri(self, key: str) -> str:
        raise NotImplementedError("this acceptance reads only materialized local files")


def result_key(run_id: str, attempt: str, head_sha: str) -> str:
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise ValueError("invalid Actions run identity")
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("invalid acceptance commit")
    return f"validation/company-growth/pr-1592/{run_id}-{attempt}/{head_sha}/report.json"


def _core_matches(history: CompanyGrowth, window: CompanyGrowth) -> bool:
    start, end = window.requested_start, window.requested_end
    return window.periods == tuple(
        p
        for p in history.periods
        if (start is None or start <= p.period_end) and p.period_end <= end
    ) and window.changes == tuple(
        c for c in history.changes if (start is None or start <= c.to_end) and c.to_end <= end
    )


def execute(
    store: ObjectStore,
    directory: Path,
    *,
    run_id: str,
    attempt: str,
    head_sha: str,
    tickers: tuple[str, ...] = _TICKERS,
    saved_result_key: str | None = None,
) -> dict[str, object]:
    key = result_key(run_id, attempt, head_sha)
    if directory.exists():
        raise FileExistsError("acceptance workspace already exists")
    directory.mkdir(parents=True, mode=0o700)
    if saved_result_key:
        if not re.fullmatch(
            r"validation/company-growth/pr-1592/[0-9]+-[1-9][0-9]*/[0-9a-f]{40}/report\.json",
            saved_result_key,
        ):
            raise ValueError("expected an existing private company growth validation result")
        saved_report = json.loads(store.get_bytes(saved_result_key))
        adopted = InputRelease.model_validate(saved_report["input_release"])
        projections: dict[str, object] = {}
        rows: list[dict[str, object]] = []
        for ticker, payload in saved_report["retained_histories"].items():
            history = CompanyGrowth.model_validate(payload)
            if history.input_release != adopted:
                raise ValueError("saved history differs from its fixed release")
            started = perf_counter()
            projection = project_company_growth(payload, ticker=ticker)
            elapsed = perf_counter() - started
            projections[ticker] = projection
            rows.append(
                {
                    "ticker": ticker,
                    "retained_period_count": len(history.periods),
                    "history_json_bytes": len(canonical_json(payload).encode()),
                    "l3_projection_json_bytes": len(canonical_json(projection).encode()),
                    "projection_seconds": elapsed,
                }
            )
        public = {
            "status": "measured",
            "head_sha": head_sha,
            "sample_count": len(rows),
            "samples": rows,
            "saved_result_key": saved_result_key,
            "private_result_key": key,
            "l3_value_review": "pending",
        }
        return _save_private_result(
            store,
            directory,
            public,
            {
                "summary": public,
                "input_release": adopted.model_dump(mode="json"),
                "saved_result_key": saved_result_key,
                "l3_projections": projections,
            },
        )
    mirror = directory / "mirror"
    cache = LakeObjectCache(root=mirror, source=_StoreSource(store))
    started = perf_counter()
    release = resolve_current_release(cache.source)
    materialize_release_metadata(cache, release)
    for name in (JQUANTS_FIN_SUMMARIES.name, JQUANTS_MASTER_SNAPSHOTS.name):
        materialize_partitions(
            cache,
            dataset=accepted_dataset(release, name),
            partitions=selected_partitions(release, name),
        )
    preparation_seconds = perf_counter() - started
    # This is an ephemeral origin reference, not a copy of holdings or a second market authority.
    database = directory / "origin-reference.sqlite"
    with closing(open_connection(database)) as connection, connection:
        write_lake_store_origin(
            connection, LakeStoreOrigin(release.release_id, release.manifest_sha256)
        )
    as_of = datetime.now(UTC).date()
    histories: dict[str, CompanyGrowth] = {}
    history_times: dict[str, float] = {}
    samples: list[Sample] = []
    for ticker in tickers:
        started = perf_counter()
        history = company_growth(database, ticker=ticker, as_of=as_of, mirror=mirror)
        history_times[ticker] = perf_counter() - started
        histories[ticker] = history
        periods = history.periods[-12:]
        if periods:
            samples.append(
                Sample(ticker=ticker, start=periods[0].period_end, end=periods[-1].period_end)
            )
    adopted = InputRelease(release_id=release.release_id, manifest_sha256=release.manifest_sha256)
    sample_report = (
        measure_samples(
            database,
            SampleInput(input_release=adopted, as_of=as_of, samples=tuple(samples)),
            mirror=mirror,
        )
        if samples
        else {"samples": [], "acceptance": "no_observed_accounting_periods"}
    )
    measured = cast("list[dict[str, object]]", sample_report["samples"])
    windows = {
        str(sample["ticker"]): CompanyGrowth.model_validate(sample["company_growth"])
        for sample in measured
    }
    summary_rows: list[dict[str, object]] = []
    for ticker, history in histories.items():
        window = windows.get(ticker)
        count = len(window.periods) if window is not None else 0
        item = next((row for row in measured if row["ticker"] == ticker), {})
        summary_rows.append(
            {
                "ticker": ticker,
                "status": history.status,
                "retained_period_count": len(history.periods),
                "sample_period_count": count,
                "history_query_seconds": history_times[ticker],
                "sample_query_seconds": item.get("query_seconds"),
                "history_json_bytes": len(canonical_json(history.model_dump(mode="json")).encode()),
                "sample_json_bytes": item.get("company_growth_json_bytes"),
                "thin_table_json_bytes": item.get("thin_reported_table_json_bytes"),
                "window_invariance": window is not None and _core_matches(history, window),
                "minimum_eight_periods": count >= 8,
            }
        )
    passed = all(row["window_invariance"] and row["minimum_eight_periods"] for row in summary_rows)
    public = {
        "status": "measured" if passed else "measured_with_input_gaps",
        "head_sha": head_sha,
        "sample_count": len(samples),
        "preparation_seconds": preparation_seconds,
        "parquet_downloaded_bytes": cache.transfers.fetched_bytes,
        "parquet_downloaded_objects": cache.transfers.fetched_objects,
        "samples": summary_rows,
        "private_result_key": key,
        "l3_value_review": "pending",
    }
    private = {
        "summary": public,
        "input_release": adopted.model_dump(mode="json"),
        "sample_report": sample_report,
        "retained_histories": {t: h.model_dump(mode="json") for t, h in histories.items()},
    }
    return _save_private_result(store, directory, public, private)


def _save_private_result(
    store: ObjectStore, directory: Path, public: dict[str, object], private: dict[str, object]
) -> dict[str, object]:
    key = str(public["private_result_key"])
    payload = canonical_json(private).encode()
    report = directory / "report.json"
    report.write_bytes(payload)
    report.chmod(0o600)
    digest = hashlib.sha256(payload).hexdigest()
    saved = store.put_file(
        key,
        report,
        sha256=digest,
        content_md5=base64.b64encode(hashlib.md5(payload, usedforsecurity=False).digest()).decode(),
        content_type="application/json",
        if_none_match=True,
    )
    if saved.size != len(payload) or saved.metadata.get("sha256") != digest:
        raise LakePublishError("acceptance result write receipt mismatch")
    readback = directory / "readback.json"
    store.download_file(key, readback, expect_bytes=len(payload))
    if sha256_file(readback) != digest:
        raise LakePublishError("acceptance result readback mismatch")
    return {**public, "private_result_bytes": len(payload), "private_result_sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--saved-result-key", default="")
    args = parser.parse_args()
    try:
        summary = execute(
            Boto3R2Store(bucket="baibai-stores"),
            args.directory,
            run_id=args.run_id,
            attempt=args.attempt,
            head_sha=args.head_sha,
            saved_result_key=args.saved_result_key or None,
        )
    except Exception as error:
        # Public Actions logs must not receive native records, numeric facts or endpoints.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "measured" else 3


if __name__ == "__main__":
    raise SystemExit(main())
