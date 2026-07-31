from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tools.cloud.batch_summary import (
    ERROR_STAGES,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_SUCCEEDED,
    load_batch_execution_summary,
)
from tools.cloud.daily_batch import (
    BatchStepError,
    CalendarCoverageError,
    CommandResult,
    _macro_refresh_groups,
    _parse_macro_series,
    main,
    run_daily_batch,
)

from baibai_engine.macro.indicators.service import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
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
SELECT_OK = CommandResult(
    0,
    "selection_id: sel-1\n"
    "recommendations:\n"
    '  - ticker: "2331"\n'
    '  - ticker: "0001"\n'
    "selection:\n"
    "  profile: value_default\n",
    "",
)


def _macro_list_row(
    series_id: str,
    category: str,
    geography: str,
    frequency: str,
    provider: str,
    *,
    kind: str = "http",
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
        "kind": kind,
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
        lines = ['asof_date: "2026-07-21"', "universe_size: 3800", "candidates:"]
        if revision_id is not None:
            lines.insert(0, f'run_revision_id: "{revision_id}"')
            lines.extend(['  - ticker: "2331"', '  - ticker: "0001"'])
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
        "screening refresh-edinet-documents": [OK],
        "screening verify-cache-coverage": [OK],
        "screening run": [RUN_OK],
        "task reconcile-earnings": [OK],
        "screening select": [SELECT_OK],
        "macro list": [MACRO_LIST_OK],
        "macro refresh": [OK, OK, OK],
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


def _call(runner: ScriptedRunner, key: str) -> list[str]:
    """The one call with this key, so adding a step does not renumber assertions.

    Refuses a key that ran more than once rather than returning the first: a step
    that repeats (macro refresh, the coverage recheck) needs the caller to say
    which occurrence it means.
    """
    matches = [argv for argv in runner.calls if _key(argv) == key]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {key!r} call, got {len(matches)}")
    return matches[0]


def test_daily_batch_runs_full_chain_with_explicit_asof(tmp_path: Path) -> None:
    runner = _runner()
    output_dir = tmp_path / "serving"

    exit_code = run_daily_batch(root=tmp_path, output_dir=output_dir, asof=ASOF, runner=runner)

    assert exit_code == 0
    assert runner.call_keys() == [
        "screening refresh-edinet-documents",
        "screening verify-cache-coverage",
        "screening run",
        "screening select",
        "macro list",
        "macro refresh",
        "macro refresh",
        "macro refresh",
        "export",
        "screening prune",
        "task reconcile-earnings",
    ]

    run_argv = _call(runner, "screening run")
    assert run_argv[:3] == ["baibai-engine", "screening", "run"]
    assert run_argv[3:5] == ["--asof", "2026-07-21"]
    assert "--output-path" in run_argv

    select_argv = _call(runner, "screening select")
    assert select_argv[3:] == [
        "--asof",
        "2026-07-21",
        "--run-revision-id",
        "rev-1",
        "--longlist-top",
        "20",
    ]

    export_argv = _call(runner, "export")
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
    assert runner.call_keys()[:6] == [
        "screening refresh-edinet-documents",
        "screening verify-cache-coverage",
        "screening bootstrap-cache",
        "screening extract-edinet-metrics",
        "screening verify-cache-coverage",
        "screening run",
    ]
    assert runner.calls[2][3:] == ["--asof", "2026-07-21"]
    assert runner.calls[3][3:] == ["--asof", "2026-07-21"]


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
        "screening refresh-edinet-documents": [OK],
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
    assert "export" in keys
    # The deferred detail is surfaced immediately, not only in the final summary.
    assert "deferred failure" in capsys.readouterr().err


@pytest.mark.parametrize("returncode", [0, 1])
def test_daily_batch_always_echoes_registry_prune_audit_lines(
    tmp_path: Path, capsys, returncode: int
) -> None:
    script = _success_script()
    script["macro refresh"] = [
        CommandResult(
            returncode,
            "us.10y\t2026-07-21\t4.2\n"
            "registry-prune-pending\tretired.series\t"
            "observations=42\tprovider_runs=3\ttransaction=abc\n"
            "registry-prune\tretired.series\tobservations=42\tprovider_runs=3\n",
            "provider down\n" if returncode else "",
        ),
        OK,
        OK,
    ]
    runner = _runner(script)

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    output = capsys.readouterr().out
    assert "registry-prune-pending\tretired.series" in output
    assert "registry-prune\tretired.series\tobservations=42\tprovider_runs=3" in output
    assert "us.10y\t2026-07-21\t4.2" not in output
    assert exit_code == (3 if returncode else 0)


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
    assert runner.call_keys()[-1] == "task reconcile-earnings"


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


# --- structured summary output --------------------------------------------


def _export_meta_writer(argv: list[str]) -> None:
    """Mimic export_read_models: write views/meta.json so local export is visible."""

    idx = argv.index("--output-dir")
    views = Path(argv[idx + 1]) / "views"
    views.mkdir(parents=True, exist_ok=True)
    (views / "meta.json").write_text("{}\n", encoding="utf-8")


def _summary_runner(
    results: dict[str, list[CommandResult]] | None = None,
) -> ScriptedRunner:
    return ScriptedRunner(
        results or _success_script(),
        writers={"screening run": _run_yaml_writer("rev-1"), "export": _export_meta_writer},
    )


def test_daily_batch_writes_succeeded_summary(tmp_path: Path) -> None:
    runner = _summary_runner()
    summary_path = tmp_path / "summary.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=runner,
        summary_output=summary_path,
    )

    assert exit_code == 0
    summary = load_batch_execution_summary(summary_path)
    assert summary.outcome == OUTCOME_SUCCEEDED
    assert summary.asof == "2026-07-21"
    assert summary.local_export is True
    assert [batch.batch_name for batch in summary.batches] == [
        "screening",
        "macro",
        "serving-export",
        "prune",
        "task-reconcile",
    ]
    screening = summary.batches[0]
    assert screening.metrics == {
        "asof": "2026-07-21",
        "run_revision_id": "rev-1",
        "selection_id": "sel-1",
        "universe": 3800,
        "candidates": 2,
        "selected": 2,
    }
    macro = summary.batches[1]
    assert macro.metrics == {"target": 5, "success": 5, "failure": 0}
    assert macro.status == "ok"
    export = summary.batches[2]
    # The delta view is absent in this fixture, so the export reports "not measured"
    # rather than zero counts that would read as "nothing changed".
    assert export.metrics == {
        "local_output": True,
        "delta_measured": False,
        "delta_entered": 0,
        "delta_exited": 0,
        "delta_er_moves": 0,
        "delta_holdings": 0,
        "delta_macro_flags": 0,
        "delta_macro_extremes": 0,
        "delta_unavailable": "view_unreadable",
    }


def test_daily_batch_writes_skipped_summary(tmp_path: Path) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: "0"})
    summary_path = tmp_path / "summary.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=None,
        runner=_summary_runner({}),
        summary_output=summary_path,
    )

    assert exit_code == 0
    summary = load_batch_execution_summary(summary_path)
    assert summary.outcome == OUTCOME_SKIPPED
    assert summary.batches == ()
    assert summary.local_export is False


def test_daily_batch_writes_degraded_summary_on_deferred_macro_failure(tmp_path: Path) -> None:
    script = _success_script()
    script["macro refresh"] = [CommandResult(1, "", "provider down\n"), OK, OK]
    summary_path = tmp_path / "summary.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=_summary_runner(script),
        summary_output=summary_path,
    )

    assert exit_code == 3
    summary = load_batch_execution_summary(summary_path)
    assert summary.outcome == OUTCOME_DEGRADED
    macro = next(batch for batch in summary.batches if batch.batch_name == "macro")
    assert macro.status == "degraded"
    assert macro.metrics == {"target": 5, "success": 4, "failure": 1}
    assert len(macro.errors) == 1
    assert macro.errors[0].code == "subprocess_failed"
    assert macro.errors[0].stage == "macro-refresh"
    assert macro.errors[0].impact == "degraded"


def test_daily_batch_counts_only_the_series_the_refresh_reported_as_failed(
    tmp_path: Path,
) -> None:
    script = _success_script()
    # The last group holds three series; one of them fails.
    script["macro refresh"] = [
        OK,
        OK,
        CommandResult(
            1,
            "",
            "error: 1 of 3 series failed to refresh:\n"
            "- jp.gdp: source unavailable\n"
            "error: refresh failed for 1 of 3 series: jp.gdp\n",
        ),
    ]
    summary_path = tmp_path / "summary.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=_summary_runner(script),
        summary_output=summary_path,
    )

    assert exit_code == 3
    summary = load_batch_execution_summary(summary_path)
    macro = next(batch for batch in summary.batches if batch.batch_name == "macro")
    # Counting the whole group would report 3 failures and 2 successes.
    assert macro.metrics == {"target": 5, "success": 4, "failure": 1}


def test_daily_batch_counts_the_whole_group_when_the_refresh_reports_no_count(
    tmp_path: Path,
) -> None:
    script = _success_script()
    script["macro refresh"] = [OK, OK, CommandResult(1, "", "Traceback: exploded\n")]
    summary_path = tmp_path / "summary.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=_summary_runner(script),
        summary_output=summary_path,
    )

    assert exit_code == 3
    summary = load_batch_execution_summary(summary_path)
    macro = next(batch for batch in summary.batches if batch.batch_name == "macro")
    # A step that died before reporting says nothing about which series survived,
    # so the health signal errs high rather than claiming successes it cannot see.
    assert macro.metrics == {"target": 5, "success": 2, "failure": 3}


def test_daily_batch_writes_failed_summary_on_fatal_screening_failure(tmp_path: Path) -> None:
    script = _success_script()
    script["screening run"] = [CommandResult(1, "", "boom\n")]
    summary_path = tmp_path / "summary.json"

    with pytest.raises(BatchStepError):
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=ASOF,
            runner=_summary_runner(script),
            summary_output=summary_path,
        )

    summary = load_batch_execution_summary(summary_path)
    assert summary.outcome == OUTCOME_FAILED
    assert [batch.batch_name for batch in summary.batches] == ["screening"]
    screening = summary.batches[0]
    assert screening.status == "failed"
    assert screening.metrics == {}
    assert screening.errors[0].code == "subprocess_failed"
    assert screening.errors[0].stage == "screening-run"
    # The redacted message carries the stage + return code, never the stderr text.
    assert "boom" not in screening.errors[0].message


def test_daily_batch_failed_summary_redacts_calendar_error(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"

    with pytest.raises(CalendarCoverageError):
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=None,
            runner=_summary_runner({}),
            summary_output=summary_path,
        )

    summary = load_batch_execution_summary(summary_path)
    assert summary.outcome == OUTCOME_FAILED
    assert summary.batches[0].errors[0].code == "calendar_store_missing"


def test_main_writes_invalid_asof_summary(tmp_path: Path, capsys) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "method").mkdir()
    summary_path = tmp_path / "summary.json"

    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "serving"),
            "--asof",
            "2026-13-99",
            "--summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == 1
    assert "not a valid YYYY-MM-DD date" in capsys.readouterr().err
    summary = load_batch_execution_summary(summary_path)
    assert summary.outcome == OUTCOME_FAILED
    assert summary.batches[0].errors[0].code == "invalid_asof"


def test_parse_macro_series_reads_json_list() -> None:
    stdout = json.dumps(
        [
            _macro_list_row("us.10y", "rates", "us", "daily", "fred_csv"),
            _macro_list_row("jp.pmi_manufacturing", "activity", "jp", "monthly", "spglobal_pmi"),
        ]
    )

    parsed = _parse_macro_series(stdout)

    assert [item.series_id for item in parsed] == ["us.10y", "jp.pmi_manufacturing"]


def test_parse_macro_series_rejects_non_json() -> None:
    with pytest.raises(BatchStepError, match="not parseable JSON"):
        _parse_macro_series("us.10y\tdaily\tfred_csv")


def test_macro_refresh_groups_buckets_every_series_by_frequency_window() -> None:
    parsed = _parse_macro_series(
        json.dumps(
            [
                _macro_list_row("us.10y", "rates", "us", "daily", "fred_csv"),
                _macro_list_row(
                    "jp.pmi_manufacturing", "activity", "jp", "monthly", "spglobal_pmi"
                ),
                _macro_list_row("jp.cpi", "prices", "jp", "monthly", "estat"),
            ]
        )
    )

    groups = _macro_refresh_groups(parsed)

    assert groups == [
        (LATEST_FETCH_LOOKBACK_DAYS["daily"], ["us.10y"]),
        (DEFAULT_LATEST_LOOKBACK_DAYS, ["jp.pmi_manufacturing", "jp.cpi"]),
    ]


def test_macro_refresh_groups_orders_derived_after_base() -> None:
    parsed = _parse_macro_series(
        json.dumps(
            [
                _macro_list_row(
                    "gold_copper_ratio", "commodity", "world", "daily", "derived", kind="local"
                ),
                _macro_list_row("us.10y", "rates", "us", "daily", "fred_csv"),
                _macro_list_row("gold", "commodity", "world", "daily", "yahoo"),
            ]
        )
    )

    groups = _macro_refresh_groups(parsed)

    # Same daily window, but base (http) is refreshed before the derived (local) series.
    assert groups == [
        (LATEST_FETCH_LOOKBACK_DAYS["daily"], ["us.10y", "gold"]),
        (LATEST_FETCH_LOOKBACK_DAYS["daily"], ["gold_copper_ratio"]),
    ]


def test_every_batch_step_name_is_a_known_error_stage() -> None:
    """A step whose name the summary schema does not know replaces the real failure.

    `_finalize_failed_summary` classifies the error while composing the summary, so
    an unregistered stage raises there and the operator gets a validation error
    instead of the failure that actually happened. The call sites are read with
    `ast` rather than a regex: several pass a call inside `argv=(...)`, which a
    paren-counting pattern skips silently — and skipping is indistinguishable from
    passing.
    """
    import ast

    from tools.cloud.daily_batch import _normalize_stage

    source = (Path(__file__).resolve().parents[1] / "tools" / "cloud" / "daily_batch.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    names: list[str] = []
    dynamic: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not (isinstance(callee, ast.Name) and callee.id == "_run_step"):
            continue
        keyword = next((item for item in node.keywords if item.arg == "name"), None)
        assert keyword is not None, "every _run_step call names its step"
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            names.append(keyword.value.value)
        else:
            # An f-string name cannot be checked statically; `_normalize_stage`
            # strips the varying part, so pin the literal prefix instead.
            dynamic.append(ast.unparse(keyword.value))

    # Guards the scan itself: a parser that silently matches nothing would make
    # this test pass while checking no step at all.
    assert len(names) + len(dynamic) == 12, "the scan lost or gained call sites"
    unregistered = {name for name in names if _normalize_stage(name) not in set(ERROR_STAGES)}
    assert unregistered == set()
    assert len(dynamic) == 1
    assert "macro-refresh" in dynamic[0]
    assert _normalize_stage("macro-refresh-370d") in set(ERROR_STAGES)


def test_a_reconcile_failure_degrades_the_batch_without_losing_the_publish(
    tmp_path: Path,
) -> None:
    """The step reports and writes nothing, so it must never cost the day's publish.

    Pins the deferral itself: replacing the try/except with a bare `_run_step`
    puts the step back on the critical path, and nothing else in the suite would
    notice.
    """
    script = _success_script()
    script["task reconcile-earnings"] = [CommandResult(returncode=1, stdout="", stderr="boom")]
    runner = ScriptedRunner(
        script, writers={"screening run": _run_yaml_writer("rev-1"), "export": _export_meta_writer}
    )
    summary_output = tmp_path / "summary.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=runner,
        summary_output=summary_output,
    )

    keys = runner.call_keys()
    assert "screening select" in keys
    assert "export" in keys
    summary = load_batch_execution_summary(summary_output)
    assert summary.outcome == OUTCOME_DEGRADED
    reconcile = next(batch for batch in summary.batches if batch.batch_name == "task-reconcile")
    assert reconcile.status == "degraded"
    assert [error.stage for error in reconcile.errors] == ["task-reconcile-earnings"]
    prune = next(batch for batch in summary.batches if batch.batch_name == "prune")
    assert prune.status == "ok"
    assert exit_code != 0


def test_a_fatal_failure_still_writes_a_summary_when_its_stage_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composing the summary must not be able to replace the failure it describes.

    Classifying the error happens while composing, so an unregistered stage raises
    there. With that work outside the guard the validation error escapes and the
    real failure is lost; this pins it inside.
    """
    import tools.cloud.batch_summary as batch_summary

    monkeypatch.setattr(
        batch_summary, "ERROR_STAGES", tuple(s for s in ERROR_STAGES if s != "screening-run")
    )
    script = _success_script()
    script["screening run"] = [CommandResult(returncode=1, stdout="", stderr="boom")]
    runner = ScriptedRunner(
        script, writers={"screening run": _run_yaml_writer("rev-1"), "export": _export_meta_writer}
    )
    summary_output = tmp_path / "summary.json"

    with pytest.raises(BatchStepError):
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=ASOF,
            runner=runner,
            summary_output=summary_output,
        )
