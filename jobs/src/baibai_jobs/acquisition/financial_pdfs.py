"""取得済みJPX決算PDFを有限handoffへ保存する。HTTP・本番書込は行わない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from baibai_engine.data_collection.providers.jpx.models import JPXResponse, validate_jpx_url
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def receive(manifest: Path, directory: Path) -> dict[str, object]:
    """Manifest contains actual capture metadata, never file mtime or inferred clocks.

    captured_at is the recorded completion of body capture, which may include
    file/hash processing. It is not the first HTTP byte or publication time.
    """
    records = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not 1 <= len(records) <= 8:
        raise ValueError("financial PDF import requires one to eight receipt records")
    prepared = []
    for record in records:
        url = record["requested_url"]
        validate_jpx_url(url)
        # Collection-specific identity stays native to the explicit JPX URL.
        parsed = urlparse(url)
        match = re.fullmatch(r"/disc/[0-9A-Z]{4}0/1401(\d{14})\.pdf", parsed.path)
        if parsed.netloc != "www2.jpx.co.jp" or match is None:
            raise ValueError("financial PDF import requires a JPX disclosure URL")
        started = datetime.fromisoformat(record["request_started_at"])
        captured = datetime.fromisoformat(record["captured_at"])
        if started.utcoffset() is None or captured.utcoffset() is None or captured < started:
            raise ValueError("financial PDF capture clocks are invalid")
        if record["http_status"] != 200 or record["content_type"] != "application/pdf":
            raise ValueError("financial PDF import requires an actual PDF 200 receipt")
        file = manifest.parent / record["file"]
        if file.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("financial PDF exceeds the import size limit")
        body = file.read_bytes()
        if not body.startswith(b"%PDF-") or hashlib.sha256(body).hexdigest() != record["sha256"]:
            raise ValueError("financial PDF bytes differ from the recorded receipt")
        prepared.append(
            (
                "pdf:" + match[1],
                started.astimezone(UTC),
                JPXResponse(url, captured.astimezone(UTC), record["content_type"], body),
            )
        )
    if len({row[0] for row in prepared}) != len(prepared):
        raise ValueError("financial PDF bundle has duplicate disclosure numbers")
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "receipts.sqlite"
    if database.exists() or (directory / "result.json").exists():
        raise FileExistsError("use a new private handoff directory")
    started = min(row[1] for row in prepared)
    asof = max(row[2].received_at.date() for row in prepared)
    with closing(open_connection(database)) as connection:
        bundle = create_bundle(
            connection,
            "financial_statements",
            asof,
            started,
            tuple((name, response.requested_url) for name, _, response in prepared),
        )
        for name, _, response in prepared:
            retain_bundle_response(connection, bundle, name, response)
        complete_bundle(connection, bundle)
    report: dict[str, object] = {
        "source": "jpx-financial-pdfs",
        "asof": asof.isoformat(),
        "started_at": started.isoformat(),
        "bundle_ids": [bundle],
        "receipt_clock": "recorded_body_capture_completion_not_first_byte",
    }
    write_json_atomic(directory / "result.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(receive(args.receipts, args.directory), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
