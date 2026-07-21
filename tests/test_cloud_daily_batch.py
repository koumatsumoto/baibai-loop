from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tools.cloud.daily_batch import (
    BatchStepError,
    CalendarCoverageError,
    CommandResult,
    main,
    run_daily_batch,
)

JST = ZoneInfo("Asia/Tokyo")
ASOF = date(2026, 7, 21)

OK = CommandResult(0, "", "")
RUN_OK = CommandResult(
    0,
    "screening run done: status=ok; run_revision_id=rev-1; output=None; "
    "universe=3800; candidates=42\n",
    "",
)
SELECT_OK = CommandResult(0, "selection_id: sel-1\nselection:\n  profile: value_default\n", "")
MACRO_LIST_OK = CommandResult(
    0,
    "us.10y\tUS 10Y Treasury\trates\tus\tdaily\t%\tfred_csv\n"
    "jp.foreign_flows\tForeign flows\tflows\tjp\tweekly\tJPY\tjquants_flows\n"
    "jp.cpi_all\tJP CPI\tprices\tjp\tmonthly\tindex\testat\n"
    "jp.gdp\tJP GDP\tgrowth\tjp\tquarterly\tJPY\testat\n"
    "jp.bankruptcies\tBankruptcies\tcredit\tjp\tmonthly\tcount\tmanual\n",
    "",
)


def _key(argv: list[str]) -> str:
    if argv[0] == sys.executable:
        return "export"
    return " ".join(argv[1:3])


class ScriptedRunner:
    def __init__(self, results: dict[str, list[CommandResult]]) -> None:
        self._results = {key: list(queue) for key, queue in results.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd) -> CommandResult:
        self.calls.append(list(argv))
        queue = self._results.get(_key(list(argv)))
        if not queue:
            raise AssertionError(f"unexpected command: {argv}")
        return queue.pop(0)

    def call_keys(self) -> list[str]:
        return [_key(argv) for argv in self.calls]


def _success_script() -> dict[str, list[CommandResult]]:
    return {
        "screening verify-cache-coverage": [OK],
        "screening run": [RUN_OK],
        "screening select": [SELECT_OK],
        "macro list": [MACRO_LIST_OK],
        "macro refresh": [OK, OK, OK],
        "macro import-manual": [OK],
        "export": [OK],
    }


def _seed_calendar(root: Path, rows: dict[date, int]) -> None:
    db = root / "data/screening/market.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE jquants_market_calendar("
            "day TEXT PRIMARY KEY, is_business_day INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO jquants_market_calendar(day, is_business_day) VALUES (?, ?)",
            [(day.isoformat(), flag) for day, flag in rows.items()],
        )
        conn.commit()
    finally:
        conn.close()


def test_daily_batch_runs_full_chain_with_explicit_asof(tmp_path: Path) -> None:
    runner = ScriptedRunner(_success_script())
    output_dir = tmp_path / "serving"

    exit_code = run_daily_batch(root=tmp_path, output_dir=output_dir, asof=ASOF, runner=runner)

    assert exit_code == 0
    assert runner.call_keys() == [
        "screening verify-cache-coverage",
        "screening run",
        "screening select",
        "macro list",
        "macro refresh",
        "macro refresh",
        "macro refresh",
        "macro import-manual",
        "export",
    ]

    run_argv = runner.calls[1]
    assert run_argv[:3] == ["baibai-engine", "screening", "run"]
    assert run_argv[3:] == ["--asof", "2026-07-21"]

    select_argv = runner.calls[2]
    assert select_argv[3:] == ["--asof", "2026-07-21", "--run-revision-id", "rev-1"]

    export_argv = runner.calls[8]
    assert export_argv[0] == sys.executable
    assert export_argv[1].endswith("tools/cloud/export_read_models.py")
    assert export_argv[2:] == [
        "--output-dir",
        str(output_dir),
        "--batch",
        "daily",
        "--repo-root",
        str(tmp_path),
    ]


def test_daily_batch_refreshes_registered_series_by_frequency_window(tmp_path: Path) -> None:
    runner = ScriptedRunner(_success_script())

    run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    refresh_calls = [argv for argv in runner.calls if _key(argv) == "macro refresh"]
    assert [argv[3:] for argv in refresh_calls] == [
        ["us.10y", "--start", (ASOF - timedelta(days=14)).isoformat(), "--end", "2026-07-21"],
        [
            "jp.foreign_flows",
            "--start",
            (ASOF - timedelta(days=60)).isoformat(),
            "--end",
            "2026-07-21",
        ],
        [
            "jp.cpi_all",
            "jp.gdp",
            "--start",
            (ASOF - timedelta(days=370)).isoformat(),
            "--end",
            "2026-07-21",
        ],
    ]
    # The manual series only syncs through import-manual.
    assert not any("jp.bankruptcies" in argv for argv in refresh_calls)
    assert ["baibai-engine", "macro", "import-manual"] in runner.calls


def test_daily_batch_bootstraps_cache_when_coverage_is_incomplete(tmp_path: Path) -> None:
    script = _success_script()
    script["screening verify-cache-coverage"] = [
        CommandResult(1, "SQLite cache coverage incomplete\n", ""),
        OK,
    ]
    script["screening bootstrap-cache"] = [OK]
    script["screening extract-edinet-metrics"] = [OK]
    runner = ScriptedRunner(script)

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 0
    assert runner.call_keys()[:5] == [
        "screening verify-cache-coverage",
        "screening bootstrap-cache",
        "screening extract-edinet-metrics",
        "screening verify-cache-coverage",
        "screening run",
    ]
    assert runner.calls[1][3:] == ["--asof", "2026-07-21"]
    assert runner.calls[2][3:] == ["--asof", "2026-07-21"]


def test_daily_batch_stops_when_coverage_stays_incomplete_after_bootstrap(tmp_path: Path) -> None:
    incomplete = CommandResult(1, "SQLite cache coverage incomplete\n", "")
    runner = ScriptedRunner(
        {
            "screening verify-cache-coverage": [incomplete, incomplete],
            "screening bootstrap-cache": [OK],
            "screening extract-edinet-metrics": [OK],
        }
    )

    with pytest.raises(BatchStepError, match="recheck"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    assert "screening run" not in runner.call_keys()


def test_daily_batch_stops_on_step_failure_with_stderr_summary(tmp_path: Path) -> None:
    script = _success_script()
    script["screening run"] = [CommandResult(2, "", "boom\nprovider unavailable\n")]
    runner = ScriptedRunner(script)

    with pytest.raises(BatchStepError) as excinfo:
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    message = str(excinfo.value)
    assert "screening-run" in message
    assert "exit 2" in message
    assert "provider unavailable" in message
    assert "screening select" not in runner.call_keys()


def test_daily_batch_stops_when_run_output_lacks_run_revision_id(tmp_path: Path) -> None:
    script = _success_script()
    script["screening run"] = [CommandResult(0, "screening run done: status=ok\n", "")]
    runner = ScriptedRunner(script)

    with pytest.raises(BatchStepError, match="run_revision_id"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    assert "screening select" not in runner.call_keys()


def test_daily_batch_stops_when_select_output_lacks_selection_id(tmp_path: Path) -> None:
    script = _success_script()
    script["screening select"] = [CommandResult(0, "selection:\n  profile: value_default\n", "")]
    runner = ScriptedRunner(script)

    with pytest.raises(BatchStepError, match="selection_id"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    assert "macro list" not in runner.call_keys()


def test_daily_batch_skips_non_business_day(tmp_path: Path, capsys) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: 0})
    runner = ScriptedRunner({})

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )

    assert exit_code == 0
    assert runner.calls == []
    assert f"skip: {today.isoformat()} は非営業日" in capsys.readouterr().out


def test_daily_batch_proceeds_on_business_day(tmp_path: Path) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: 1})
    runner = ScriptedRunner(_success_script())

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )

    assert exit_code == 0
    assert runner.calls[0][3:] == ["--asof", today.isoformat()]
    assert runner.call_keys()[-1] == "export"


def test_daily_batch_errors_when_calendar_does_not_cover_the_date(tmp_path: Path) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today - timedelta(days=30): 1})
    runner = ScriptedRunner({})

    with pytest.raises(CalendarCoverageError, match="does not cover"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner)

    assert runner.calls == []


def test_daily_batch_errors_when_calendar_table_is_missing(tmp_path: Path) -> None:
    db = tmp_path / "data/screening/market.sqlite"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    runner = ScriptedRunner({})

    with pytest.raises(CalendarCoverageError, match="unreadable"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner)


def test_daily_batch_errors_when_market_sqlite_is_absent(tmp_path: Path) -> None:
    runner = ScriptedRunner({})

    with pytest.raises(CalendarCoverageError, match="does not exist"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner)


def test_main_rejects_a_root_without_project_markers(tmp_path: Path, capsys) -> None:
    exit_code = main(["--output-dir", str(tmp_path / "serving"), "--repo-root", str(tmp_path)])

    assert exit_code == 1
    assert "pyproject.toml" in capsys.readouterr().err
