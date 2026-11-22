"""検証済みの静止copyから欠落したGeneration/Selectionだけを別copyへ戻す。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.sqlite.machine_analysis import (
    RUN_STORE_SCHEMA_VERSION,
)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _require_inactive(path: Path) -> None:
    if any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("history recovery requires standalone, inactive SQLite snapshots")


def _check_snapshot(path: Path, expected_sha256: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("snapshot SHA256 must be a lowercase hexadecimal digest")
    _require_inactive(path)
    if _sha256(path) != expected_sha256:
        raise ValueError("snapshot SHA256 mismatch")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != RUN_STORE_SCHEMA_VERSION:
            raise ValueError("history recovery requires the current run-store schema")
        # An INSERT trigger could alter existing rows despite an append-only caller.
        if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger'").fetchone():
            raise ValueError("history recovery does not accept stores with triggers")


def _check_models(connection: sqlite3.Connection, generation_id: str) -> None:
    source_count = connection.execute(
        "SELECT COUNT(*) FROM history.generated_model WHERE generation_id=?", (generation_id,)
    ).fetchone()[0]
    current_count = connection.execute(
        "SELECT COUNT(*) FROM main.generated_model WHERE generation_id=?", (generation_id,)
    ).fetchone()[0]
    mismatch = connection.execute(
        "SELECT 1 FROM history.generated_model h LEFT JOIN main.generated_model c "
        "ON (c.generation_id=h.generation_id AND c.model_id=h.model_id AND c.subject=h.subject) "
        "WHERE h.generation_id=? AND (c.generation_id IS NULL "
        "OR c.calculation_revision IS NOT h.calculation_revision "
        "OR c.payload_gzip IS NOT h.payload_gzip) LIMIT 1",
        (generation_id,),
    ).fetchone()
    if source_count != current_count or mismatch:
        raise ValueError(f"conflicting generated_model rows: {generation_id}")


def _append_history(
    connection: sqlite3.Connection, generation_ids: tuple[str, ...]
) -> dict[str, object]:
    missing: list[str] = []
    existing: list[str] = []
    selection_ids: list[str] = []
    for generation_id in generation_ids:
        source = connection.execute(
            "SELECT generation_id,as_of,evaluation_time,header "
            "FROM history.model_generation WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if source is None:
            raise ValueError(f"source generation is absent: {generation_id}")
        current = connection.execute(
            "SELECT generation_id,as_of,evaluation_time,header "
            "FROM main.model_generation WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if current is None:
            missing.append(generation_id)
        else:
            if tuple(source) != tuple(current):
                raise ValueError(f"conflicting model_generation: {generation_id}")
            _check_models(connection, generation_id)
            existing.append(generation_id)
        for selection in connection.execute(
            "SELECT selection_id,generation_id,published_at,payload_gzip "
            "FROM history.screening_selection WHERE generation_id=? ORDER BY selection_id",
            (generation_id,),
        ):
            saved = connection.execute(
                "SELECT selection_id,generation_id,published_at,payload_gzip "
                "FROM main.screening_selection WHERE selection_id=?",
                (selection[0],),
            ).fetchone()
            if saved is None:
                selection_ids.append(selection[0])
            elif tuple(saved) != tuple(selection):
                raise ValueError(f"conflicting screening_selection: {selection[0]}")

    # No INSERT OR REPLACE: any conflict stops the whole isolated transaction.
    inserted_models = 0
    for generation_id in missing:
        connection.execute(
            "INSERT INTO main.model_generation (generation_id,as_of,evaluation_time,header) "
            "SELECT generation_id,as_of,evaluation_time,header "
            "FROM history.model_generation WHERE generation_id=?",
            (generation_id,),
        )
        inserted_models += connection.execute(
            "INSERT INTO main.generated_model "
            "(generation_id,model_id,subject,calculation_revision,payload_gzip) "
            "SELECT generation_id,model_id,subject,calculation_revision,payload_gzip "
            "FROM history.generated_model WHERE generation_id=?",
            (generation_id,),
        ).rowcount
    for selection_id in selection_ids:
        connection.execute(
            "INSERT INTO main.screening_selection "
            "(selection_id,generation_id,published_at,payload_gzip) "
            "SELECT selection_id,generation_id,published_at,payload_gzip "
            "FROM history.screening_selection WHERE selection_id=?",
            (selection_id,),
        )
    return {
        "added_generation_ids": missing,
        "verified_existing_generation_ids": existing,
        "added_model_count": inserted_models,
        "added_selection_ids": selection_ids,
    }


def restore_model_history(
    *,
    source: Path,
    current: Path,
    output: Path,
    source_sha256: str,
    current_sha256: str,
    generation_ids: Sequence[str],
) -> dict[str, object]:
    """Write a NEW local copy, preserving current rows and exact historical compressed bytes.

    Inputs must be verified, inactive snapshots. This does not acquire a production
    lease, refresh a remote CAS, publish a pair, or reconstruct missing L1/L3 data.
    """
    identities = tuple(generation_ids)
    if not identities or any(not value.strip() for value in identities):
        raise ValueError("explicit nonempty generation IDs are required")
    if len(set(identities)) != len(identities):
        raise ValueError("generation IDs must be distinct")
    source, current = source.resolve(strict=True), current.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("history recovery output must not exist")
    if source.samefile(current):
        raise ValueError("source and current snapshots must be distinct")
    if any(Path(f"{output}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("history recovery output has SQLite sidecars")
    _check_snapshot(source, source_sha256)
    _check_snapshot(current, current_sha256)

    created = False
    try:
        with output.open("xb") as destination:
            created = True
            with current.open("rb") as original:
                shutil.copyfileobj(original, destination, length=1024 * 1024)
        if _sha256(output) != current_sha256:
            raise ValueError("current snapshot changed while copying")
        with closing(
            sqlite3.connect(output.as_uri(), uri=True, isolation_level=None)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "ATTACH DATABASE ? AS history", (source.as_uri() + "?mode=ro&immutable=1",)
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                report = _append_history(connection, identities)
                if connection.execute("PRAGMA main.foreign_key_check").fetchone():
                    raise ValueError("restored snapshot has invalid foreign keys")
                if _sha256(source) != source_sha256 or _sha256(current) != current_sha256:
                    raise ValueError("input snapshot changed during recovery")
                # New WAL commits can leave the verified main-file bytes unchanged.
                _require_inactive(source)
                _require_inactive(current)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {
            "status": "prepared_local_history_copy",
            "source_sha256": source_sha256,
            "current_sha256": current_sha256,
            "output_sha256": _sha256(output),
            **report,
        }
    except BaseException:
        if created:
            for suffix in ("-wal", "-shm", "-journal", ""):
                Path(f"{output}{suffix}").unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs restore-model-history")
    for name in ("source", "current", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--current-sha256", required=True)
    parser.add_argument("--generation-id", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = restore_model_history(
        source=args.source,
        current=args.current,
        output=args.output,
        source_sha256=args.source_sha256,
        current_sha256=args.current_sha256,
        generation_ids=args.generation_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
