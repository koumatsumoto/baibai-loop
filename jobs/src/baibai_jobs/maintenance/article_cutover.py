"""One-time schema48 article working-copy migration and guarded machine cutover."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.paths import MARKET_DB_PATH, RUNS_DB_PATH
from baibai_engine.storage.sqlite.source_cache.article_upgrade import upgrade_article_working_copy
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.publication.lease import _load_handle, verify_held
from baibai_jobs.publication.snapshot_publication import _serving_pointer

BUNDLE_KEYS = ("market.sqlite", "runs.sqlite", "indicators.sqlite")


def pulled_generations(directory: Path) -> dict[str, str]:
    result = {}
    for key in BUNDLE_KEYS:
        value = (directory / f"{key}.etag").read_text().strip().strip('"')
        if not value:
            raise ValueError("cutover requires every pulled machine ETag")
        result[key] = value
    return result


def prepare_copy(
    source: Path, output: Path, receipt: Path, *, kind: str, generation_directory: Path
) -> None:
    if receipt.exists():
        raise FileExistsError("article cutover receipt already exists")
    if kind not in {"canonical", "input", "stage"}:
        raise ValueError("unsupported article migration copy kind")
    generations = pulled_generations(generation_directory) if kind == "canonical" else {}
    origin = read_lake_store_origin(source)
    if kind == "canonical" and origin is None:
        raise ValueError("hydrate schema48 canonical before migrating its working copy")
    upgrade_article_working_copy(source, output)
    write_json_atomic(
        receipt,
        {
            "format": "article-cutover-48-49-v1",
            "kind": kind,
            "source": str(source.resolve()),
            "output": str(output.resolve()),
            "base_generations": generations,
            "base_origin": asdict(origin) if origin else None,
        },
    )


def verify_cutover(
    store: ObjectStore,
    *,
    receipt: Path,
    market: Path,
    runs: Path,
    generation_directory: Path,
    lease_handle: Path,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Keep the ordinary current/current merge strict; bind this one-time exception."""
    value = json.loads(receipt.read_text())
    if (
        value["format"] != "article-cutover-48-49-v1"
        or value["kind"] != "canonical"
        or value["output"] != str(market.resolve())
    ):
        raise ValueError("cutover requires the exact canonical migration receipt")
    verify_held(store, _load_handle(lease_handle))
    expected = pulled_generations(generation_directory)
    if expected != value["base_generations"]:
        raise ValueError("cutover pulled machine generations changed since conversion")
    pending = []
    for key, etag in expected.items():
        remote = store.head(key)
        if remote is None:
            raise ValueError("cutover machine key is missing")
        if remote.etag.strip('"') == etag:
            pending.append(key)
            continue
        candidate = value.get("upload_candidates", {}).get(key)
        if not allow_partial or candidate is None or candidate["expected_etag"] != etag:
            raise ValueError("cutover remote bundle changed; inspect partial PUT before recovery")
        # A timed-out PUT is uncertain until the actual current object is read.
        # Metadata/ETag alone cannot identify bytes uploaded by the shell transfer.
        with tempfile.TemporaryDirectory(prefix="article-cutover-readback-") as temporary:
            path = Path(temporary) / key
            store.download_file(key, path, expect_bytes=remote.size)
            after = store.head(key)
            if (
                sha256_file(path) != candidate["sha256"]
                or after is None
                or after.etag != remote.etag
            ):
                raise ValueError("partial cutover key differs from the recorded upload candidate")
    with closing(connect_read_only(market)):
        pass
    origin = read_lake_store_origin(market)
    pointer = _serving_pointer(store).pointer
    if (
        origin is None
        or pointer is None
        or origin.release_id != pointer.release_id
        or origin.release_manifest_sha256 != pointer.manifest_sha256
        or asdict(origin) == value["base_origin"]
    ):
        raise ValueError("publish the migrated fixed release before machine cutover")
    generation = ModelGenerationStore(runs).read(f"models-{origin.release_id}")
    if (
        generation is None
        or generation.metadata.inputs["market"].reference != origin.release_id
        or generation.metadata.inputs["market"].sha256 != origin.release_manifest_sha256
    ):
        raise ValueError("migrated published release has no corresponding model generation")
    return {"release_id": origin.release_id, "base_generations": expected, "pending_keys": pending}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare-copy", "verify-cutover", "resume-plan", "record-upload")
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--kind", choices=("canonical", "input", "stage"), default="canonical")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--runs", type=Path, default=RUNS_DB_PATH)
    parser.add_argument(
        "--generation-directory",
        type=Path,
        default=Path(os.environ.get("R2_GENERATION_DIR") or "stores/.r2-generations"),
    )
    parser.add_argument("--lease-handle", type=Path)
    parser.add_argument("--key", choices=BUNDLE_KEYS)
    parser.add_argument("--upload", type=Path)
    parser.add_argument("--expected-etag")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-copy":
            if args.source is None or args.output is None:
                raise ValueError("--source and --output are required")
            prepare_copy(
                args.source,
                args.output,
                args.receipt,
                kind=args.kind,
                generation_directory=args.generation_directory,
            )
        elif args.command == "record-upload":
            if args.key is None or args.upload is None or args.expected_etag is None:
                raise ValueError("upload candidate requires key, path and expected ETag")
            value = json.loads(args.receipt.read_text())
            etag = args.expected_etag.strip('"')
            if value["base_generations"][args.key] != etag:
                raise ValueError("upload candidate differs from the fresh cutover bundle")
            candidate = {"expected_etag": etag, "sha256": sha256_file(args.upload)}
            saved = value.setdefault("upload_candidates", {}).get(args.key)
            if saved is not None and saved != candidate:
                raise ValueError("cutover upload candidate changed; keep the original work copy")
            value["upload_candidates"][args.key] = candidate
            write_json_atomic(args.receipt, value)
        else:
            if args.lease_handle is None:
                raise ValueError("--lease-handle is required")
            report = verify_cutover(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
                receipt=args.receipt,
                market=args.market,
                runs=args.runs,
                generation_directory=args.generation_directory,
                lease_handle=args.lease_handle,
                allow_partial=args.command == "resume-plan",
            )
            print(
                " ".join(report["pending_keys"])
                if args.command == "resume-plan"
                else json.dumps(report, sort_keys=True)
            )
    except Exception as error:
        print(f"article cutover failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
