"""One-time, local-only schema 51→52 copy. Retire after the explicit cutover is accepted.

The source stays byte-identical. New cash-detail columns start NULL, not inferred
from old metrics. This tool neither re-extracts facts nor publishes any store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    validate_current_schema,
)

DETAILS = (
    "financing_lease_repayments_cash_effect",
    "financing_noncontrolling_dividends_cash_effect",
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fingerprints(connection: sqlite3.Connection, *, old_columns: bool) -> dict[str, object]:
    results: dict[str, object] = {}
    for name, ddl in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({_quoted(name)})")]
        if old_columns and name == "edinet_metrics":
            columns = [column for column in columns if column not in DETAILS]
        selected = ",".join(map(_quoted, columns))
        order = selected if "WITHOUT ROWID" in (ddl or "").upper() else "rowid"
        checksum = hashlib.sha256()
        count = 0
        # Names come from sqlite_master/PRAGMA and every identifier is quoted, never raw SQL.
        query = f"SELECT {selected} FROM {_quoted(name)} ORDER BY {order}"  # nosec B608
        for row in connection.execute(query):
            count += 1
            for value in row:
                data = (
                    value
                    if isinstance(value, bytes)
                    else json.dumps(
                        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode()
                )
                checksum.update(
                    (b"B" if isinstance(value, bytes) else b"J")
                    + len(data).to_bytes(8, "big")
                    + data
                )
        results[name] = {"columns": columns, "count": count, "sha256": checksum.hexdigest()}
    return results


def _integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity is not exactly ok")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("SQLite foreign keys are not intact")


def migrate(source: Path, output: Path, *, expected_sha256: str) -> dict[str, object]:
    if SQLITE_SCHEMA_VERSION != 52:
        raise ValueError("this one-time tool requires the schema 52 implementation")
    source = source.resolve(strict=True)
    if output.exists() or output.is_symlink() or source == output.resolve():
        raise ValueError("migration requires a new output file")
    incomplete = output.with_name(output.name + ".incomplete")
    receipt_path = output.with_name(output.name + ".receipt.json")
    if any(path.exists() or path.is_symlink() for path in (incomplete, receipt_path)):
        raise ValueError("previous migration attempt must remain separate")
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("a closed fixed source is required")
    if digest(source) != expected_sha256:
        raise ValueError("source SHA differs from the explicit plan")
    source_stat = source.stat()
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
        if old.execute("PRAGMA user_version").fetchone()[0] != 51:
            raise ValueError("source must be schema 51")
        old_names = {row[1] for row in old.execute("PRAGMA table_info(edinet_metrics)")}
        if any(name in old_names for name in DETAILS):
            raise ValueError("source already contains the new details")
        _integrity(old)
        before = _fingerprints(old, old_columns=False)
        schema_before = old.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name!='edinet_metrics' "
            "ORDER BY type,name"
        ).fetchall()
        # Exclusive creation prevents a concurrent attempt from becoming an overwrite.
        with incomplete.open("xb"):
            pass
        with closing(sqlite3.connect(incomplete)) as new:
            old.backup(new)
            with new:
                for name in DETAILS:
                    new.execute(f"ALTER TABLE edinet_metrics ADD COLUMN {_quoted(name)} REAL")
                new.execute("PRAGMA user_version=52")
            validate_current_schema(new)
            _integrity(new)
            after = _fingerprints(new, old_columns=True)
            if before != after:
                raise ValueError("pre-existing table values changed")
            schema_after = new.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name!='edinet_metrics' "
                "ORDER BY type,name"
            ).fetchall()
            if schema_before != schema_after:
                raise ValueError("unrelated schema objects changed")
            if new.execute(
                "SELECT count(*) FROM edinet_metrics WHERE "
                "financing_lease_repayments_cash_effect IS NOT NULL OR "
                "financing_noncontrolling_dividends_cash_effect IS NOT NULL"
            ).fetchone()[0]:
                raise ValueError("historical details must remain unknown")
    if digest(source) != expected_sha256 or source.stat().st_mtime_ns != source_stat.st_mtime_ns:
        raise ValueError("source changed during migration")
    result = {
        "status": "verified_local_schema_copy",
        "source_sha256": expected_sha256,
        "output_sha256": digest(incomplete),
        "source_schema": 51,
        "output_schema": 52,
        "all_preexisting_table_fingerprints": before,
        "new_detail_columns": list(DETAILS),
        "historical_details": "NULL; no re-extraction or backfill",
        "no_remote_write": True,
    }
    os.link(incomplete, output)
    incomplete.unlink()
    with receipt_path.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = migrate(args.source, args.output, expected_sha256=args.expected_sha256)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "source_sha256",
                    "output_sha256",
                    "source_schema",
                    "output_schema",
                )
            }
        )
    )


def verify_copy(source: Path, target: Path, *, source_sha256: str, target_sha256: str) -> None:
    """Recheck the exact prepared copy before the separate one-time publisher."""
    paths = (source, target)
    before = tuple((digest(p), p.stat().st_mtime_ns) for p in paths)
    if (before[0][0], before[1][0]) != (source_sha256, target_sha256):
        raise ValueError("prepared schema copy digest differs")
    if any(Path(str(p) + suffix).exists() for p in paths for suffix in ("-wal", "-journal")):
        raise ValueError("closed migration copies are required")
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old,
        closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)) as new,
    ):
        if old.execute("PRAGMA user_version").fetchone()[0] != 51:
            raise ValueError("prepared source must be schema 51")
        validate_current_schema(new)
        _integrity(old)
        _integrity(new)
        if _fingerprints(old, old_columns=False) != _fingerprints(new, old_columns=True):
            raise ValueError("prepared copy changed pre-existing data")
        sql = (
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name!='edinet_metrics' "
            "ORDER BY type,name"
        )
        if old.execute(sql).fetchall() != new.execute(sql).fetchall():
            raise ValueError("prepared copy changed unrelated schema")
        if new.execute(
            "SELECT count(*) FROM edinet_metrics WHERE "
            "financing_lease_repayments_cash_effect IS NOT NULL OR "
            "financing_noncontrolling_dividends_cash_effect IS NOT NULL"
        ).fetchone()[0]:
            raise ValueError("prepared historical details are not NULL")
    if before != tuple((digest(p), p.stat().st_mtime_ns) for p in paths):
        raise ValueError("prepared copy moved during verification")


if __name__ == "__main__":
    main()
