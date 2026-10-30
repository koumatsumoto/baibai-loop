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
from baibai_engine.data_collection.receipts.articles.store import read_receipt, retain_receipt
from baibai_engine.machine_analysis.observations.articles.contracts import (
    ArticleExtractionArtifact,
    ArticleObservation,
)
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.reader import FixedArticleReader
from baibai_engine.machine_analysis.observations.articles.store import (
    read_observations,
    save_artifact,
)
from baibai_engine.machine_analysis.observations.trends.validation import (
    PAYLOAD_SCHEMA,
    decode_artifact,
)
from baibai_engine.storage.snapshots.objects import (
    LakeObjectCache,
    LocalMirrorSource,
    R2ObjectSource,
    R2ReadCredentials,
)
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

_COMMAND_HELP = {
    "save-receipt": "Save an immutable receipt and its private original body.",
    "save-artifact": "Save an extraction artifact bound to retained text coordinates.",
    "project": "Project a retained index or document without fetching the Web.",
    "list": "List article references from one explicitly fixed release.",
    "get": "Read exact article evidence from its original fixed release.",
    "exact-evidence": "Resolve exact cited evidence in an explicitly selected retained release.",
}


def _semantic_validation(
    artifact: ArticleExtractionArtifact | None,
    observation: ArticleObservation | None,
    receipt: ArticleReceipt | None = None,
) -> str:
    """Validate known claims at the CLI seam; unknown payloads have raw integrity only."""
    if artifact is None or artifact.payload.get("schema") != PAYLOAD_SCHEMA:
        return "raw_integrity_only"
    if observation is None:
        raise ValueError("trend artifact requires its retained observation")
    decode_artifact(artifact, observation, receipt=receipt)
    return PAYLOAD_SCHEMA


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine articles", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    receipt = sub.add_parser(
        "save-receipt",
        help=_COMMAND_HELP["save-receipt"],
        description=_COMMAND_HELP["save-receipt"],
    )
    receipt.add_argument("--receipt-file", type=Path, required=True)
    receipt.add_argument("--body-file", type=Path)
    artifact = sub.add_parser(
        "save-artifact",
        help=_COMMAND_HELP["save-artifact"],
        description=_COMMAND_HELP["save-artifact"],
    )
    artifact.add_argument("--artifact-file", type=Path, required=True)
    projection = sub.add_parser(
        "project", help=_COMMAND_HELP["project"], description=_COMMAND_HELP["project"]
    )
    projection.add_argument("--receipt-id", required=True)
    projection.add_argument("--kind", choices=("index", "document"), default="index")
    projection.add_argument("--index-observation-id")
    for writer in (receipt, artifact, projection):
        writer.add_argument("--sqlite-path", type=Path, required=True)
    for name in ("list", "get", "exact-evidence"):
        reader = sub.add_parser(name, help=_COMMAND_HELP[name], description=_COMMAND_HELP[name])
        reader.add_argument("--release", required=True, help="explicit retained lookup release")
        reader.add_argument("--manifest-sha256", required=True)
        reader.add_argument("--mirror", type=Path)
        reader.add_argument("--cache", type=Path, required=True)
        if name == "list":
            reader.add_argument("--limit", type=int, default=20)
            reader.add_argument("--after")
            reader.add_argument("--month", help="UTC receipt month YYYY-MM; defaults to latest")
        else:
            reader.add_argument("--reference-file", type=Path, required=True)
            reader.add_argument(
                "--output-dir", type=Path, required=True, help="private output destination"
            )
    return parser


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: dict[str, object]
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
                observation = read_observations(conn, [artifact.observation_id])[0]
                receipt = read_receipt(
                    conn, observation.receipt_id, receipt_sha256=observation.receipt_sha256
                )
                semantic_validation = _semantic_validation(artifact, observation, receipt)
                save_artifact(conn, artifact)
                result = {
                    "artifact_id": artifact.artifact_id,
                    "sha256": artifact.sha256,
                    "semantic_validation": semantic_validation,
                }
            else:
                run, observations = project_receipt(
                    conn,
                    args.receipt_id,
                    kind=args.kind,
                    index_observation_id=args.index_observation_id,
                )
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
            result = reader.list(after=args.after, limit=args.limit, month=args.month)
        else:
            reference = ArticleEvidenceRef.model_validate_json(args.reference_file.read_bytes())
            evidence = (
                reader.get(reference) if args.command == "get" else reader.exact_evidence(reference)
            )
            semantic_validation = _semantic_validation(
                evidence.artifact, evidence.observation, evidence.receipt
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
                "semantic_validation": semantic_validation,
            }
            with (args.output_dir / "evidence.json").open("x") as output:
                json.dump(payload, output, ensure_ascii=False)
            result = {
                "receipt_id": reference.receipt_id,
                "lookup_release_id": release.release_id,
                "lineage_release_id": reference.lineage_release_id,
                "output_dir": str(args.output_dir),
                "semantic_validation": semantic_validation,
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
