"""Explicit private article saves and fixed-release reads; never print original text to logs."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleEvidenceRef,
    ArticleReceipt,
)
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.machine_analysis.observations.articles.contracts import ArticleExtractionArtifact
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.reader import FixedArticleReader
from baibai_engine.machine_analysis.observations.articles.store import save_artifact
from baibai_engine.storage.snapshots.objects import (
    LakeObjectCache,
    LocalMirrorSource,
    R2ObjectSource,
    R2ReadCredentials,
)
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine articles")
    sub = parser.add_subparsers(dest="command", required=True)
    receipt = sub.add_parser("save-receipt")
    receipt.add_argument("--receipt-file", type=Path, required=True)
    receipt.add_argument("--body-file", type=Path)
    artifact = sub.add_parser("save-artifact")
    artifact.add_argument("--artifact-file", type=Path, required=True)
    projection = sub.add_parser("project")
    projection.add_argument("--receipt-id", required=True)
    projection.add_argument("--kind", choices=("index", "document"), default="index")
    for writer in (receipt, artifact, projection):
        writer.add_argument("--sqlite-path", type=Path, required=True)
    for name in ("list", "get", "exact-evidence"):
        reader = sub.add_parser(name)
        reader.add_argument("--release", required=True, help="explicit retained lookup release")
        reader.add_argument("--manifest-sha256", required=True)
        reader.add_argument("--mirror", type=Path)
        reader.add_argument("--cache", type=Path, required=True)
        if name == "list":
            reader.add_argument("--limit", type=int, default=20)
            reader.add_argument("--after")
        else:
            reader.add_argument("--reference-file", type=Path, required=True)
            reader.add_argument(
                "--output-dir", type=Path, required=True, help="private output destination"
            )
    return parser


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"save-receipt", "save-artifact", "project"}:
        with closing(open_connection(args.sqlite_path)) as conn, conn:
            if args.command == "save-receipt":
                receipt = ArticleReceipt.model_validate_json(args.receipt_file.read_bytes())
                retain_receipt(
                    conn, receipt, body=args.body_file.read_bytes() if args.body_file else None
                )
                result = {"receipt_id": receipt.receipt_id, "sha256": receipt.sha256}
            elif args.command == "save-artifact":
                artifact = ArticleExtractionArtifact.model_validate_json(
                    args.artifact_file.read_bytes()
                )
                save_artifact(conn, artifact)
                result = {"artifact_id": artifact.artifact_id, "sha256": artifact.sha256}
            else:
                run, observations = project_receipt(conn, args.receipt_id, kind=args.kind)
                result = {
                    "run_id": run.run_id,
                    "status": run.status,
                    "observations": len(observations),
                }
    else:
        source = (
            LocalMirrorSource(args.mirror)
            if args.mirror
            else R2ObjectSource(R2ReadCredentials.from_env())
        )
        release = resolve_release(source, args.release, manifest_sha256=args.manifest_sha256)
        reader = FixedArticleReader(release, LakeObjectCache(args.cache, source))
        if args.command == "list":
            result = reader.list(after=args.after, limit=args.limit)
        else:
            reference = ArticleEvidenceRef.model_validate_json(args.reference_file.read_bytes())
            evidence = (
                reader.get(reference) if args.command == "get" else reader.exact_evidence(reference)
            )
            args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            # Refuse accidental replacement; the command is explicit private materialization.
            with (args.output_dir / "body.bin").open("xb") as output:
                output.write(evidence.body)
            payload = {
                "reference": reference.model_dump(mode="json"),
                "lookup_release_id": evidence.lookup_release_id,
                "lookup_manifest_sha256": evidence.lookup_manifest_sha256,
                "receipt": evidence.receipt.model_dump(mode="json"),
                "observation": evidence.observation.model_dump(mode="json")
                if evidence.observation
                else None,
                "artifact": evidence.artifact.model_dump(mode="json")
                if evidence.artifact
                else None,
            }
            with (args.output_dir / "evidence.json").open("x") as output:
                json.dump(payload, output, ensure_ascii=False)
            result = {
                "receipt_id": reference.receipt_id,
                "lookup_release_id": release.release_id,
                "lineage_release_id": reference.lineage_release_id,
                "output_dir": str(args.output_dir),
            }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except Exception as exc:
        # Validation tracebacks can include submitted private source/extraction text.
        print(
            f"article command failed ({type(exc).__name__}); inspect private input/store",
            file=sys.stderr,
        )
        return 1
