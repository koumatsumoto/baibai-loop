from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tools.cloud.daily_batch import (
    _MACRO_REFRESH_WINDOW_DAYS,
    _MACRO_REFRESH_WINDOW_DEFAULT_DAYS,
    BatchStepError,
    CalendarCoverageError,
    CommandResult,
    _macro_refresh_groups,
    _parse_macro_series,
    main,
    run_daily_batch,
)

from baibai_engine.market.sqlite import store_jquants_market_calendar
from baibai_engine.screening.run_store import ScreeningRunStore

JST = ZoneInfo("Asia/Tokyo")
ASOF = date(2026, 7, 21)

OK = CommandResult(0, "", "")
RUN_OK = CommandResult(
    0,
    "screening run done: status=ok; run_revision_id=rev-1; output=/tmp/run.yaml; "
    "universe=3800; candidates=42\n",
    "",
)
SELECT_OK = CommandResult(0, "selection_id: sel-1\nselection:\n  profile: value_default\n", "")


def _macro_list_row(
    series_id: str,
    category: str,
    geography: str,
    frequency: str,
    provider: str,
    *,
    refreshable: bool = True,
) -> dict[str, object]:
    """One `macro list --format json` row as the CLI emits it."""

    return {
        "series_id": series_id,
        "name": series_id,
        "category": category,
        "geography": geography,
        "frequency": frequency,
        "unit": "unit",
        "provider": provider,
        "refreshable": refreshable,
    }


MACRO_LIST_OK = CommandResult(
    0,
    json.dumps(
        [
            _macro_list_row("us.10y", "rates", "us", "daily", "fred_csv"),
            _macro_list_row("jp.foreign_flows", "flows", "jp", "weekly", "jquants_flows"),
            _macro_list_row("jp.cpi_all", "prices", "jp", "monthly", "estat"),
            _macro_list_row("jp.gdp", "growth", "jp", "quarterly", "estat"),
            _macro_list_row("jp.bankruptcies", "credit", "jp", "monthly", "tsr_bankruptcies"),
        ]
    ),
    "",
)


def _key(argv: list[str]) -> str:
    if argv[0].endswith("python") or "export_read_models" in " ".join(argv):
        return "export"
    return " ".join(argv[1:3])


def _run_yaml_writer(revision_id: str | None) -> Callable[[list[str]], None]:
    """Mimic `screening run --output-path`: write the YAML view the batch reads."""

    def _writer(argv: list[str]) -> None:
        idx = argv.index("--output-path")
        lines = ['asof_date: "2026-07-21"']
        if revision_id is not None:
            lines.insert(0, f'run_revision_id: "{revision_id}"')
        Path(argv[idx + 1]).write_text("\n".join(lines) + "\n", encoding="utf-8")

    return _writer


class ScriptedRunner:
    def __init__(
        self,
        results: dict[str, list[CommandResult]],
        writers: dict[str, Callable[[list[str]], None]] | None = None,
    ) -> None:
        self._results = {key: list(queue) for key, queue in results.items()}
        self._writers = writers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd) -> CommandResult:
        self.calls.append(list(argv))
        key = _key(list(argv))
        writer = self._writers.get(key)
        if writer is not None:
            writer(list(argv))
        queue = self._results.get(key)
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
        "screening prune": [OK],
    }


def _runner(
    results: dict[str, list[CommandResult]] | None = None,
    *,
    run_revision_id: str | None = "rev-1",
) -> ScriptedRunner:
    return ScriptedRunner(
        results or _success_script(),
        writers={"screening run": _run_yaml_writer(run_revision_id)},
    )


def _publish_run(root: Path, *, asof: str, run_at: str, revision_id: str) -> None:
    store = ScreeningRunStore(root / "data/screening/runs.sqlite")
    store.publish_run(
        {
            "run_id": f"screening-{asof.replace('-', '')}",
            "run_date": asof,
            "asof_date": asof,
            "run_at": run_at,
            "universe_size": 0,
            "candidates": [],
        },
        run_revision_id=revision_id,
    )


def _seed_calendar(root: Path, rows: dict[date, str]) -> None:
    db = root / "data/screening/market.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    days = sorted(rows)
    store_jquants_market_calendar(
        db,
        [{"Date": day.isoformat(), "HolidayDivision": division} for day, division in rows.items()],
        requested_start=days[0],
        requested_end=days[-1],
    )


def test_daily_batch_runs_full_chain_with_explicit_asof(tmp_path: Path) -> None:
    runner = _runner()
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
        "screening prune",
    ]

    run_argv = runner.calls[1]
    assert run_argv[:3] == ["baibai-engine", "screening", "run"]
    assert run_argv[3:5] == ["--asof", "2026-07-21"]
    assert "--output-path" in run_argv

    select_argv = runner.calls[2]
    assert select_argv[3:] == ["--asof", "2026-07-21", "--run-revision-id", "rev-1"]

    export_argv = runner.calls[8]
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
    runner = _runner()

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
            "jp.bankruptcies",
            "--start",
            (ASOF - timedelta(days=370)).isoformat(),
            "--end",
            "2026-07-21",
        ],
    ]
    # PMI remains a manual series and only syncs through import-manual.
    assert ["baibai-engine", "macro", "import-manual"] in runner.calls


def test_macro_refresh_windows_match_engine_latest_fetch_lookback() -> None:
    from baibai_engine.macro.indicators.service import (
        DEFAULT_LATEST_LOOKBACK_DAYS,
        LATEST_FETCH_LOOKBACK_DAYS,
    )

    assert _MACRO_REFRESH_WINDOW_DEFAULT_DAYS == DEFAULT_LATEST_LOOKBACK_DAYS
    for frequency, expected in LATEST_FETCH_LOOKBACK_DAYS.items():
        resolved = _MACRO_REFRESH_WINDOW_DAYS.get(frequency, _MACRO_REFRESH_WINDOW_DEFAULT_DAYS)
        assert resolved == expected


def test_daily_batch_bootstraps_cache_when_coverage_is_incomplete(tmp_path: Path) -> None:
    script = _success_script()
    script["screening verify-cache-coverage"] = [
        CommandResult(1, "SQLite cache coverage incomplete for --asof 2026-07-21\n", ""),
        OK,
    ]
    script["screening bootstrap-cache"] = [OK]
    script["screening extract-edinet-metrics"] = [OK]
    runner = _runner(script)

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


def test_daily_batch_treats_verify_exit1_without_marker_as_crash(tmp_path: Path) -> None:
    script = _success_script()
    script["screening verify-cache-coverage"] = [
        CommandResult(1, "", "Traceback: ConfigError: rules file is corrupt\n")
    ]
    runner = _runner(script)

    with pytest.raises(BatchStepError, match="without the coverage-incomplete marker"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    assert "screening bootstrap-cache" not in runner.call_keys()
    assert "screening run" not in runner.call_keys()


def test_daily_batch_stops_when_coverage_stays_incomplete_after_bootstrap(tmp_path: Path) -> None:
    incomplete = CommandResult(1, "SQLite cache coverage incomplete for --asof 2026-07-21\n", "")
    script = {
        "screening verify-cache-coverage": [incomplete, incomplete],
        "screening bootstrap-cache": [OK],
        "screening extract-edinet-metrics": [OK],
    }
    runner = _runner(script)

    with pytest.raises(BatchStepError, match="recheck"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    assert "screening run" not in runner.call_keys()


def test_daily_batch_stops_on_step_failure_with_stderr_summary(tmp_path: Path) -> None:
    script = _success_script()
    script["screening run"] = [CommandResult(1, "", "boom\nprovider unavailable\n")]
    runner = _runner(script)

    with pytest.raises(BatchStepError) as excinfo:
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    message = str(excinfo.value)
    assert "screening-run" in message
    assert "exit 1" in message
    assert "provider unavailable" in message
    assert "screening select" not in runner.call_keys()


def test_daily_batch_continues_when_run_reports_partial_warning(tmp_path: Path, capsys) -> None:
    script = _success_script()
    script["screening run"] = [
        CommandResult(
            2,
            "screening run done: status=partial warning; run_revision_id=rev-1; "
            "output=/tmp/run.yaml; universe=3800; candidates=42\n"
            "screening run partial warning reasons:\n- ttm_quality 非 exact 件数: 10\n",
            "",
        )
    ]
    runner = _runner(script)

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 0
    assert "export" in runner.call_keys()
    assert "partial warning (run is published" in capsys.readouterr().out


def test_daily_batch_defers_macro_refresh_failure_until_after_export(
    tmp_path: Path, capsys
) -> None:
    script = _success_script()
    script["macro refresh"] = [
        CommandResult(1, "", "provider down\n"),
        OK,
        OK,
    ]
    runner = _runner(script)

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 3
    keys = runner.call_keys()
    assert keys.count("macro refresh") == 3
    assert "macro import-manual" in keys
    assert "export" in keys
    # The deferred detail is surfaced immediately, not only in the final summary.
    assert "deferred failure" in capsys.readouterr().err


def test_daily_batch_passes_previous_run_revision_when_resolvable(tmp_path: Path) -> None:
    _publish_run(
        tmp_path, asof="2026-07-17", run_at="2026-07-17T18:00:00+09:00", revision_id="old-1"
    )
    _publish_run(
        tmp_path, asof="2026-07-17", run_at="2026-07-17T19:00:00+09:00", revision_id="old-2"
    )
    _publish_run(tmp_path, asof="2026-07-21", run_at="2026-07-21T18:00:00+09:00", revision_id="cur")
    runner = _runner()

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 0
    select_argv = next(argv for argv in runner.calls if _key(argv) == "screening select")
    assert select_argv[-2:] == ["--previous-run-revision-id", "old-2"]


def test_daily_batch_stops_with_message_when_runs_store_is_corrupt(tmp_path: Path) -> None:
    runs_db = tmp_path / "data/screening/runs.sqlite"
    runs_db.parent.mkdir(parents=True, exist_ok=True)
    runs_db.write_bytes(b"this is not a sqlite database")
    runner = _runner()

    with pytest.raises(BatchStepError, match="runs store is unreadable"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)


def test_daily_batch_defers_macro_list_failure_and_still_exports(tmp_path: Path) -> None:
    script = _success_script()
    script["macro list"] = [CommandResult(1, "", "boom\n")]
    runner = _runner(script)

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 3
    keys = runner.call_keys()
    assert "macro refresh" not in keys
    assert "macro import-manual" in keys
    assert "export" in keys


def test_daily_batch_stops_when_run_view_lacks_run_revision_id(tmp_path: Path) -> None:
    runner = _runner(run_revision_id=None)

    with pytest.raises(BatchStepError, match="run_revision_id"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    assert "screening select" not in runner.call_keys()


def test_daily_batch_stops_when_select_output_lacks_selection_id(tmp_path: Path) -> None:
    script = _success_script()
    script["screening select"] = [CommandResult(0, "selection:\n  profile: value_default\n", "")]
    runner = _runner(script)

    with pytest.raises(BatchStepError, match="selection_id"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    assert "macro list" not in runner.call_keys()


def test_daily_batch_skips_non_business_day(tmp_path: Path, capsys) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: "0"})
    runner = _runner({})

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )

    assert exit_code == 0
    assert runner.calls == []
    assert f"skip: {today.isoformat()} は非営業日" in capsys.readouterr().out


def test_daily_batch_proceeds_on_business_day(tmp_path: Path) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: "1"})
    runner = _runner()

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )

    assert exit_code == 0
    assert runner.calls[0][3:] == ["--asof", today.isoformat()]
    assert runner.call_keys()[-1] == "screening prune"


def test_daily_batch_errors_when_calendar_does_not_cover_the_date(tmp_path: Path) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today - timedelta(days=30): "1"})
    runner = _runner({})

    with pytest.raises(CalendarCoverageError, match="does not cover"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner)

    assert runner.calls == []


def test_daily_batch_errors_when_calendar_table_is_missing(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "data/screening/market.sqlite"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    runner = _runner({})

    with pytest.raises(CalendarCoverageError, match="unreadable"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner)


def test_daily_batch_errors_when_market_sqlite_is_absent(tmp_path: Path) -> None:
    runner = _runner({})

    with pytest.raises(CalendarCoverageError, match="does not exist"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner)


def test_main_rejects_a_root_without_project_markers(tmp_path: Path, capsys) -> None:
    exit_code = main(["--output-dir", str(tmp_path / "serving"), "--repo-root", str(tmp_path)])

    assert exit_code == 1
    assert "pyproject.toml" in capsys.readouterr().err


def test_main_requires_method_directory_in_repo_root(tmp_path: Path, capsys) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    exit_code = main(["--repo-root", str(tmp_path), "--output-dir", str(tmp_path / "serving")])

    assert exit_code == 1
    assert "does not contain method/" in capsys.readouterr().err


def test_parse_macro_series_reads_json_list() -> None:
    stdout = json.dumps(
        [
            _macro_list_row("us.10y", "rates", "us", "daily", "fred_csv"),
            _macro_list_row("jp.pmi", "activity", "jp", "monthly", "manual", refreshable=False),
        ]
    )

    parsed = _parse_macro_series(stdout)

    assert [(item.series_id, item.refreshable) for item in parsed] == [
        ("us.10y", True),
        ("jp.pmi", False),
    ]


def test_parse_macro_series_rejects_non_json() -> None:
    with pytest.raises(BatchStepError, match="not parseable JSON"):
        _parse_macro_series("us.10y\tdaily\tfred_csv")


def test_macro_refresh_groups_skips_non_refreshable_series() -> None:
    parsed = _parse_macro_series(
        json.dumps(
            [
                _macro_list_row("us.10y", "rates", "us", "daily", "fred_csv"),
                _macro_list_row("jp.pmi", "activity", "jp", "monthly", "manual", refreshable=False),
                _macro_list_row("jp.cpi", "prices", "jp", "monthly", "estat"),
            ]
        )
    )

    groups = _macro_refresh_groups(parsed)

    assert groups == [
        (_MACRO_REFRESH_WINDOW_DAYS["daily"], ["us.10y"]),
        (_MACRO_REFRESH_WINDOW_DEFAULT_DAYS, ["jp.cpi"]),
    ]
