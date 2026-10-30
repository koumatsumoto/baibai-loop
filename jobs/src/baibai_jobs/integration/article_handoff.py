"""Article-only append/reprojection from an exact immutable stage."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.data_collection.receipts.articles.store import copy_receipt_closure, read_receipt
from baibai_engine.machine_analysis.observations.articles.contracts import ArticleObservation
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
    kinds = report["projection_kinds"]
    evidence = report.get("document_evidence", {})
    if not isinstance(evidence, dict):
        raise ValueError("invalid article document evidence")
    observations = []
    failed = []
    artifacts: list[str] = []
    extraction_failures: list[str] = []
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
        index_observations: dict[tuple[str, str, str], ArticleObservation] = {}
        ordered = [item for item in identities if kinds[item] == "index"] + [
            item for item in identities if kinds[item] != "index"
        ]
        for identity in ordered:
            kind = kinds[identity]
            link = evidence.get(identity) if kind == "document" else None
            if source == "nhk_news" and kind == "document" and link is None:
                failed.append(identity)
                continue  # The GUID cannot be inferred from the fetched URL.
            selected = None
            if link is not None:
                if not isinstance(link, dict) or any(
                    not isinstance(link.get(field), str)
                    for field in ("index_receipt_id", "article_id", "native_locator", "title")
                ):
                    raise ValueError("invalid article document evidence reference")
                selected = index_observations.get(
                    (link["index_receipt_id"], link["article_id"], link["native_locator"])
                )
                if selected is None:
                    failed.append(identity)
                    continue  # Retain raw; do not infer an index observation from the URL.
                if selected.title != link["title"]:
                    failed.append(identity)
                    continue
            with conn:
                run, projected = project_receipt(
                    conn,
                    identity,
                    kind=kind,
                    index_observation_id=selected.observation_id if selected is not None else None,
                )
            observation_ids = [item.observation_id for item in projected]
            observations.extend(observation_ids)
            if kind == "index":
                index_observations.update(
                    {(identity, item.article_id, item.native_locator): item for item in projected}
                )
            try:
                with conn:
                    extracted = extract_observations(
                        conn, observation_ids, generated_at=datetime.now(UTC)
                    )
                artifacts.extend(item.artifact_id for item in extracted)
                extraction_failures.extend(
                    item.artifact_id for item in extracted if item.status == "failed"
                )
            except ValueError:
                # Retained L1 and native projection are still read back. This is
                # an explicit failure, never a missing/empty successful model.
                extraction_failures.append(identity)
            if run.status == "parse_failed":
                failed.append(identity)
    return ReplayResult(
        report={
            "receipt_ids": sorted(closure),
            "observation_ids": observations,
            "projection_failures": failed,
            "artifact_ids": sorted(set(artifacts)),
            "extraction_failures": extraction_failures,
            "acquisition_status": report.get("status"),
        },
        complete=not (failed or extraction_failures),
    )
