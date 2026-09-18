"""Show and validate the external macro draft contract without opening stores."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from .handoff import MacroContextHandoff, load_handoff


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="handoff_command", required=True)
    validate = commands.add_parser("validate", help="check structure only; never publish")
    validate.add_argument("artifact", type=Path)
    validate.add_argument("--format", choices=("yaml", "json"), default="yaml")
    schema = commands.add_parser("schema", help="emit JSON Schema from the Pydantic model")
    schema.add_argument("--format", choices=("json",), default="json")


def run(args: argparse.Namespace) -> int:
    try:
        if args.db is not None:
            raise ValueError("handoff does not use --db; remove the option")
        if args.handoff_command == "schema":
            schema = MacroContextHandoff.model_json_schema(mode="validation")
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            print(json.dumps(schema, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        document = load_handoff(args.artifact)
        payload = {
            "status": "ok",
            "validation_scope": "structure_only",
            "publish_ready": False,
            "schema_version": document.schema_version,
            "analysis_id": document.analysis_id,
            "as_of": document.as_of.isoformat(),
            "evidence_count": len(document.evidence),
            "data_issue_count": len(document.data_issues),
            "input_bindings_complete": (
                document.bindings.reading is not None and document.bindings.market is not None
            ),
        }
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        else:
            yaml.safe_dump(payload, sys.stdout, allow_unicode=True, sort_keys=False)
        return 0
    except ValidationError as error:
        errors = [
            {
                "path": ".".join(str(part) for part in item["loc"]) or "$",
                "message": item["msg"],
            }
            for item in error.errors(include_url=False, include_context=False, include_input=False)
        ]
    except (OSError, ValueError, yaml.YAMLError) as error:
        errors = [{"path": "$", "message": str(error)}]
    print(
        json.dumps(
            {"status": "error", "validation_scope": "structure_only", "errors": errors},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 1
