"""Prepare an exact article-only recovery plan without fetching a live provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_jobs.acquisition.article_planning import ARTICLE_NAMESPACE, ARTICLE_SOURCES
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.external_assessments.staging import read_private


def exact_entry(
    store: ObjectStore, *, source: str, stage_id: str, manifest_sha256: str
) -> dict[str, Any]:
    if source not in ARTICLE_SOURCES or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("article recovery requires an article source and exact manifest digest")
    key = stage_prefix(stage_id, namespace=ARTICLE_NAMESPACE) + "/handoff.json"
    saved = read_private(store, key)
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != manifest_sha256:
        raise LakePublishError("exact article recovery manifest differs or is missing")
    value = json.loads(saved.payload)["result"]
    if value["source"] != source:
        raise ValueError("article recovery source differs from immutable stage")
    clock = datetime.fromisoformat(value["started_at"])
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("article recovery requires the original timezone-aware clock")
    date.fromisoformat(value["asof"])
    return {
        "source": source,
        "market": True,
        "namespace": ARTICLE_NAMESPACE,
        "stage_id": stage_id,
        "manifest_sha256": manifest_sha256,
        "started_at": value["started_at"],
        "asof": value["asof"],
        "explicit_article_recovery": True,
    }


def prepare(
    store: ObjectStore, directory: Path, *, source: str, stage_id: str, manifest_sha256: str
) -> None:
    if (directory / "plan.json").exists():
        raise FileExistsError("recovery plan is already fixed")
    entry = exact_entry(store, source=source, stage_id=stage_id, manifest_sha256=manifest_sha256)
    directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        directory / "plan.json", {"handoffs": [entry], "external": [], "external_failed": []}
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--source", choices=ARTICLE_SOURCES, required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        prepare(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
            args.directory,
            source=args.source,
            stage_id=args.stage_id,
            manifest_sha256=args.manifest_sha256,
        )
    except Exception as error:
        print(f"article recovery preparation failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
