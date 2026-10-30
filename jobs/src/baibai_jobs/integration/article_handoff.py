"""Article-only append/reprojection from an exact immutable stage."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.data_collection.receipts.articles.store import copy_receipt_closure, read_receipt
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.trends.extraction import extract_observations
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.replay_result import ReplayResult


def import_handoff(source: str, database: Path, directory: Path) -> ReplayResult:
    if source not in SOURCES:
        raise ValueError("unsupported article handoff source")
    report = json.loads((directory / "result.json").read_text())
    if report["source"] != source:
        raise ValueError("article handoff source mismatch")
    identities = report["receipt_ids"]
    if not isinstance(identities, list) or any(not isinstance(item, str) for item in identities):
        raise ValueError("invalid article handoff receipt identities")
    observations = []
    failed = []
    artifacts = []
    with (
        closing(connect_read_only(directory / "receipts.sqlite")) as raw,
        closing(open_connection(database)) as conn,
    ):
        closure = set(identities)
        for identity in identities:
            receipt = read_receipt(raw, identity)
            if receipt.body_ref is not None:
                closure.add(receipt.body_ref.receipt_id)
            if receipt.source != source:
                raise ValueError("article receipt belongs to another source")
        # L1 commits before projection. A parse error cannot discard already
        # received bytes, and late replay never changes receipt clocks/identities.
        with conn:
            for identity in identities:
                copy_receipt_closure(raw, conn, identity)
        for identity in identities:
            with conn:
                run, projected = project_receipt(
                    conn, identity, kind=report["projection_kinds"][identity]
                )
            observation_ids = [item.observation_id for item in projected]
            observations.extend(observation_ids)
            with conn:
                extracted = extract_observations(
                    conn, observation_ids, generated_at=datetime.now(UTC)
                )
            artifacts.extend(item.artifact_id for item in extracted)
            if run.status == "parse_failed":
                failed.append(identity)
    return ReplayResult(
        report={
            "receipt_ids": sorted(closure),
            "observation_ids": observations,
            "projection_failures": failed,
            "artifact_ids": sorted(set(artifacts)),
            "acquisition_status": report.get("status"),
        },
        complete=not failed,
    )
