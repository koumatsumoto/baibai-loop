"""Explicit acceptance of already received TradingView observations; never fetches."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from baibai_engine.market.sqlite import SQLiteSchemaError, validate_current_schema

from .collector import JST
from .contract import COLUMNS, TABLE
from .diagnostics import SourceDataError
from .observations import COLUMNS as REQUEST_COLUMNS
from .observations import normalize_batch

NAMES = tuple(item[0] for item in COLUMNS)
MAX_INPUT_BYTES = 65_535


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    library_file_id: str = Field(pattern=r"^libfile_[a-z0-9]+$")
    file_id: str = Field(pattern=r"^file_[a-z0-9]+$")
    version: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReceiptTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def validate_times(self) -> ReceiptTiming:
        if self.start.utcoffset() != timedelta(0) or self.end.utcoffset() != timedelta(0):
            raise ValueError("receipt timestamps must be UTC")
        if self.end < self.start:
            raise ValueError("receipt end precedes start")
        return self


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    trial: int = Field(gt=0)
    timing: ReceiptTiming
    result: dict[str, Any]


class ObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    source_ref: SourceReference
    observation_date_jst: date
    columns: list[str]
    observations: list[Receipt] = Field(min_length=1, max_length=2)


@dataclass(frozen=True)
class AcceptedObservations:
    document: ObservationInput
    sha256: str
    values: tuple[tuple[object, ...], ...]

    @property
    def keys(self) -> set[tuple[object, object]]:
        return {(row[0], row[1]) for row in self.values}


def load_observations(path: Path, *, sha256: str) -> AcceptedObservations:
    """Hash-pin the selected response bytes and validate them before opening a store.

    source_ref identifies the separately retained Library original; its declared digest
    is provenance, not a claim that this subset reconstructs the complete source file.
    """
    try:
        raw = path.read_bytes()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or len(raw) > MAX_INPUT_BYTES:
            raise ValueError("invalid input boundary")
        if hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError("input digest mismatch")
        document = ObservationInput.model_validate_json(raw)
        if document.columns != REQUEST_COLUMNS:
            raise ValueError("columns differ from the request contract")
        if len({item.trial for item in document.observations}) != len(document.observations):
            raise ValueError("duplicate receipt")
        values: list[tuple[object, ...]] = []
        for receipt in document.observations:
            if receipt.timing.end.astimezone(JST).date() != document.observation_date_jst:
                raise ValueError("snapshot date differs from actual receipt date")
            data, missing = receipt.result.get("data"), receipt.result.get("missing")
            if not isinstance(data, dict) or not isinstance(missing, list):
                raise ValueError("invalid response accounting")
            symbols = list(data) + [item["symbol"] for item in missing]
            if not 1 <= len(symbols) <= 50 or any(
                not isinstance(symbol, str) or not re.fullmatch(r"TSE:[0-9][0-9A-Z]{3}", symbol)
                for symbol in symbols
            ):
                raise ValueError("invalid symbols")
            rows = normalize_batch(receipt.result, symbols, receipt.timing.end)
            values.extend(
                tuple(
                    document.observation_date_jst.isoformat()
                    if name == "snapshot_date"
                    else row.get(name)
                    for name in NAMES
                )
                for row in rows
            )
        accepted = AcceptedObservations(document, sha256, tuple(values))
        if len(accepted.keys) != len(values):
            raise ValueError("multiple observations for one daily key")
        return accepted
    except (OSError, ValueError, TypeError, KeyError, ValidationError):
        raise SourceDataError("Saved TradingView input is invalid") from None


def _digest_rows(
    conn: sqlite3.Connection, *, exclude: set[tuple[object, object]] | None = None
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    # Names are exclusively the fixed physical contract, never supplied SQL.
    rows = conn.execute(f"SELECT {','.join(NAMES)} FROM {TABLE} ORDER BY snapshot_date,ticker")  # nosec B608
    for row in rows:
        if exclude and (row[0], row[1]) in exclude:
            continue
        digest.update(
            json.dumps(row, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        )
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _existing(conn: sqlite3.Connection, row: tuple[object, ...]) -> tuple[object, ...] | None:
    result = conn.execute(
        f"SELECT {','.join(NAMES)} FROM {TABLE} WHERE snapshot_date=? AND ticker=?",  # nosec B608
        row[:2],
    ).fetchone()
    return tuple(result) if result is not None else None


def accept_observations(path: Path, accepted: AcceptedObservations) -> dict[str, object]:
    """Accept explicit receipts atomically, without changing collector's live-fetch guards."""
    if not path.is_file():
        raise SourceDataError("Market SQLite is unavailable")
    conn = sqlite3.connect(path)
    try:
        validate_current_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        retained_count, retained_sha256 = _digest_rows(conn, exclude=accepted.keys)
        added = []
        for row in accepted.values:
            stored = _existing(conn, row)
            if stored is None:
                added.append(row)
            elif stored != row:
                raise SourceDataError("Saved TradingView observation conflicts with existing row")
        conn.executemany(
            f"INSERT INTO {TABLE}({','.join(NAMES)}) VALUES ({','.join('?' for _ in NAMES)})",  # nosec B608
            added,
        )
        total_count, total_sha256 = _digest_rows(conn)
        conn.commit()
        return {
            "status": "accepted" if added else "already_recorded",
            "snapshot_date": accepted.document.observation_date_jst.isoformat(),
            "rows_added": len(added),
            "rows_verified": len(accepted.values),
            "existing_equal_rows": len(accepted.values) - len(added),
            "coverage_status": "partial",
            "input_sha256": accepted.sha256,
            "source_ref": accepted.document.source_ref.model_dump(),
            "receipts": [
                {
                    "trial": item.trial,
                    "start": item.timing.start.isoformat(),
                    "end": item.timing.end.isoformat(),
                }
                for item in accepted.document.observations
            ],
            "retained_rows": retained_count,
            "retained_rows_sha256": retained_sha256,
            "total_rows": total_count,
            "total_rows_sha256": total_sha256,
            "provider_requests": 0,
        }
    except (sqlite3.Error, SQLiteSchemaError):
        raise SourceDataError("Market SQLite acceptance failed") from None
    finally:
        conn.close()  # An uncommitted transaction is rolled back on every failure.


def verify_observations(
    path: Path, accepted: AcceptedObservations, report: dict[str, object]
) -> None:
    """Check a fresh fixed-release hydrate against accepted rows and retained history."""
    try:
        if (
            report.get("input_sha256") != accepted.sha256
            or report.get("source_ref") != accepted.document.source_ref.model_dump()
            or report.get("coverage_status") != "partial"
        ):
            raise ValueError("acceptance report identity mismatch")
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            validate_current_schema(conn)
            if any(_existing(conn, row) != row for row in accepted.values):
                raise ValueError("accepted observation differs")
            if _digest_rows(conn, exclude=accepted.keys) != (
                report.get("retained_rows"),
                report.get("retained_rows_sha256"),
            ):
                raise ValueError("retained history differs")
            if _digest_rows(conn) != (report.get("total_rows"), report.get("total_rows_sha256")):
                raise ValueError("total observations differ")
        finally:
            conn.close()
    except (ValueError, sqlite3.Error, SQLiteSchemaError):
        raise SourceDataError("Saved TradingView readback differs") from None
