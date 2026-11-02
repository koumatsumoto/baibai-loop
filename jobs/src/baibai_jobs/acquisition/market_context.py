"""明示取得した有限MCP応答の保存・exact再読。常設pollや売買判断は実行しない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast
from urllib.parse import quote

from baibai_engine.data_collection.providers.tradingview.market_models import (
    MARKET_GROUP,
    MARKET_SOURCE_ID,
    MarketToolCapture,
)
from baibai_engine.data_collection.providers.tradingview.market_receipts import (
    read_market_capture,
    retain_market_capture,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    initialize_database,
    validate_current_schema,
)
from baibai_engine.machine_analysis.observations.prices.tradingview_market import (
    read_market_observation,
)
from baibai_engine.primitives.time import JST
from baibai_engine.storage.filesystem import write_text_new
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff, stage_prefix
from baibai_jobs.acquisition.external_assessments.staging import read_private


def prepare(capture_paths: list[Path], directory: Path) -> dict[str, object]:
    if not 1 <= len(capture_paths) <= 3:
        raise ValueError("market capture pack requires one to three explicit responses")
    captures = [MarketToolCapture.model_validate_json(path.read_bytes()) for path in capture_paths]
    if len({capture.receipt_id for capture in captures}) != len(captures):
        raise ValueError("market pack repeats a receipt ID")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    database = directory / "receipts.sqlite"
    with closing(initialize_database(database)) as conn:
        for capture in captures:
            retain_market_capture(conn, capture)
    database.chmod(0o600)
    references = [
        {"receipt_id": capture.receipt_id, "sha256": capture.digest()} for capture in captures
    ]
    report: dict[str, object] = {
        "group": MARKET_GROUP,
        "started_at": min(capture.requested_at_utc for capture in captures).isoformat(),
        "asof": max(capture.received_at_utc for capture in captures)
        .astimezone(JST)
        .date()
        .isoformat(),
        "sources": [
            {
                "source_id": MARKET_SOURCE_ID,
                "status": "failed" if any(c.result.is_error for c in captures) else "ok",
            }
        ],
        "market_receipts": references,
    }
    path = directory / "result.json"
    path.write_text(json.dumps(report, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return report


def read_exact(database: Path, receipt_id: str, digest: str) -> dict[str, object]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        capture = read_market_capture(conn, receipt_id, expected_sha256=digest)
    observation = read_market_observation(capture)
    origin_path = database.parent / "market-origin.json"
    origin = json.loads(origin_path.read_text()) if origin_path.is_file() else None
    if origin is not None:
        with database.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != origin["receipt_sha256"]:
                raise ValueError("market local copy differs from retained handoff")
    return {
        "observation": observation,
        "retention": origin or {"status": "local_only"},
        "assessment_input": {
            "input_id": "tv-market-" + capture.receipt_id,
            "reference": (
                f"source-handoff://{origin['stage_id']}/receipt/"
                f"{quote(capture.receipt_id, safe='')}"
                f"?manifest_sha256={origin['manifest_sha256']}&capture_sha256={digest}"
            ),
            "layer": "L1",
            "observation_as_of": None,
            "available_at": capture.received_at_utc.isoformat(),
            "status": "failed" if observation["status"] == "failed" else "ok",
        }
        if origin is not None
        else None,
    }


def validate_packet(directory: Path, *, canonical: Path | None = None) -> dict[str, object]:
    """Only the explicit finite documents belong in a manual market handoff."""
    report = json.loads((directory / "result.json").read_text())
    references = report["market_receipts"]
    if (
        report["group"] != MARKET_GROUP
        or not isinstance(references, list)
        or not 1 <= len(references) <= 3
        or [item["source_id"] for item in report["sources"]] != [MARKET_SOURCE_ID]
    ):
        raise ValueError("market capture handoff scope changed")
    expected = {ref["receipt_id"] for ref in references}
    if len(expected) != len(references):
        raise ValueError("market handoff repeats receipt identity")
    with closing(
        sqlite3.connect((directory / "receipts.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        validate_current_schema(conn)
        for table in ("economic_source_records", "economic_source_runs"):
            if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:  # nosec B608
                raise ValueError("market packet contains unrelated source rows")
        actual = set(conn.execute("SELECT source_id,request_key FROM economic_source_documents"))
        if actual != {(MARKET_SOURCE_ID, identity) for identity in expected}:
            raise ValueError("market handoff contains unrelated documents")
        for ref in references:
            read_market_capture(conn, ref["receipt_id"], expected_sha256=ref["sha256"])
    if canonical is not None:
        with closing(sqlite3.connect(canonical.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            for ref in references:
                if conn.execute(
                    "SELECT 1 FROM economic_source_documents WHERE source_id=? AND request_key=?",
                    (MARKET_SOURCE_ID, ref["receipt_id"]),
                ).fetchone():
                    # 別packで同じIDを別時刻へ再利用しても、旧引用を曖昧にしない。
                    read_market_capture(conn, ref["receipt_id"], expected_sha256=ref["sha256"])
    return cast("dict[str, object]", report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "stage", "download", "read"))
    parser.add_argument("--capture", type=Path, action="append", default=[])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--stage-id")
    parser.add_argument("--receipt-id")
    parser.add_argument("--sha256")
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        report = prepare(args.capture, args.directory)
    elif args.command == "read":
        if not args.receipt_id or not args.sha256:
            parser.error("read requires --receipt-id and --sha256")
        report = read_exact(args.directory / "receipts.sqlite", args.receipt_id, args.sha256)
    else:
        if not args.stage_id or not args.stage_id.startswith(MARKET_GROUP + "-"):
            parser.error("stage/download requires --stage-id with tradingview_market- prefix")
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        key = stage_prefix(args.stage_id) + "/handoff.json"
        if args.command == "stage":
            validate_packet(args.directory)
            save_handoff(store, args.stage_id, args.directory)
        else:
            if not args.manifest_sha256:
                parser.error("download requires --manifest-sha256")
            saved = read_private(store, key)
            if saved is None or hashlib.sha256(saved.payload).hexdigest() != args.manifest_sha256:
                raise ValueError("fixed market handoff manifest differs")
            if args.directory.exists():
                raise FileExistsError(args.directory)
            load_handoff(store, args.stage_id, args.directory)
            validate_packet(args.directory)
        saved = read_private(store, key)
        assert saved is not None
        if (
            args.command == "download"
            and hashlib.sha256(saved.payload).hexdigest() != args.manifest_sha256
        ):
            raise ValueError("fixed market handoff changed during download")
        manifest = json.loads(saved.payload)
        origin = {
            "status": "private_handoff",
            "stage_id": args.stage_id,
            "manifest_sha256": hashlib.sha256(saved.payload).hexdigest(),
            "receipt_sha256": manifest["receipt_sha256"],
        }
        origin_path = args.directory / "market-origin.json"
        if origin_path.exists():
            if json.loads(origin_path.read_text()) != origin:
                raise ValueError("local market origin already names another handoff")
        else:
            write_text_new(origin_path, json.dumps(origin, sort_keys=True))
        report = {
            "stage_id": args.stage_id,
            "group": MARKET_GROUP,
            "manifest_sha256": hashlib.sha256(saved.payload).hexdigest(),
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
