from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest
import yaml
from tools.limit_outcome import build_parser, main

from baibai_engine.market.sqlite import open_connection
from baibai_engine.position.ledger import load_portfolio_ledger
from baibai_engine.position.store import LedgerStoreService

FIXTURE = Path(__file__).parent / "fixtures" / "portfolio-ledger" / "representative.yaml"


def _app_db(tmp_path: Path, ledger_path: Path = FIXTURE) -> Path:
    path = tmp_path / "app.sqlite"
    document = load_portfolio_ledger(ledger_path)
    document = document.model_copy(
        update={
            "market_prices": tuple(
                price.model_copy(update={"source_kind": "licensed_dataset"})
                for price in document.market_prices
            )
        }
    )
    LedgerStoreService(path).import_document(document)
    return path


def test_limit_outcome_keeps_the_initial_contract_fixed() -> None:
    help_text = build_parser().format_help()

    assert "--root" not in help_text
    assert "--horizon-sessions" not in help_text


def _market_sqlite(
    path: Path,
    *,
    lows: dict[date, float | None] | None = None,
    adjustment_factors: dict[date, float | None] | None = None,
) -> None:
    sessions = [
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
        date(2026, 6, 5),
        date(2026, 6, 8),
        date(2026, 6, 9),
        date(2026, 6, 10),
        date(2026, 6, 11),
        date(2026, 6, 12),
    ]
    resolved_lows = lows or {}
    resolved_factors = adjustment_factors or {}
    conn = open_connection(path)
    try:
        conn.executemany(
            "INSERT INTO jquants_market_calendar(day, is_business_day) VALUES (?, 1)",
            [(session.isoformat(),) for session in sessions],
        )
        conn.execute(
            "INSERT INTO source_coverage(source, coverage_key, coverage_start, coverage_end, "
            "fetched_at_utc, record_count, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "jquants_market_calendar",
                "test-window",
                sessions[0].isoformat(),
                sessions[-1].isoformat(),
                "2026-06-13T00:00:00+00:00",
                len(sessions),
                "ok",
            ),
        )
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, low, close, adjustment_factor) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "2331",
                    session.isoformat(),
                    resolved_lows.get(session, 1060.0),
                    1100.0 + index * 10,
                    resolved_factors.get(session, 1.0),
                )
                for index, session in enumerate(sessions)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    asof: date = date(2026, 6, 12),
    sqlite_path: Path | None = None,
    ledger_path: Path = FIXTURE,
    reservation_id: str = "reservation-2331-first",
) -> tuple[int, dict[str, object]]:
    market = sqlite_path or tmp_path / "market.sqlite"
    app_db = _app_db(tmp_path, ledger_path)
    exit_code = main(
        [
            "--db",
            str(app_db),
            "--reservation-id",
            reservation_id,
            "--sqlite-path",
            str(market),
            "--asof",
            asof.isoformat(),
        ]
    )
    output = capsys.readouterr()
    return exit_code, yaml.safe_load(output.out) if output.out else {}


def test_limit_outcome_reports_touch_without_inferring_fill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path, lows={date(2026, 6, 4): 1040.0})

    exit_code, payload = _run(tmp_path, capsys, sqlite_path=sqlite_path)

    assert exit_code == 0
    assert set(payload) == {
        "status",
        "reservation_id",
        "ticker",
        "remaining_quantity",
        "limit_yen",
        "asof",
        "touch",
        "post_expiry",
        "sources",
    }
    assert set(payload["sources"]) == {
        "ledger_entity",
        "ledger_append_head",
        "market_data_ref",
        "selected_rows_sha256",
        "market_hash_basis",
    }
    assert payload["status"] == "observed"
    assert payload["remaining_quantity"] == 100
    assert payload["touch"] == {
        "touched_limit": True,
        "fill_inferred": False,
        "first_touch_date": "2026-06-04",
        "first_touch_raw_low_yen": 1040.0,
        "touch_session_count": 1,
    }
    assert "fill" not in payload
    assert payload["post_expiry"]["reference_date"] == "2026-06-05"
    assert payload["post_expiry"]["horizon_sessions"] == 5
    assert payload["post_expiry"]["horizon_date"] == "2026-06-12"
    assert payload["post_expiry"]["observed_price_change_pct"] == pytest.approx(4.424779)


def test_limit_outcome_excludes_submission_and_includes_full_expiry_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(
        sqlite_path,
        lows={date(2026, 6, 2): 900.0, date(2026, 6, 5): 1000.0},
    )

    exit_code, payload = _run(tmp_path, capsys, sqlite_path=sqlite_path)

    assert exit_code == 0
    assert payload["touch"]["first_touch_date"] == "2026-06-05"
    assert payload["touch"]["touch_session_count"] == 1


def test_limit_outcome_stays_pending_without_human_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _run(
        tmp_path,
        capsys,
        asof=date(2026, 7, 20),
        reservation_id="reservation-8929-pending",
    )

    assert exit_code == 0
    assert payload["status"] == "pending"
    assert payload["reason"] == "human_confirmed_release_required"
    assert payload["sources"]["selected_rows_sha256"] is None


def test_limit_outcome_marks_non_expiry_terminal_reason_not_eligible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    raw["events"].append(
        {
            "event_id": "human-cancel-8929",
            "type": "release",
            "occurred_at": "2026-07-10T10:00:00+09:00",
            "reservation_id": "reservation-8929-pending",
            "reason": "cancelled",
            "decision_reference": "https://github.com/koumatsumoto/baibai-loop/issues/360",
        }
    )
    ledger_path = tmp_path / "cancelled-ledger.yaml"
    ledger_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    exit_code, payload = _run(
        tmp_path,
        capsys,
        asof=date(2026, 7, 20),
        ledger_path=ledger_path,
        reservation_id="reservation-8929-pending",
    )

    assert exit_code == 0
    assert payload["status"] == "not_eligible"
    assert payload["reason"] == "release_reason_cancelled"
    assert payload["sources"]["selected_rows_sha256"] is None


def test_limit_outcome_rejects_asof_before_cancelled_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    raw["events"].append(
        {
            "event_id": "human-cancel-8929",
            "type": "release",
            "occurred_at": "2026-07-10T10:00:00+09:00",
            "reservation_id": "reservation-8929-pending",
            "reason": "cancelled",
            "decision_reference": "https://github.com/koumatsumoto/baibai-loop/issues/360",
        }
    )
    ledger_path = tmp_path / "cancelled-ledger.yaml"
    ledger_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    app_db = _app_db(tmp_path, ledger_path)

    assert (
        main(
            [
                "--db",
                str(app_db),
                "--reservation-id",
                "reservation-8929-pending",
                "--sqlite-path",
                str(tmp_path / "missing.sqlite"),
                "--asof",
                "2026-07-09",
            ]
        )
        == 2
    )
    assert "asof must not predate human-confirmed release" in capsys.readouterr().err


def test_limit_outcome_waits_for_complete_post_expiry_horizon(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)

    exit_code, payload = _run(
        tmp_path,
        capsys,
        sqlite_path=sqlite_path,
        asof=date(2026, 6, 10),
    )

    assert exit_code == 0
    assert payload["status"] == "pending"
    assert payload["reason"] == "insufficient_post_expiry_sessions"
    assert payload["details"]["post_expiry_sessions_observed"] == 3


def test_limit_outcome_does_not_skip_a_missing_post_expiry_bar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)
    conn = open_connection(sqlite_path)
    try:
        conn.execute(
            "DELETE FROM jquants_daily_bars WHERE ticker = ? AND traded_at = ?",
            ("2331", "2026-06-09"),
        )
        conn.commit()
    finally:
        conn.close()

    exit_code, payload = _run(tmp_path, capsys, sqlite_path=sqlite_path)

    assert exit_code == 0
    assert payload["status"] == "unresolved"
    assert payload["reason"] == "missing_raw_bar_for_observation_window"
    assert payload["details"]["dates"] == ["2026-06-09"]


def test_limit_outcome_rejects_asof_before_confirmed_expiry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app_db = _app_db(tmp_path)
    assert (
        main(
            [
                "--db",
                str(app_db),
                "--reservation-id",
                "reservation-2331-first",
                "--sqlite-path",
                str(tmp_path / "missing.sqlite"),
                "--asof",
                "2026-06-04",
            ]
        )
        == 2
    )
    assert "asof must not predate human-confirmed release" in capsys.readouterr().err


def test_limit_outcome_rejects_asof_before_submission_for_pending_reservation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app_db = _app_db(tmp_path)
    assert (
        main(
            [
                "--db",
                str(app_db),
                "--reservation-id",
                "reservation-8929-pending",
                "--sqlite-path",
                str(tmp_path / "missing.sqlite"),
                "--asof",
                "2026-07-02",
            ]
        )
        == 2
    )
    assert "asof must not predate reservation submission" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("lows", "factors", "reason"),
    [
        ({date(2026, 6, 4): None}, {}, "missing_raw_low_for_active_session"),
        ({date(2026, 6, 4): 0.0}, {}, "invalid_raw_low_for_active_session"),
        ({}, {date(2026, 6, 4): 0.5}, "corporate_action_in_observation_window"),
    ],
)
def test_limit_outcome_does_not_bridge_missing_or_changed_price_basis(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    lows: dict[date, float | None],
    factors: dict[date, float | None],
    reason: str,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path, lows=lows, adjustment_factors=factors)

    exit_code, payload = _run(tmp_path, capsys, sqlite_path=sqlite_path)

    assert exit_code == 0
    assert payload["status"] == "unresolved"
    assert payload["reason"] == reason


@pytest.mark.parametrize(
    ("factor", "reason"),
    [
        (0.5, "corporate_action_in_observation_window"),
        (None, "corporate_action_basis_unassessed"),
    ],
)
def test_limit_outcome_checks_submission_day_corporate_action_basis(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    factor: float | None,
    reason: str,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path, adjustment_factors={date(2026, 6, 2): factor})

    exit_code, payload = _run(tmp_path, capsys, sqlite_path=sqlite_path)

    assert exit_code == 0
    assert payload["status"] == "unresolved"
    assert payload["reason"] == reason


def test_limit_outcome_rejects_raw_bar_outside_business_calendar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)
    conn = open_connection(sqlite_path)
    try:
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, low, close, adjustment_factor) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2331", "2026-06-06", 1040.0, 1100.0, 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    exit_code, payload = _run(tmp_path, capsys, sqlite_path=sqlite_path)

    assert exit_code == 0
    assert payload["status"] == "unresolved"
    assert payload["reason"] == "raw_bar_outside_business_calendar"
    assert payload["details"]["dates"] == ["2026-06-06"]


@pytest.mark.parametrize("corruption", ["expiry_before_expiration", "overfill"])
def test_limit_outcome_rejects_schema_valid_but_invalid_ledger_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    corruption: str,
) -> None:
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    if corruption == "expiry_before_expiration":
        release = next(
            event for event in raw["events"] if event["event_id"] == "release-2331-expired"
        )
        release["occurred_at"] = "2026-06-05T15:29:00+09:00"
    else:
        fill = next(event for event in raw["events"] if event["event_id"] == "fill-2331-partial")
        fill["quantity"] = 300
    ledger_path = tmp_path / f"{corruption}.yaml"
    ledger_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    app_db = _app_db(tmp_path, ledger_path)

    assert (
        main(
            [
                "--db",
                str(app_db),
                "--reservation-id",
                "reservation-2331-first",
                "--sqlite-path",
                str(tmp_path / "missing.sqlite"),
                "--asof",
                "2026-06-12",
            ]
        )
        == 2
    )
    assert capsys.readouterr().err.startswith("error:")


def test_limit_outcome_is_read_only_and_source_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)
    before = sqlite_path.read_bytes()
    exit_code, payload = _run(tmp_path, capsys, sqlite_path=sqlite_path)
    directory_before = set(tmp_path.iterdir())

    assert exit_code == 0
    assert sqlite_path.read_bytes() == before
    assert set(tmp_path.iterdir()) == directory_before
    assert payload["sources"]["ledger_entity"] == "portfolio-ledger"
    assert payload["sources"]["ledger_append_head"] == 11
    fingerprint = payload["sources"]["selected_rows_sha256"]
    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64
    assert payload["sources"]["market_hash_basis"].startswith("canonical-json-v1:")

    conn = open_connection(sqlite_path)
    try:
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, low, close, adjustment_factor) "
            "VALUES (?, ?, ?, ?, ?)",
            ("9999", "2026-06-04", 500.0, 510.0, 1.0),
        )
        conn.commit()
    finally:
        conn.close()
    assert (
        hashlib.sha256(sqlite_path.read_bytes()).hexdigest() != hashlib.sha256(before).hexdigest()
    )
    exit_code, repeated = _run(tmp_path, capsys, sqlite_path=sqlite_path)
    assert exit_code == 0
    assert repeated["sources"]["selected_rows_sha256"] == fingerprint


def test_limit_outcome_ignores_market_rows_after_fixed_horizon(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _market_sqlite(sqlite_path)
    _, baseline = _run(tmp_path, capsys, sqlite_path=sqlite_path)
    conn = open_connection(sqlite_path)
    try:
        conn.execute(
            "INSERT INTO jquants_market_calendar(day, is_business_day) VALUES (?, 1)",
            ("2026-06-15",),
        )
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, low, close, adjustment_factor) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2331", "2026-06-15", 900.0, 900.0, 0.5),
        )
        conn.execute(
            "UPDATE source_coverage SET coverage_end = ? WHERE source = 'jquants_market_calendar'",
            ("2026-06-15",),
        )
        conn.commit()
    finally:
        conn.close()

    exit_code, later = _run(
        tmp_path,
        capsys,
        sqlite_path=sqlite_path,
        asof=date(2026, 6, 15),
    )

    assert exit_code == 0
    assert later["status"] == "observed"
    assert later["sources"]["selected_rows_sha256"] == baseline["sources"]["selected_rows_sha256"]
