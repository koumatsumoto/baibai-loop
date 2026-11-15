"""Opt-in finite semantic refresh of retained article versions in a normal daily run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baibai_engine.machine_analysis.observations.articles.contracts import (
        ArticleExtractionArtifact,
    )

_FIELDS = {"as_of", "observations"}
_TARGET_FIELDS = {
    "observation_id",
    "observation_sha256",
    "article_version_id",
    "extractor_revision",
    "result_sha256",
}


def decode_recovery(raw: str) -> dict[str, Any]:
    """Validate before credential-bearing steps, using only the standard library."""
    if len(raw.encode()) > 16_384:
        raise ValueError("article recovery input is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.keys() != _FIELDS:
        raise ValueError("article recovery fields are invalid")
    asof = value["as_of"]
    if not isinstance(asof, str) or date.fromisoformat(asof).isoformat() != asof:
        raise ValueError("article recovery date must use YYYY-MM-DD")
    targets = value["observations"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= 16:
        raise ValueError("article recovery requires 1-16 explicit observations")
    identities = []
    for target in targets:
        if not isinstance(target, dict) or target.keys() != _TARGET_FIELDS:
            raise ValueError("article recovery observation fields are invalid")
        for field, pattern in (
            ("observation_id", r"article-observation-[0-9a-f]{64}"),
            ("observation_sha256", r"[0-9a-f]{64}"),
            ("article_version_id", r"article-version-[0-9a-f]{64}"),
            ("extractor_revision", r"(?:tsr-performance|boj-policy-decision)-v[0-9]+"),
            ("result_sha256", r"[0-9a-f]{64}"),
        ):
            if not isinstance(target[field], str) or re.fullmatch(pattern, target[field]) is None:
                raise ValueError("article recovery observation identity is invalid")
        if target["observation_id"] != "article-observation-" + target["observation_sha256"]:
            raise ValueError("article recovery observation ID differs from its digest")
        identities.append(target["observation_id"])
    if len(set(identities)) != len(identities):
        raise ValueError("article recovery duplicates an observation")
    if len({t["article_version_id"] for t in targets}) != len(targets):
        raise ValueError("article recovery duplicates an article version")
    return value


def result_sha256(artifact: ArticleExtractionArtifact) -> str:
    """Pin compared meaning and exact evidence, but never backdate its generation clock."""
    from baibai_engine.primitives.json import canonical_json

    value = {
        "status": artifact.status,
        "method": artifact.method,
        "extractor_revision": artifact.extractor_revision,
        "payload": artifact.payload,
    }
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def apply_recovery(
    *, raw: str, database: Path, asof: date, generated_at: datetime
) -> dict[str, Any]:
    """Append only the fixed, compared results; caller owns fresh hydration and lease."""
    from baibai_engine.data_collection.receipts.articles.store import read_body, read_receipt
    from baibai_engine.machine_analysis.observations.articles.store import read_observations
    from baibai_engine.machine_analysis.observations.trends.extraction import (
        expected_revision,
        extract_observations,
    )
    from baibai_engine.machine_analysis.observations.trends.validation import decode_artifact
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection
    from baibai_jobs.publication.article_verification import capture_delta

    request = decode_recovery(raw)
    if request["as_of"] != asof.isoformat() or generated_at.tzinfo is None:
        raise ValueError("article recovery date or generation clock is invalid")
    if not database.is_file():
        raise ValueError("article recovery requires a hydrated existing market store")
    targets = {t["observation_id"]: t for t in request["observations"]}
    with closing(open_connection(database)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        observations = read_observations(conn, tuple(targets))
        for observation in observations:
            target = targets[observation.observation_id]
            if (
                observation.sha256 != target["observation_sha256"]
                or observation.article_version_id != target["article_version_id"]
                or expected_revision(observation.source) != target["extractor_revision"]
            ):
                raise ValueError("article recovery differs from the fixed observation or revision")
            receipt = read_receipt(
                conn, observation.receipt_id, receipt_sha256=observation.receipt_sha256
            )
            if receipt.received_at > generated_at:
                raise ValueError("article recovery cannot precede the retained receipt")
            read_body(conn, observation.body_ref)
        artifacts = extract_observations(conn, tuple(targets), generated_at=generated_at)
        versions = {target["article_version_id"]: target for target in targets.values()}
        for artifact in artifacts:
            version = artifact.payload.get("article_version_id")
            target = versions.get(version) if isinstance(version, str) else None
            if (
                target is None
                or artifact.status not in {"extracted", "unsupported"}
                or result_sha256(artifact) != target["result_sha256"]
            ):
                raise ValueError("article recovery result differs from the compared meaning")
            original = read_observations(conn, (artifact.observation_id,))[0]
            receipt = read_receipt(
                conn, original.receipt_id, receipt_sha256=original.receipt_sha256
            )
            decode_artifact(artifact, original, receipt=receipt)
        artifact_ids = sorted({a.artifact_id for a in artifacts})
    return {
        "status": "applied",
        "conditions_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "as_of": asof.isoformat(),
        "executed_at": generated_at.isoformat(),
        "observation_count": len(targets),
        "artifact_ids": artifact_ids,
        "artifact_statuses": {a.artifact_id: a.status for a in artifacts},
        "article_delta": capture_delta(database, (), artifact_ids=artifact_ids),
    }


def main(argv: list[str] | None = None) -> int:
    from baibai_engine.storage.json import write_json_atomic
    from baibai_engine.storage.objects.r2 import Boto3R2Store
    from baibai_engine.storage.paths import MARKET_DB_PATH
    from baibai_jobs.integration.sources import canonical_generations
    from baibai_jobs.publication.article_verification import verify_delta
    from baibai_jobs.publication.lease import _load_handle, verify_held

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "verify"))
    parser.add_argument("--conditions", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--asof", type=date.fromisoformat, required=True)
    parser.add_argument("--lease-handle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = args.conditions.read_text()
        request = decode_recovery(raw)
        if request["as_of"] != args.asof.isoformat():
            raise ValueError("article recovery differs from the integration date")
        handle = _load_handle(args.lease_handle)
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            verify_held(store, handle)
            if args.command == "apply":
                result = apply_recovery(
                    raw=raw, database=args.market, asof=args.asof, generated_at=datetime.now(UTC)
                )
            else:
                result = json.loads(args.report.read_bytes())
                if result["conditions_sha256"] != hashlib.sha256(raw.encode()).hexdigest():
                    raise ValueError("article recovery conditions changed before readback")
                generations = canonical_generations(store)
                result["readback"] = verify_delta(
                    store,
                    delta=result["article_delta"],
                    directory=args.report.parent / "article-semantic-readback",
                    generations=generations,
                )
                result["canonical_generations"] = generations
                result["status"] = "published_and_verified"
            verify_held(store, handle)
        write_json_atomic(args.report, result)
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "observations": result["observation_count"],
                    "artifacts": len(result["artifact_ids"]),
                }
            )
        )
    except Exception as error:
        print(f"article semantic recovery failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
