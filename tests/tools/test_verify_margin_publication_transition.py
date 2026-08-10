from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from tools.diagnostics.verify_margin_publication_transition import (
    MarginTransitionVerificationError,
    main,
    render_report,
    verify_transition,
)

OBSERVED_AT = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


def _verify(sqlite_path: Path):
    return verify_transition(sqlite_path, observed_at_utc=OBSERVED_AT)


def _create_store(path: Path, *, daily_scale: float = 1.0) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA user_version = 21;
            CREATE TABLE source_coverage(
              source TEXT NOT NULL,
              coverage_key TEXT NOT NULL,
              coverage_start TEXT,
              coverage_end TEXT,
              fetched_at_utc TEXT NOT NULL,
              record_count INTEGER NOT NULL,
              status TEXT NOT NULL,
              error TEXT,
              PRIMARY KEY(source, coverage_key)
            );
            CREATE TABLE jquants_weekly_margin(
              week_end TEXT NOT NULL,
              ticker TEXT NOT NULL,
              long_vol REAL,
              short_vol REAL,
              long_std_vol REAL,
              long_neg_vol REAL,
              short_std_vol REAL,
              short_neg_vol REAL,
              issue_type TEXT,
              PRIMARY KEY(week_end, ticker)
            );
            CREATE TABLE jquants_all_issues_daily_margin(
              balance_date TEXT NOT NULL,
              ticker TEXT NOT NULL,
              long_vol REAL,
              short_vol REAL,
              long_std_vol REAL,
              long_neg_vol REAL,
              short_std_vol REAL,
              short_neg_vol REAL,
              issue_type TEXT,
              PRIMARY KEY(balance_date, ticker)
            );
            """
        )
        rows = [
            ("1301", 100.0, 20.0, 60.0, 40.0, 15.0, 5.0, "2"),
            ("7203", 200.0, 50.0, 150.0, 50.0, 30.0, 20.0, "2"),
            ("9984", 300.0, 90.0, 200.0, 100.0, 50.0, 40.0, "1"),
        ]
        conn.executemany(
            "INSERT INTO jquants_weekly_margin VALUES ('2026-09-18', ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO jquants_all_issues_daily_margin VALUES "
            "('2026-09-25', ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (ticker, *(value * daily_scale for value in values), issue_type)
                for ticker, *values, issue_type in rows
            ],
        )
        conn.executemany(
            "INSERT INTO source_coverage VALUES (?, ?, ?, ?, ?, ?, 'ok', NULL)",
            [
                (
                    "jquants_weekly_margin",
                    "get_mkt_margin_interest:2026-09-18..2026-09-18",
                    "2026-09-18",
                    "2026-09-18",
                    "2026-09-24T07:01:00+00:00",
                    3,
                ),
                (
                    "jquants_all_issues_daily_margin",
                    "get_mkt_margin_interest:2026-09-25..2026-09-25",
                    "2026-09-25",
                    "2026-09-25",
                    "2026-09-28T07:01:00+00:00",
                    3,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_transition_report_passes_predeclared_shape_and_unit_gates(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path, daily_scale=1.1)

    result = verify_transition(
        sqlite_path,
        observed_at_utc=OBSERVED_AT,
    )
    report = render_report(result)

    assert result.passed
    assert result.row_count_ratio == 1.0
    assert result.overlap_coefficient == 1.0
    assert result.issue_type_agreement == 1.0
    assert result.schema_version == 21
    assert "machine verdict: `pass`" in report
    assert str(tmp_path) not in report
    assert "market store: `market.sqlite`" in report
    assert "| `long_vol_total_ratio` | 1.100000 | 0.50..2.00 | pass |" in report
    assert "| `long_vol` | 600 | 660 | 1.100000" in report


def test_transition_verifier_accepts_a_later_additive_schema_version(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA user_version = 22")
    finally:
        conn.close()

    result = _verify(sqlite_path)

    assert result.passed
    assert result.schema_version == 22


def test_transition_report_fails_a_unit_change_without_rejecting_the_evidence(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    report_path = tmp_path / "report.md"
    _create_store(sqlite_path, daily_scale=100.0)

    result = _verify(sqlite_path)
    status = main(
        [
            "--sqlite",
            str(sqlite_path),
            "--output",
            str(report_path),
            "--observed-at",
            OBSERVED_AT.isoformat(),
        ]
    )

    assert not result.passed
    assert status == 1
    failed = {gate.name for gate in result.gates if not gate.passed}
    assert failed == {"long_vol_total_ratio", "short_vol_total_ratio"}
    assert "machine verdict: `fail`" in render_report(result)
    assert "machine verdict: `fail`" in report_path.read_text(encoding="utf-8")


def test_transition_report_rejects_an_equal_sized_different_population(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        for current, replacement in (("1301", "1111"), ("7203", "2222"), ("9984", "3333")):
            conn.execute(
                "UPDATE jquants_all_issues_daily_margin SET ticker = ? WHERE ticker = ?",
                (replacement, current),
            )
        conn.commit()
    finally:
        conn.close()

    result = _verify(sqlite_path)

    assert not result.passed
    assert result.row_count_ratio == 1.0
    assert result.overlap_coefficient == 0.0
    assert {gate.name for gate in result.gates if not gate.passed} == {
        "ticker_overlap_coefficient",
        "issue_type_agreement",
    }


def test_transition_verifier_rejects_inconsistent_component_semantics(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE jquants_all_issues_daily_margin SET long_std_vol = 999 WHERE ticker = '1301'"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MarginTransitionVerificationError, match="long components"):
        _verify(sqlite_path)


def test_transition_verifier_rejects_coverage_count_mismatch(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE source_coverage SET record_count = 2 "
            "WHERE source = 'jquants_all_issues_daily_margin'"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MarginTransitionVerificationError, match="differs from table count"):
        _verify(sqlite_path)


def test_transition_verifier_rejects_prepublication_coverage(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE source_coverage SET fetched_at_utc = '2026-09-27T06:00:00+00:00' "
            "WHERE source = 'jquants_all_issues_daily_margin'"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MarginTransitionVerificationError, match="predates its 2026-09-28T16:00"):
        _verify(sqlite_path)


def test_transition_verifier_enforces_the_first_daily_publication_time(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE source_coverage SET fetched_at_utc = '2026-09-28T06:59:59+00:00' "
            "WHERE source = 'jquants_all_issues_daily_margin'"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MarginTransitionVerificationError, match="2026-09-28T16:00"):
        _verify(sqlite_path)

    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE source_coverage SET fetched_at_utc = '2026-09-28T07:00:00+00:00' "
            "WHERE source = 'jquants_all_issues_daily_margin'"
        )
        conn.commit()
    finally:
        conn.close()
    assert _verify(sqlite_path).passed


def test_transition_verifier_rejects_observation_before_fetch(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)

    with pytest.raises(MarginTransitionVerificationError, match="observation time predates"):
        verify_transition(
            sqlite_path,
            observed_at_utc=datetime(2026, 9, 28, 6, 59, tzinfo=UTC),
        )


def test_cli_writes_report_and_returns_gate_status(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    report_path = tmp_path / "reports" / "report.md"
    _create_store(sqlite_path, daily_scale=1.05)

    status = main(
        [
            "--sqlite",
            str(sqlite_path),
            "--output",
            str(report_path),
            "--observed-at",
            "2026-09-28T08:00:00+00:00",
        ]
    )

    assert status == 0
    assert report_path.read_text(encoding="utf-8").startswith(
        "# 信用取引残高 公表移行初回検証（2026-09-25 残高）"
    )


@pytest.mark.parametrize("alias_kind", ["direct", "symlink", "hardlink"])
def test_cli_refuses_any_output_alias_of_the_market_store(tmp_path: Path, alias_kind: str) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _create_store(sqlite_path)
    before = sqlite_path.read_bytes()
    output_path = sqlite_path
    if alias_kind == "symlink":
        output_path = tmp_path / "report-symlink.md"
        output_path.symlink_to(sqlite_path)
    elif alias_kind == "hardlink":
        output_path = tmp_path / "report-hardlink.md"
        output_path.hardlink_to(sqlite_path)

    status = main(
        [
            "--sqlite",
            str(sqlite_path),
            "--output",
            str(output_path),
            "--observed-at",
            OBSERVED_AT.isoformat(),
        ]
    )

    assert status == 2
    assert sqlite_path.read_bytes() == before


def test_cli_classifies_report_write_failure_as_exit_2_and_preserves_existing_report(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    report_path = tmp_path / "report.md"
    _create_store(sqlite_path)
    report_path.write_text("existing report\n", encoding="utf-8")

    with patch.object(Path, "replace", side_effect=OSError("injected write failure")):
        status = main(
            [
                "--sqlite",
                str(sqlite_path),
                "--output",
                str(report_path),
                "--observed-at",
                OBSERVED_AT.isoformat(),
            ]
        )

    assert status == 2
    assert report_path.read_text(encoding="utf-8") == "existing report\n"
    assert not list(tmp_path.glob(".report.md.*.tmp"))


def test_cli_classifies_directory_output_as_exit_2(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    output_path = tmp_path / "report.md"
    _create_store(sqlite_path)
    output_path.mkdir()

    status = main(
        [
            "--sqlite",
            str(sqlite_path),
            "--output",
            str(output_path),
            "--observed-at",
            OBSERVED_AT.isoformat(),
        ]
    )

    assert status == 2
    assert output_path.is_dir()
