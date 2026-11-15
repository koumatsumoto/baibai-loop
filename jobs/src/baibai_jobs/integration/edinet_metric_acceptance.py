"""Opt-in publication acceptance of previously compared EDINET metric results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import closing
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baibai_engine.machine_analysis.observations.financials.edinet_models import (
        EdinetMetricRecord,
    )
    from baibai_engine.storage.objects.r2 import ObjectStore

_FIELDS = {"as_of", "extractor_revision", "list_sha256", "archive_sha256", "samples"}
_SAMPLE_FIELDS = {
    "ticker",
    "source_doc_id",
    "source_document_revision",
    "archive_sha256",
    "metric_sha256",
}


def decode_acceptance(raw: str) -> dict[str, Any]:
    """Keep pre-credential validation independent of the installed engine environment."""
    if len(raw.encode()) > 16_384:
        raise ValueError("EDINET acceptance input is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.keys() != _FIELDS:
        raise ValueError("EDINET acceptance fields are invalid")
    asof = value["as_of"]
    if not isinstance(asof, str) or date.fromisoformat(asof).isoformat() != asof:
        raise ValueError("EDINET acceptance date must use YYYY-MM-DD")
    revision = value["extractor_revision"]
    if (
        not isinstance(revision, str)
        or re.fullmatch(r"edinet-metrics-v[0-9]+-[a-z0-9-]{1,100}", revision) is None
    ):
        raise ValueError("EDINET acceptance revision is invalid")
    samples = value["samples"]
    if not isinstance(samples, list) or not 1 <= len(samples) <= 16:
        raise ValueError("EDINET acceptance needs a bounded nonempty sample")
    digests = [value["list_sha256"], value["archive_sha256"]]
    tickers = []
    for sample in samples:
        if not isinstance(sample, dict) or sample.keys() != _SAMPLE_FIELDS:
            raise ValueError("EDINET acceptance sample fields are invalid")
        for field in ("ticker", "source_doc_id"):
            text = sample[field]
            if (
                not isinstance(text, str)
                or not 1 <= len(text) <= 32
                or not text.isascii()
                or not text.isalnum()
            ):
                raise ValueError("EDINET acceptance sample identity is invalid")
        tickers.append(sample["ticker"])
        digests.extend(
            sample[field]
            for field in ("source_document_revision", "archive_sha256", "metric_sha256")
        )
    if len(set(tickers)) != len(tickers):
        raise ValueError("EDINET acceptance samples duplicate a ticker")
    if any(
        not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in digests
    ):
        raise ValueError("EDINET acceptance requires lowercase SHA-256 digests")
    return value


def metric_result_sha256(record: EdinetMetricRecord) -> str:
    from baibai_engine.primitives.json import canonical_json

    return hashlib.sha256(canonical_json(asdict(record)).encode()).hexdigest()


def verify_acceptance(
    *, raw: str, report: dict[str, Any], database: Path, store: ObjectStore
) -> dict[str, object]:
    from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
    from baibai_engine.machine_analysis.generation.edinet_inputs import read_edinet_input
    from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
        EDINET_METRIC_REVISION,
    )
    from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
        read_edinet_metric_baseline,
    )
    from baibai_engine.storage.sqlite.source_cache.read import connect_read_only

    expected = decode_acceptance(raw)
    asof = date.fromisoformat(expected["as_of"])
    if (
        report.get("asof") != expected["as_of"]
        or expected["extractor_revision"] != EDINET_METRIC_REVISION
    ):
        raise ValueError("EDINET acceptance differs from the integration date or current revision")
    extraction = report.get("extractions", {}).get("edinet-metrics", {})
    if (
        extraction.get("status") not in {"completed", "unchanged"}
        or type(extraction.get("exit_code")) is not int
        or extraction["exit_code"] != 0
        or extraction.get("deferred_inputs") != []
        or extraction.get("input_asof") != expected["as_of"]
        or extraction.get("snapshot_asof") != expected["as_of"]
    ):
        raise ValueError("EDINET metrics did not complete for the accepted date")
    baseline = read_edinet_metric_baseline(database, asof)
    if baseline is None or baseline.asof_date != asof:
        raise ValueError("EDINET accepted metric baseline is absent")
    with closing(connect_read_only(database)) as connection:
        coverage = connection.execute(
            "SELECT status FROM source_coverage WHERE source='edinet_metrics' AND coverage_key=?",
            (asof.isoformat(),),
        ).fetchone()
    if (
        coverage is None
        or coverage[0] != "ok"
        or any(row.extractor_revision != EDINET_METRIC_REVISION for row in baseline.rows.values())
    ):
        raise ValueError("EDINET accepted baseline is partial or has a different revision")
    fixed = read_edinet_input(store, InputSnapshot.model_validate(report["edinet_input"]))
    if (
        fixed.lists.identity != expected["list_sha256"]
        or fixed.archives.identity != expected["archive_sha256"]
    ):
        raise ValueError("EDINET fixed original membership changed since comparison")
    for sample in expected["samples"]:
        row = baseline.rows.get(sample["ticker"])
        archive = fixed.archives.metadata(sample["source_doc_id"], 5)
        if (
            row is None
            or row.record.source_doc_id != sample["source_doc_id"]
            or row.source_document_revision != sample["source_document_revision"]
            or archive is None
            or archive.sha256 != sample["archive_sha256"]
            or metric_result_sha256(row.record) != sample["metric_sha256"]
        ):
            raise ValueError("EDINET saved sample differs from the compared original or result")
    return {
        "status": "accepted",
        "as_of": asof.isoformat(),
        "extractor_revision": EDINET_METRIC_REVISION,
        "metric_count": len(baseline.rows),
        "sample_count": len(expected["samples"]),
        "conditions_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "edinet_input_sha256": fixed.identity.sha256,
    }


def main(argv: list[str] | None = None) -> int:
    from baibai_engine.storage.objects.r2 import Boto3R2Store

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conditions", type=Path, required=True)
    parser.add_argument("--integration-report", type=Path, required=True)
    parser.add_argument("--market", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = args.conditions.read_text()
        decode_acceptance(raw)
        report = json.loads(args.integration_report.read_bytes())
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            result = verify_acceptance(raw=raw, report=report, database=args.market, store=store)
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        print(f"EDINET metric acceptance failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
