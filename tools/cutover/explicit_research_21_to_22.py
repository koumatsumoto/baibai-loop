"""21の正本を保持し、CAAのTriage FKだけNULL許容にした22のcopyを生成する。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from hashlib import sha256
from pathlib import Path

from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL


def table_fingerprints(connection: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    result = {}
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall():
        # Names come only from SQLite's schema, not user input.
        quoted = '"' + name.replace('"', '""') + '"'
        rows = connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()  # nosec B608
        result[name] = (
            len(rows),
            sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest(),
        )
    return result


def cutover(source: Path, output: Path) -> dict[str, object]:
    if APPLICATION_SCHEMA_VERSION != 22:
        raise ValueError("this one-shot tool belongs only to application release 22")
    if source.resolve() == output.resolve() or output.exists():
        raise ValueError("output must be a new file, separate from the source")
    partial = output.with_name(output.name + ".partial")
    if partial.exists():
        raise ValueError("partial output already exists; inspect it before retrying")
    with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as original:
        original.execute("BEGIN")
        if original.execute("PRAGMA user_version").fetchone()[0] != 21:
            raise ValueError("source must be application schema 21")
        if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("source integrity_check failed")
        if original.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("source foreign_key_check failed")
        before = table_fingerprints(original)
        output.parent.mkdir(parents=True, exist_ok=True)
        # An exclusive creation prevents overwrite of another prepared copy.
        with partial.open("xb"):
            pass
        partial.chmod(0o600)
        with sqlite3.connect(partial) as target, sqlite3.connect(":memory:") as current:
            original.backup(target)
            current.executescript(SCHEMA_SQL)
            ddl = current.execute(
                "SELECT sql FROM sqlite_schema WHERE name='capital_allocation_assessment'"
            ).fetchone()[0]
            target.execute("PRAGMA foreign_keys=OFF")
            target.execute("BEGIN IMMEDIATE")
            try:
                target.execute("ALTER TABLE capital_allocation_assessment RENAME TO old_caa_21")
                target.execute(ddl)
                target.execute(
                    "INSERT INTO capital_allocation_assessment "
                    "SELECT * FROM old_caa_21 ORDER BY rowid"
                )
                target.execute("DROP TABLE old_caa_21")
                target.execute(
                    "CREATE INDEX capital_allocation_assessment_asof_idx ON "
                    "capital_allocation_assessment(as_of,published_at,capital_allocation_assessment_id)"
                )
                target.execute("PRAGMA user_version=22")
                if table_fingerprints(target) != before:
                    raise ValueError(
                        "cutover changed stored rows; source kept, output not published"
                    )
                if target.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("output foreign_key_check failed")
                target.commit()
            except BaseException:
                target.rollback()
                raise
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("output integrity_check failed")
    partial.replace(output)
    return {
        "source_version": 21,
        "target_version": 22,
        "all_table_rows_unchanged": True,
        "output": str(output),
        "row_counts": {key: value[0] for key, value in before.items()},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(cutover(args.source, args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
