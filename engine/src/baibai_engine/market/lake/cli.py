"""Read-only CLI for market lake contracts and local object inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import JsonValue, ValidationError

from .inventory import inventory
from .models import DatasetManifest, load_manifest_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine lake")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser(
        "inventory",
        help="list object and byte totals in a local mirror without reading object contents",
    )
    inventory_parser.add_argument(
        "--root",
        type=Path,
        default=Path("stores/lake"),
        help="local bucket mirror root; object keys below it must start with lake/",
    )
    inventory_parser.add_argument("--format", choices=("yaml", "json"), default="yaml")

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate one JSON manifest contract without dereferencing or changing objects",
    )
    validate_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser.add_argument("--format", choices=("yaml", "json"), default="yaml")
    return parser


def _emit(payload: dict[str, JsonValue], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(yaml.safe_dump(payload, sort_keys=False))


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
