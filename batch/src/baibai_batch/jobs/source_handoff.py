"""Private original-receipt handoffs; publishers replay onto freshly hydrated stores."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_batch.observability.discord import write_json_atomic
from baibai_batch.storage.economic_staging import load_handoff, save_handoff
from baibai_batch.storage.lake_publish import Boto3R2Store
from baibai_engine.batch_api import (
    JQUANTS_FINANCIAL_SOURCES,
    JQUANTS_MARKET_SOURCES,
    MARKET_DB_PATH,
    TradingViewHandoff,
    acquire_edinet_archives,
    edinet_archive_keys,
    export_edinet_handoff,
    export_jpx_handoff,
    export_jquants_handoff,
    export_master_calendar_dependencies,
    export_tradingview_handoff,
    import_edinet_handoff,
    import_jpx_handoff,
    import_jquants_handoff,
    import_tradingview_handoff,
)
from baibai_engine.read_api import market_calendar_business_day

SOURCES = (
    "tradingview",
    "jquants-market",
    "jquants-financial",
    "jpx-earnings",
    "jpx-regulations",
    "edinet-list",
    "edinet-metrics",
    "edinet-facts",
)
_JPX_COLLECTION = {"jpx-earnings": "earnings_calendar", "jpx-regulations": "regulations"}


def _command(
    source: str, asof: date, *, database: Path, offline: bool = False
) -> list[str]:
    if source in {"edinet-metrics", "edinet-facts"}:
        verb = ("extract-" if offline else "refresh-") + source
        return [
            "baibai-engine",
            "screening",
            verb,
            "--asof",
            asof.isoformat(),
            "--sqlite-path",
            str(database),
        ]
    if offline:
        raise ValueError("this source has no extraction command")
    return [
        "baibai-engine",
        "screening",
        "bootstrap-cache",
        "--source-group",
        source,
        "--asof",
        asof.isoformat(),
        "--sqlite-path",
        str(database),
    ]


def _export(
    source: str,
    asof: date,
    since: datetime,
    database: Path,
    directory: Path,
    archives: set[tuple[str, int]] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "receipts.sqlite"
    report: dict[str, object] = {
        "source": source,
        "asof": asof.isoformat(),
        "started_at": since.isoformat(),
    }
    if source == "tradingview":
        report.update(export_tradingview_handoff(database, path, day=asof, since=since).to_json())
    elif source.startswith("jquants-"):
        selected = (
            JQUANTS_MARKET_SOURCES if source == "jquants-market" else JQUANTS_FINANCIAL_SOURCES
        )
        report["request_ids"] = export_jquants_handoff(
            database, path, since=since, sources=selected
        )
    elif source in _JPX_COLLECTION:
        report["bundle_ids"] = export_jpx_handoff(
            database, path, since=since, collection=_JPX_COLLECTION[source]
        )
    else:
        report.update(
            export_edinet_handoff(database, path, since=since, archives=archives or set())
        )
    if source in {"jquants-market", "tradingview"}:
        basis = date.fromisoformat(str(report["master_asof"])) if source == "tradingview" else asof
        for dependency in export_master_calendar_dependencies(
            database, day=asof, master_asof=basis
        ):
            write_json_atomic(directory / f"dependency-{dependency['request_id']}.json", dependency)
    write_json_atomic(directory / "result.json", report)


def _replay(
    source: str, database: Path, directory: Path, *, extract: bool = True
) -> dict[str, object]:
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if report["source"] != source:
        raise ValueError("handoff source differs from the publication plan")
    path = directory / "receipts.sqlite"
    if source == "tradingview":
        return import_tradingview_handoff(path, database, TradingViewHandoff.from_json(report))
    if source.startswith("jquants-"):
        selected = (
            JQUANTS_MARKET_SOURCES if source == "jquants-market" else JQUANTS_FINANCIAL_SOURCES
        )
        return import_jquants_handoff(path, database, report["request_ids"], sources=selected)
    if source in _JPX_COLLECTION:
        return import_jpx_handoff(
            path, database, tuple(report["bundle_ids"]), collection=_JPX_COLLECTION[source]
        )
    result = import_edinet_handoff(path, database)
    if extract and source in {"edinet-metrics", "edinet-facts"}:
        # Re-plan from the latest canonical lists/archives, never copy derived facts
        # computed against the acquisition's older database. These CLIs are offline.
        completed = subprocess.run(  # nosec B603
            _command(source, date.fromisoformat(report["asof"]), database=database, offline=True),
            check=False,
            text=True,
            capture_output=True,
        )
        print(completed.stdout, end="")
        print(completed.stderr, end="", file=sys.stderr)
        result["extraction_exit_code"] = completed.returncode
        result["extraction_status"] = (
            "skipped_not_initialized"
            if "skipped=not_initialized" in completed.stdout
            else "completed"
            if completed.returncode == 0
            else "failed"
        )
    return result


def complete(result: dict[str, object]) -> bool:
    return not any(
        result.get(key)
        for key in (
            "rejected_receipts",
            "incomplete_requests",
            "projection_failures",
            "incomplete_bundles",
            "failed_days",
            "archive_conflicts",
            "extraction_exit_code",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("input", "resume", "acquire", "export", "stage", "download", "replay", "check"),
    )
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--stage-id")
    parser.add_argument("--input-directory", type=Path)
    args = parser.parse_args(argv)
    directory = args.directory
    try:
        if args.command == "input":
            from baibai_batch.storage.source_inputs import load_input

            load_input(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
                args.source,
                args.database,
                directory,
            )
        elif args.command == "resume":
            from baibai_batch.storage.source_receipts import available_handoffs, download_fixed

            if args.asof is None:
                raise ValueError("--asof is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            saved_input = json.loads((directory / "result.json").read_text())
            sources = (
                ("edinet-list", args.source)
                if args.source in {"edinet-metrics", "edinet-facts"}
                else (args.source,)
            )
            for source in sources:
                for index, entry in enumerate(
                    available_handoffs(store, source=source, include_integrated=True)
                ):
                    if entry.get("invalid") or entry.get("late_before_integrated"):
                        continue
                    integrated = entry.get("integration")
                    if isinstance(integrated, dict) and (
                        integrated["status"] == "needs_explicit_recovery"
                        or integrated["integrated_at"] <= saved_input["created_at"]
                    ):
                        continue
                    if args.source == "tradingview" and entry["asof"] != args.asof.isoformat():
                        continue
                    if (
                        args.source.startswith("edinet-")
                        and str(entry["asof"]) > args.asof.isoformat()
                    ):
                        continue
                    packet = directory / f"resume-{source}-{index}"
                    download_fixed(store, entry, packet)
                    _replay(source, args.database, packet, extract=False)
        elif args.command in {"stage", "download"}:
            if args.stage_id is None:
                raise ValueError("--stage-id is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            if args.command == "stage":
                save_handoff(store, args.stage_id, directory, market=True)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print(f"handoff={args.stage_id}", file=stream)
            else:
                load_handoff(store, args.stage_id, directory, market=True)
        elif args.command in {"acquire", "export"}:
            if args.asof is None or (args.command == "export" and args.since is None):
                raise ValueError("--asof and export --since are required")
            since = args.since or datetime.now(UTC)
            before = (
                edinet_archive_keys(args.database) if args.source.startswith("edinet-") else set()
            )
            status = 0
            acquisition_report: dict[str, object] = {}
            if args.command == "acquire":
                if args.source == "tradingview":
                    raise ValueError("TradingView uses the guarded OAuth acquisition route")
                if (
                    args.source.startswith("jquants-")
                    and os.environ.get("GITHUB_EVENT_NAME") == "schedule"
                    and market_calendar_business_day(args.database, args.asof) is False
                ):
                    acquisition_report = {"status": "skipped_non_business_day"}
                elif args.source in {"edinet-metrics", "edinet-facts"}:
                    if args.input_directory is None:
                        raise ValueError(
                            "EDINET archive acquisition requires its scoped input manifest"
                        )
                    saved = json.loads((args.input_directory / "result.json").read_text())
                    result = acquire_edinet_archives(
                        args.database,
                        group=args.source,
                        asof=args.asof,
                        api_key=os.environ.get("EDINET_API_KEY", ""),
                        retained={(str(key[0]), int(key[1])) for key in saved["archive_keys"]}
                        | before,
                    )
                    print(json.dumps(result, sort_keys=True))
                    acquisition_report = result
                    status = int(result["status"] == "failed")
                else:
                    status = subprocess.run(
                        _command(args.source, args.asof, database=args.database), check=False
                    ).returncode  # nosec B603
            archives = (
                edinet_archive_keys(args.database) - before
                if args.source.startswith("edinet-")
                else set()
            )
            _export(args.source, args.asof, since, args.database, directory, archives)
            report = json.loads((directory / "result.json").read_text())
            report["acquisition"] = acquisition_report or {
                "status": "failed" if status else "received"
            }
            if args.input_directory is not None:
                report["input_generation"] = json.loads(
                    (args.input_directory / "result.json").read_text()
                )["canonical_generations"]
            write_json_atomic(directory / "result.json", report)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as stream:
                    print("stage_ready=true", file=stream)
            return status
        elif args.command == "replay":
            result = _replay(args.source, args.database, directory)
            write_json_atomic(directory / "replayed.json", result)
            print(json.dumps(result, sort_keys=True))
        else:
            result = json.loads((directory / "replayed.json").read_text(encoding="utf-8"))
            return 0 if complete(result) else 1
    except Exception as error:
        # Raw provider payload and credentials must not enter cross-job output.
        print(f"source handoff failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
