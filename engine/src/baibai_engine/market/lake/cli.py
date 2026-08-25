"""Read-only CLI for market lake contracts and local object inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import JsonValue, ValidationError

from .duck import LakeCredentialError
from .inventory import inventory
from .models import DatasetManifest, L1ReleaseSourceRef, load_lake_model_json, load_manifest_json
from .objects import LakeObjectError, open_lake
from .reader import (
    LakeReadError,
    resolve_current_release,
    resolve_release,
    resolve_release_ref,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine lake")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser(
        "inventory",
        help="list object/byte totals and Raw sidecar retention classes in a local mirror",
    )
    inventory_parser.add_argument(
        "--root",
        type=Path,
        default=Path("stores"),
        help="local bucket mirror root; the lake namespace is the lake/ subtree below it",
    )
    inventory_parser.add_argument("--format", choices=("yaml", "json"), default="yaml")

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate one JSON manifest contract without dereferencing or changing objects",
    )
    validate_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser.add_argument("--format", choices=("yaml", "json"), default="yaml")

    resolve_parser = subparsers.add_parser(
        "resolve",
        help="resolve one fixed release and print the immutable identity a run would use",
    )
    resolve_parser.add_argument("--mirror", type=Path, required=True)
    resolve_parser.add_argument(
        "--bucket", help="read manifests from this R2 bucket instead of the local mirror"
    )
    target = resolve_parser.add_mutually_exclusive_group()
    target.add_argument(
        "--release", help="resolve this release instead of reading the current pointer"
    )
    target.add_argument(
        "--release-ref", type=Path, help="resolve a typed digest-pinned release reference"
    )
    resolve_parser.add_argument(
        "--manifest-sha256", help="required digest when --release names a release"
    )
    resolve_parser.add_argument("--format", choices=("yaml", "json"), default="yaml")
    return parser


def _emit(payload: dict[str, JsonValue], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(yaml.safe_dump(payload, sort_keys=False))


def _resolved_release(args: argparse.Namespace) -> dict[str, JsonValue]:
    """Freeze one release and describe it, without dereferencing any data object."""

    with open_lake(mirror=args.mirror, bucket=args.bucket) as (_session, cache):
        if args.release is not None:
            if args.manifest_sha256 is None:
                raise LakeReadError("--release requires --manifest-sha256")
            release = resolve_release(
                cache.source,
                args.release,
                manifest_sha256=args.manifest_sha256,
            )
        elif args.manifest_sha256 is not None:
            raise LakeReadError("--manifest-sha256 is valid only with --release")
        elif args.release_ref is not None:
            reference = load_lake_model_json(
                args.release_ref.read_bytes(),
                L1ReleaseSourceRef,
            )
            release = resolve_release_ref(cache.source, reference)
        else:
            release = resolve_current_release(cache.source)
    return {
        "schema_version": 1,
        "kind": "lake_release",
        "status": "ok",
        "release_id": release.release_id,
        "manifest_key": release.manifest_key,
        "manifest_sha256": release.manifest_sha256,
        "data_as_of": release.data_as_of.isoformat(),
        "datasets": [
            {
                "dataset": name,
                "build_id": manifest.build_id,
                "contract_version": manifest.contract_version,
                "manifest_sha256": release.dataset_manifest_sha256[name],
                "partitions": len(manifest.partitions),
                "objects": manifest.totals.objects,
                "bytes": manifest.totals.bytes,
                "rows": manifest.totals.rows,
            }
            for name, manifest in sorted(release.dataset_manifests.items())
        ],
    }


def _validation_error(error: ValidationError) -> str:
    failures = []
    for failure in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in failure["loc"]) or "<root>"
        failures.append(f"{location}: {failure['type']}")
    return "; ".join(failures)


def _read_only_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            _emit(inventory(args.root), args.format)
            return 0
        if args.command == "resolve":
            _emit(_resolved_release(args), args.format)
            return 0

        path: Path = args.manifest
        manifest = load_manifest_json(path.read_text(encoding="utf-8"))
        if isinstance(manifest, DatasetManifest):
            identity: dict[str, JsonValue] = {
                "dataset": manifest.dataset,
                "layer": manifest.layer,
                "contract_version": manifest.contract_version,
                "build_id": manifest.build_id,
            }
            kind = "dataset_manifest"
        else:
            identity = {
                "release_id": manifest.release_id,
                "datasets": len(manifest.datasets),
            }
            kind = "release_manifest"
        _emit(
            {
                "schema_version": 1,
                "kind": kind,
                "status": "ok",
                "scope": "manifest_contract",
                "path": str(path),
                "identity": identity,
            },
            args.format,
        )
        return 0
    except ValidationError as error:
        print(f"error: invalid manifest: {_validation_error(error)}", file=sys.stderr)
        return 1
    except (LakeCredentialError, LakeObjectError, LakeReadError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    import sys

    from .write_cli import WRITE_COMMANDS, print_combined_help
    from .write_cli import main as write_main

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print_combined_help()
        return 0
    if arguments[0] in WRITE_COMMANDS:
        return write_main(arguments)
    return _read_only_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
