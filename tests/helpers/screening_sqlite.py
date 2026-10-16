"""Test helpers for the screening market.sqlite cache.

Common fixture builders that previously appeared verbatim across multiple
test modules. Keep these strictly minimal — only the duplications that exist
in real test code.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.storage.observations.schema import open_connection
from tests.helpers.master import insert_master_rows


def insert_financial_record(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Insert a constructed provider-native document, without financial projection."""
    conn.execute(
        "INSERT OR REPLACE INTO jquants_fin_summaries "
        "(ticker, disclosed_at, disclosure_number, document_payload) VALUES (?, ?, ?, ?)",
        (
            str(record["Code"])[:4],
            record["DiscDate"],
            str(record["DiscNo"]),
            json.dumps(record, ensure_ascii=False),
        ),
    )


def make_master_records(
    asof: date,
    *,
    count: int = 2500,
    secondary_class_count: int = 0,
) -> list[dict[str, str]]:
    """Return a complete official-key master response for offline tests."""
    records = [
        {
            "Date": asof.isoformat(),
            "Code": f"{1000 + index:04d}0",
            "CoName": f"Company {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
            "ProdCat": "011",
        }
        for index in range(count)
    ]
    records.extend(
        {
            "Date": asof.isoformat(),
            "Code": f"{9000 + index:04d}1",
            "CoName": f"Secondary class {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
            "ProdCat": "011",
        }
        for index in range(secondary_class_count)
    )
    return records


def add_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    coverage_key: str,
    record_count: int = 1,
    min_date: str | None = None,
    max_date: str | None = None,
    status: str = "ok",
    error: str | None = None,
    fetched_at_utc: str | None = None,
) -> None:
    """Insert one ``source_coverage`` row, mirroring the production INSERT shape."""
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO source_coverage("
        "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, "
        "status, error"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, coverage_key, min_date, max_date, fetched_at, record_count, status, error),
    )


def insert_daily_bars_from_closes(
    sqlite_path: Path,
    ticker: str,
    closes: list[float],
    *,
    end_date: date,
    turnover_value: float | None = None,
) -> None:
    """Insert consecutive raw closes; absent turnover remains SQL NULL."""
    start = end_date - timedelta(days=len(closes) - 1)
    rows = [
        (ticker, (start + timedelta(days=index)).isoformat(), close, turnover_value)
        for index, close in enumerate(closes)
    ]
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, turnover_value) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


CALIBRATION_FIXTURE_ASOF = date(2026, 6, 30)

# Forty call sites build the same two-name store, and a panel build only reads it, so
# it is written once per session and copied after that.
_CALIBRATION_TEMPLATE: tempfile.TemporaryDirectory[str] | None = None
_CALIBRATION_TEMPLATE_PATH: Path | None = None
_CALIBRATION_TEMPLATE_SHA256: str | None = None


def build_calibration_fixture_sqlite(sqlite_path: Path) -> None:
    """Two priced, disclosed names — the smallest market store a panel build accepts."""

    global _CALIBRATION_TEMPLATE, _CALIBRATION_TEMPLATE_PATH, _CALIBRATION_TEMPLATE_SHA256
    if _CALIBRATION_TEMPLATE_PATH is None:
        _CALIBRATION_TEMPLATE = tempfile.TemporaryDirectory()
        _CALIBRATION_TEMPLATE_PATH = Path(_CALIBRATION_TEMPLATE.name) / "market.sqlite"
        _write_calibration_fixture_sqlite(_CALIBRATION_TEMPLATE_PATH)
        _CALIBRATION_TEMPLATE_SHA256 = _file_sha256(_CALIBRATION_TEMPLATE_PATH)
    # Callers write to their own copy, so nothing should reach the shared bytes. Checked
    # rather than assumed: a template mutated once would change every later caller with
    # nothing failing.
    assert _file_sha256(_CALIBRATION_TEMPLATE_PATH) == _CALIBRATION_TEMPLATE_SHA256, (
        "calibration fixture template was mutated"
    )
    assert sqlite_path != _CALIBRATION_TEMPLATE_PATH
    shutil.copyfile(_CALIBRATION_TEMPLATE_PATH, sqlite_path)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_calibration_fixture_sqlite(sqlite_path: Path) -> None:
    ASOF = CALIBRATION_FIXTURE_ASOF

    conn = open_connection(sqlite_path)
    try:
        insert_master_rows(
            conn,
            [
                ("2026-06-01", "9001", "キャッシュリッチ", "プライム", "サービス業", "011"),
                ("2026-06-01", "9002", "割高", "プライム", "サービス業", "011"),
            ],
        )
        add_source_coverage(
            conn,
            source="jquants_master_snapshots",
            coverage_key="latest",
            record_count=2,
            min_date="2026-06-01",
            max_date="2026-06-01",
        )
        for ticker, values in (
            (
                "9001",
                {
                    "EPS": 10.0,
                    "BPS": 200.0,
                    "Sales": 5e9,
                    "CFO": 1e9,
                    "CashEq": 4e9,
                    "TA": 3e10,
                    "Eq": 2e10,
                    "OP": 5e8,
                    "OdP": 5e8,
                    "NP": 1e9,
                    "DivAnn": 4.0,
                    "NxFEPS": 11.0,
                    "NxFDivAnn": 4.5,
                    "EqAR": 2e10 / 3e10,
                },
            ),
            (
                "9002",
                {
                    "EPS": 1.0,
                    "BPS": 10.0,
                    "Sales": 5e9,
                    "CFO": 1e8,
                    "CashEq": 1e8,
                    "TA": 2e10,
                    "Eq": 5e9,
                    "OP": 5e8,
                    "OdP": 5e8,
                    "NP": 1e8,
                    "NxFEPS": 1.0,
                    "EqAR": 5e9 / 2e10,
                },
            ),
        ):
            insert_financial_record(
                conn,
                {
                    "Code": f"{ticker}0",
                    "DiscDate": "2026-05-10",
                    "DiscTime": "15:00:00",
                    "DiscNo": f"statement-{ticker}",
                    "DocType": "FYFinancialStatements_Consolidated_JP",
                    "CurPerType": "FY",
                    "CurFYSt": "2025-04-01",
                    "CurFYEn": "2026-03-31",
                    "CurPerSt": "2025-04-01",
                    "CurPerEn": "2026-03-31",
                    "NxtFYSt": "2026-04-01",
                    "NxtFYEn": "2027-03-31",
                    "ShOutFY": 1e8,
                    "TrShFY": 0.0,
                    **values,
                },
            )
        add_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key="2026",
            record_count=2,
            min_date="2026-05-10",
            max_date="2026-06-30",
        )
        conn.execute(
            # 同じ連結scopeを申告し、総資産の規模比較を通る入力。
            # 数値の一致だけで会計entityの同一性が証明されるわけではない。
            "INSERT INTO edinet_metrics("
            "asof_date, ticker, debt, cash, net_cash, investment_securities, "
            "total_assets, consolidation_basis, failure_reasons, extractor_revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ASOF.isoformat(),
                "9001",
                1e9,
                4e9,
                3e9,
                2e9,
                3e10,
                "consolidated",
                "[]",
                "a" * 64,
            ),
        )
        add_source_coverage(
            conn,
            source="edinet_metrics",
            coverage_key=ASOF.isoformat(),
            record_count=1,
            min_date=ASOF.isoformat(),
            max_date=ASOF.isoformat(),
        )
        conn.commit()
    finally:
        conn.close()
    for ticker in ("9001", "9002"):
        insert_daily_bars_from_closes(
            sqlite_path,
            ticker,
            [100.0] * 200,
            end_date=ASOF,
            turnover_value=2e8,
        )


def market_store_with_fetch_claim(root: Path, *, claimed_rows: int, held_rows: int) -> Path:
    """Write a market store whose fetch ledger claims rows the store may not hold.

    ``held_rows`` short of ``claimed_rows`` is not the interesting case — coverage
    windows overlap, so the working store is short by millions. Zero is: it is what
    the published copy looks like after the lake-owned tables were emptied.
    """

    path = root / "stores/market/market.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_connection(path)) as connection:
        connection.execute(
            "INSERT INTO source_coverage "
            "(source, coverage_key, coverage_start, coverage_end, fetched_at_utc, "
            "record_count, status) VALUES "
            "('jquants_daily_bars', '2026-08-17', '2026-08-17', '2026-08-17', "
            "'2026-08-17T08:00:00+00:00', ?, 'ok')",
            (claimed_rows,),
        )
        for index in range(held_rows):
            connection.execute(
                "INSERT INTO jquants_daily_bars (ticker, traded_at, close) VALUES (?, ?, ?)",
                (f"{1000 + index}", "2026-08-17", 100.0),
            )
        connection.commit()
    return path


def seed_daily_bars(
    sqlite_path: Path,
    rows: list[tuple[str, str, float | None, float | None]],
) -> None:
    """Insert (ticker, traded_at, close, adjustment_factor) rows into the store."""
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, adjustment_factor) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
