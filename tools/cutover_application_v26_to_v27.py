"""schema26の読取専用コピーからschema27を作る一回用tool。反映完了後に撤去する。

本番の取得・writer停止・ETag条件付き反映はjobs/OPERATIONS.mdに従う。
このtoolは別のローカルDBを作るだけで、正本を置換・公開しない。
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.sqlite.application.schema import (
    APPLICATION_SCHEMA_VERSION,
    JUDGMENT_RETROSPECTIVE_SCHEMA_SQL,
)


def cutover(source_path: Path, destination_path: Path) -> Path:
    """WALを含むconsistent backupにtableだけを追加し、旧行は再serializeしない。"""

    if APPLICATION_SCHEMA_VERSION != 27:
        raise RuntimeError("this one-time tool requires application schema 27 code")
    source_path = source_path.resolve(strict=True)
    destination_path = destination_path.resolve()
    if source_path == destination_path:
        raise ValueError("source and destination must be different paths")
    if destination_path.exists():
        raise FileExistsError(f"destination already exists: {destination_path}")

    # A same-directory staging file is exposed only after validation. hardlink_to
    # refuses an existing destination even if another process created it meanwhile.
    with tempfile.TemporaryDirectory(
        dir=destination_path.parent, prefix=f".{destination_path.name}.cutover-"
    ) as temporary:
        staged_path = Path(temporary) / "application.sqlite"
        staged_path.touch(mode=0o600)
        with (
            closing(sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(staged_path)) as destination,
        ):
            source.execute("PRAGMA query_only = ON")
            source.execute("BEGIN")
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if version != 26:
                raise ValueError(f"source must have user_version=26; found {version}")
            source.backup(destination)
            source.rollback()
            destination.execute("PRAGMA foreign_keys = ON")
            try:
                destination.executescript("BEGIN IMMEDIATE;\n" + JUDGMENT_RETROSPECTIVE_SCHEMA_SQL)
                destination.execute("PRAGMA user_version = 27")
                if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError("cutover integrity_check failed")
                if destination.execute("PRAGMA foreign_key_check").fetchall():
                    raise sqlite3.IntegrityError("cutover foreign_key_check failed")
                destination.commit()
            except BaseException:
                destination.rollback()
                raise
        destination_path.hardlink_to(staged_path)
    return destination_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="schema26のローカルDB")
    parser.add_argument("--destination", type=Path, required=True, help="未作成のschema27出力先")
    args = parser.parse_args(argv)
    try:
        destination = cutover(args.source, args.destination)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"移行失敗: {error}\n")
    print(f"schema27の作業コピーを作成しました（正本未反映）: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
