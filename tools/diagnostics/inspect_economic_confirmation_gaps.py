"""固定したlocal copyだけで、経済range原本の確認時計欠落候補を調べる。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Never

from baibai_engine.data_collection.acquisition.policy import source_policy
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    scoped_record_sources,
    validate_collection_confirmations,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    decode_records,
    read_snapshot_rows,
)
from baibai_engine.data_collection.receipts.native_sources.store import open_read_only_connection
from baibai_engine.storage.objects.json import read_private
from baibai_engine.storage.objects.r2 import RemoteObject
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.receipts import download_fixed
from baibai_jobs.integration.economic_handoff import validate_handoff
from baibai_jobs.integration.economic_sources import _read


@dataclass(frozen=True)
class Stage:
    stage_id: str
    manifest_sha256: str
    directory: Path


class _LocalCopies:
    """既存handoff readerへlocal fileだけを渡す。列挙・通信・公開は持たない。"""

    def __init__(self, canonical: Path, stages: tuple[Stage, ...]) -> None:
        self.paths = {"indicators.sqlite": canonical}
        for stage in stages:
            prefix = stage_prefix(stage.stage_id)
            raw = stage.directory / "receipts.sqlite"
            self.paths[prefix + "/handoff.json"] = stage.directory / "handoff.json"
            self.paths[prefix + "/integrated.json"] = stage.directory / "integrated.json"
            self.paths[f"{prefix}/receipts-{sha256_file(raw)}.sqlite"] = raw
        for path in self.paths.values():
            if path.suffix == ".sqlite" and any(
                Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
            ):
                raise ValueError("closed SQLite copies without sidecars are required")
        self.digests = {
            key: sha256_file(path) if path.is_file() else None for key, path in self.paths.items()
        }

    def head(self, key: str) -> RemoteObject | None:
        if key not in self.paths:
            raise ValueError("object is outside the supplied local copies")
        path = self.paths[key]
        digest = sha256_file(path) if path.is_file() else None
        if digest != self.digests[key]:
            raise ValueError("local input changed during inspection")
        if digest is None:
            return None
        return RemoteObject(digest, path.stat().st_size, {"sha256": digest}, "")

    def get_bytes(self, key: str) -> bytes:
        if self.head(key) is None:
            raise FileNotFoundError("local handoff object is missing")
        return self.paths[key].read_bytes()

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        remote = self.head(key)
        if remote is None or (expect_bytes is not None and remote.size != expect_bytes):
            raise ValueError("local handoff size differs")
        shutil.copyfile(self.paths[key], path)
        self.head(key)

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> Never:
        raise PermissionError("inspection cannot publish")

    def verify_unchanged(self) -> None:
        for key in self.paths:
            self.head(key)


def _clock(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("receipt clocks require a timezone")
    return result.astimezone(UTC)


def _evidence(row: tuple[Any, ...], *, checked_at: str | None = None) -> dict[str, Any]:
    if len(decode_records(row[4])) != row[3]:
        raise ValueError("native record count differs from its payload")
    return {
        "observed_at": str(row[1]),
        "fetched_at": str(row[2]),
        "checked_at": checked_at,
        "record_count": int(row[3]),
        "content_sha256": hashlib.sha256(row[4]).hexdigest(),
    }


def _events(
    conn: sqlite3.Connection, source_id: str, start: date, end: date
) -> list[dict[str, Any]]:
    validate_collection_confirmations(conn)
    if conn.execute(
        "SELECT 1 FROM economic_source_collection_confirmations WHERE source_id=?", (source_id,)
    ).fetchone():
        raise ValueError("complete-source confirmations are outside this range diagnostic")
    scope = (source_id, start.isoformat(), end.isoformat())
    events = [
        _evidence(tuple(row))
        for row in conn.execute(
            "SELECT * FROM economic_source_records "
            "WHERE source_id=? AND observed_at BETWEEN ? AND ?",
            scope,
        )
    ]
    for row in conn.execute(
        "SELECT r.*, c.checked_at FROM economic_source_record_confirmations c "
        "JOIN economic_source_records r ON r.source_id=c.source_id "
        "AND r.observed_at=c.observed_at AND r.fetched_at=c.adopted_fetched_at "
        "WHERE c.source_id=? AND c.observed_at BETWEEN ? AND ?",
        scope,
    ):
        events.append(_evidence(tuple(row[:5]), checked_at=str(row[5])))
    return events


def _time(event: dict[str, Any]) -> datetime:
    return _clock(event["checked_at"] or event["fetched_at"])


def _content(event: dict[str, Any]) -> tuple[int, str]:
    return event["record_count"], event["content_sha256"]


def _finding(
    day: date,
    adopted: dict[str, Any] | None,
    canonical: list[dict[str, Any]],
    raw: list[dict[str, Any]],
) -> dict[str, Any]:
    conflicts: set[str] = set()
    known: dict[datetime, tuple[int, str]] = {}
    for event in (*canonical, *raw):
        stamp, content = _time(event), _content(event)
        if stamp in known and known[stamp] != content:
            conflicts.add(stamp.isoformat())
        known[stamp] = content
    latest = max(raw, key=_time) if raw else None
    if conflicts:
        status = "conflicting_receipt"
    elif latest is None:
        status = "no_raw_evidence"
    elif adopted is None:
        status = "missing_canonical_candidate"
    elif _time(adopted) > _time(latest):
        status = "canonical_newer"
    elif _time(adopted) == _time(latest):
        status = "current_matches"
    else:
        status = (
            "confirmation_gap_candidate"
            if _content(adopted) == _content(latest)
            else "adoption_gap_candidate"
        )
    return {
        "observed_at": day.isoformat(),
        "status": status,
        "canonical": adopted,
        "latest_receipt_in_window": latest,
        "conflicting_clocks": sorted(conflicts),
    }


def inspect(
    canonical: Path,
    *,
    canonical_sha256: str,
    stages: tuple[Stage, ...],
    source_id: str,
    start: date,
    end: date,
    received_since: datetime,
    received_until: datetime,
) -> dict[str, Any]:
    """候補だけを返す。全stage収集済み・過去影響・回復可能とは認定しない。"""
    since, until = _clock(received_since.isoformat()), _clock(received_until.isoformat())
    if start > end or since > until or not stages:
        raise ValueError("explicit finite days, receipt clocks and stages are required")
    if len({stage.stage_id for stage in stages}) != len(stages):
        raise ValueError("duplicate stage identity")
    if source_id not in scoped_record_sources():
        raise ValueError("only registered range-record sources are supported")
    group = source_policy(load_sources()[source_id]).acquisition_group
    reader = _LocalCopies(canonical, stages)
    if reader.digests["indicators.sqlite"] != canonical_sha256:
        raise ValueError("canonical differs from the fixed digest")
    raw_events: list[dict[str, Any]] = []
    handoffs = []
    with tempfile.TemporaryDirectory(prefix="economic-confirmation-inspection-") as temporary:
        directory = Path(temporary)
        canonical_copy = directory / "canonical.sqlite"
        _read(reader, canonical_copy)
        with closing(open_read_only_connection(canonical_copy)) as conn:
            canonical_events = _events(conn, source_id, start, end)
            checks = {
                event["observed_at"]: event["checked_at"]
                for event in canonical_events
                if event["checked_at"] is not None
            }
            adopted = {
                row[1]: _evidence(tuple(row), checked_at=checks.get(row[1]))
                for row in read_snapshot_rows(conn, start, end, source_id=source_id)
            }
        for index, stage in enumerate(stages):
            packet = directory / str(index)
            download_fixed(
                reader,
                {
                    "stage_id": stage.stage_id,
                    "market": False,
                    "manifest_sha256": stage.manifest_sha256,
                },
                packet,
            )
            validate_handoff(packet, group)
            result = json.loads((packet / "result.json").read_text())
            marker = read_private(reader, stage_prefix(stage.stage_id) + "/integrated.json")
            status = "not_supplied"
            if marker is not None:
                saved = json.loads(marker.payload)
                if (
                    saved.get("manifest_sha256") != stage.manifest_sha256
                    or saved.get("source") != "economic-" + group
                    or saved.get("started_at") != result["started_at"]
                    or saved.get("status") not in {"integrated", "needs_explicit_recovery"}
                ):
                    raise ValueError("marker differs from the fixed handoff")
                status = saved["status"]
            raw = packet / "receipts.sqlite"
            with closing(open_read_only_connection(raw)) as conn:
                for event in _events(conn, source_id, start, end):
                    if since <= _time(event) <= until:
                        raw_events.append(
                            {**event, "stage_id": stage.stage_id, "marker_status": status}
                        )
            acquisition_status = next(
                (row["status"] for row in result["sources"] if row["source_id"] == source_id),
                "not_requested",
            )
            if acquisition_status not in {"ok", "failed", "not_requested"}:
                raise ValueError("unknown acquisition status")
            handoffs.append(
                {
                    "stage_id": stage.stage_id,
                    "manifest_sha256": stage.manifest_sha256,
                    "receipt_sha256": sha256_file(raw),
                    "marker_sha256": hashlib.sha256(marker.payload).hexdigest() if marker else None,
                    "marker_status": status,
                    "acquisition_status": acquisition_status,
                }
            )
        findings = [
            _finding(
                day,
                adopted.get(day.isoformat()),
                [item for item in canonical_events if item["observed_at"] == day.isoformat()],
                [item for item in raw_events if item["observed_at"] == day.isoformat()],
            )
            for offset in range((end - start).days + 1)
            for day in (start + timedelta(days=offset),)
        ]
        reader.verify_unchanged()
    return {
        "kind": "local_economic_confirmation_inspection",
        "source_id": source_id,
        "observed_start": start.isoformat(),
        "observed_end": end.isoformat(),
        "received_since": since.isoformat(),
        "received_until": until.isoformat(),
        "canonical_sha256": canonical_sha256,
        "handoffs": handoffs,
        "counts": dict(Counter(item["status"] for item in findings)),
        "findings": findings,
        "coverage": "not_established",
        "production_impact": "unverified",
        "recovery": "not_performed",
        "next_step": "review_stage_coverage_and_acceptance_before_recovery_or_refetch",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", required=True, type=Path)
    parser.add_argument("--canonical-sha256", required=True)
    parser.add_argument(
        "--stage", required=True, action="append", nargs=3, metavar=("ID", "SHA256", "DIRECTORY")
    )
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--received-since", required=True, type=datetime.fromisoformat)
    parser.add_argument("--received-until", required=True, type=datetime.fromisoformat)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = inspect(
            args.canonical,
            canonical_sha256=args.canonical_sha256,
            stages=tuple(
                Stage(identity, digest, Path(path)) for identity, digest, path in args.stage
            ),
            source_id=args.source_id,
            start=args.start,
            end=args.end,
            received_since=args.received_since,
            received_until=args.received_until,
        )
    except Exception as error:
        # Exception text can contain private provider values or local paths.
        print(json.dumps({"status": "failed", "reason": type(error).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
