from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from baibai_batch.jobs.daily import (
    BatchStepError,
    CalendarCoverageError,
    CommandResult,
    _macro_refresh_groups,
    _Notice,
    _parse_macro_series,
    _read_daily_delta,
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
EXTRACT_OK = CommandResult(
    0,
    "EDINET extraction summary: selected=1 reused=1 downloaded=0 "
    "quarantined_events=0 quarantined_tickers=0 quarantine_sample=none "
    "baseline_asof=2026-07-18\n",
    "",
)
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
    if "baibai_web.materialize" in argv:
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
        "screening verify-cache-coverage": [OK, OK],
        "screening bootstrap-cache": [OK],
        "screening refresh-buyback-reports": [OK],
        "screening extract-edinet-metrics": [EXTRACT_OK],
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
    store = ScreeningRunStore(root / "stores/screening/runs.sqlite")
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
    db = root / "stores/market/market.sqlite"
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
        "screening bootstrap-cache",
        "screening extract-edinet-metrics",
        "screening refresh-buyback-reports",
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
    assert export_argv[:3] == [sys.executable, "-m", "baibai_web.materialize"]
    assert export_argv[3:] == [
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
    script["screening extract-edinet-metrics"] = [EXTRACT_OK]
    runner = _runner(script)

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 0
    assert runner.call_keys()[:7] == [
        "screening refresh-edinet-documents",
        "screening verify-cache-coverage",
        "screening bootstrap-cache",
        "screening extract-edinet-metrics",
        "screening refresh-buyback-reports",
        "screening verify-cache-coverage",
        "screening run",
    ]
    assert runner.calls[2][3:] == ["--asof", "2026-07-21"]
    assert runner.calls[3][3:] == ["--asof", "2026-07-21"]


def test_daily_batch_bootstraps_cache_when_coverage_is_complete(tmp_path: Path) -> None:
    runner = _runner()

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 0
    assert runner.call_keys()[:7] == [
        "screening refresh-edinet-documents",
        "screening verify-cache-coverage",
        "screening bootstrap-cache",
        "screening extract-edinet-metrics",
        "screening refresh-buyback-reports",
        "screening verify-cache-coverage",
        "screening run",
    ]


def test_daily_batch_rejects_extraction_without_quarantine_counters(tmp_path: Path) -> None:
    script = _success_script()
    script["screening verify-cache-coverage"] = [
        CommandResult(1, "SQLite cache coverage incomplete for --asof 2026-07-21\n", ""),
    ]
    script["screening bootstrap-cache"] = [OK]
    script["screening extract-edinet-metrics"] = [OK]
    runner = _runner(script)

    with pytest.raises(BatchStepError, match="without quarantine counters"):
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=ASOF,
            runner=runner,
        )

    assert "screening run" not in runner.call_keys()


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
        "screening refresh-buyback-reports": [OK],
        "screening extract-edinet-metrics": [EXTRACT_OK],
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
    runs_db = tmp_path / "stores/screening/runs.sqlite"
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

    db = tmp_path / "stores/market/market.sqlite"
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


# --- notice for the Discord notifier ---------------------------------------


def _export_meta_writer(argv: list[str]) -> None:
    """Mimic export_read_models: write views/meta.json so local export is visible."""

    idx = argv.index("--output-dir")
    views = Path(argv[idx + 1]) / "views"
    views.mkdir(parents=True, exist_ok=True)
    (views / "meta.json").write_text("{}\n", encoding="utf-8")


def _notice_runner(
    results: dict[str, list[CommandResult]] | None = None,
) -> ScriptedRunner:
    return ScriptedRunner(
        results or _success_script(),
        writers={"screening run": _run_yaml_writer("rev-1"), "export": _export_meta_writer},
    )


def _load_notice(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_daily_batch_writes_the_notice_with_asof_and_an_unmeasured_delta(tmp_path: Path) -> None:
    runner = _notice_runner()
    notice_path = tmp_path / "notice.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=runner,
        notice_output=notice_path,
    )

    assert exit_code == 0
    # The delta view is absent in this fixture, so the notice says "not measured"
    # rather than empty lists that would read as "nothing changed".
    assert _load_notice(notice_path) == {
        "asof": "2026-07-21",
        "skipped": False,
        "failed_stage": None,
        "delta_measured": False,
        "delta_unmeasured_reason": "view_unreadable",
        "entered": [],
        "exited": [],
    }


def _delta_view(path: Path, entered: list[object], exited: list[object] | None = None) -> Path:
    payload = {
        "entered": entered,
        "exited": [] if exited is None else exited,
        "er_moves": [],
        "holdings": [],
        "macro_flags": [],
        "macro_extremes": [],
        "unavailable": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _entry(ticker: str, name: object, er: object) -> dict[str, object]:
    return {
        "ticker": ticker,
        "company_name": name,
        "er_annual_pct": er,
        "disclosed_since_previous": False,
    }


def test_daily_delta_names_entered_tickers_by_estimate_descending(
    tmp_path: Path,
) -> None:
    view = _delta_view(
        tmp_path / "daily-delta.json",
        [
            _entry("1001", "Alpha", 8.0),
            _entry("1002", "Bravo", 22.5),
            _entry("1003", "Charlie", 12.0),
            _entry("1004", "Delta", 30.0),
            _entry("1005", "Echo", 15.0),
            _entry("1006", "Foxtrot", 9.0),
        ],
    )

    notice = _Notice()
    _read_daily_delta(view, notice)

    assert notice.delta_measured is True
    # Capped at five names; the notifier says how many more there are.
    assert notice.entered == [
        "1004 Delta E[r]+30.0%",
        "1002 Bravo E[r]+22.5%",
        "1005 Echo E[r]+15.0%",
        "1003 Charlie E[r]+12.0%",
        "1006 Foxtrot E[r]+9.0%",
    ]


def test_daily_delta_names_a_ticker_whose_name_or_estimate_is_missing(
    tmp_path: Path,
) -> None:
    view = _delta_view(
        tmp_path / "daily-delta.json",
        [
            _entry("2001", None, 5.0),
            _entry("2002", "Named", None),
            _entry("2003", None, None),
        ],
    )

    # A partly-known row is still the pointer the reader needs, so the known fields
    # are reported and the unknown ones are left out. Rows without an estimate sort
    # last because the estimate is what ranks them.
    notice = _Notice()
    _read_daily_delta(view, notice)
    assert notice.entered == [
        "2001 E[r]+5.0%",
        "2002 Named",
        "2003",
    ]


def test_daily_delta_names_exited_tickers_from_the_other_side(tmp_path: Path) -> None:
    view = _delta_view(
        tmp_path / "daily-delta.json",
        [_entry("1001", "Alpha", 8.0)],
        [_entry("9001", "Zulu", 6.0), _entry("9002", "Yankee", 11.0)],
    )

    notice = _Notice()
    _read_daily_delta(view, notice)

    # A name leaving the pool is the same kind of fact as one entering it, ranked
    # the same way, and read from the row shape the two sides share.
    assert notice.exited == ["9002 Yankee E[r]+11.0%", "9001 Zulu E[r]+6.0%"]
    assert notice.entered == ["1001 Alpha E[r]+8.0%"]


def test_daily_delta_reports_an_empty_list_when_nothing_entered(tmp_path: Path) -> None:
    view = _delta_view(tmp_path / "daily-delta.json", [])

    notice = _Notice()
    _read_daily_delta(view, notice)

    # Measured and empty is what "no new name today" has to look like; the
    # notifier renders it as なし rather than as an unmeasured delta.
    assert notice.delta_measured is True
    assert notice.entered == []


def test_daily_delta_drops_unreadable_entered_rows_without_failing(
    tmp_path: Path,
) -> None:
    view = _delta_view(
        tmp_path / "daily-delta.json",
        [
            "not-a-row",
            _entry(" ", "Blank", 40.0),
            {"company_name": "No ticker", "er_annual_pct": 50.0},
            _entry("3001", "Broken", float("nan")),
            _entry("3002", "Boolish", True),
            _entry("3003", "Multi\nline {name}", 3.0),
            _entry("3004", "x" * 200, 2.0),
        ],
    )

    notice = _Notice()
    _read_daily_delta(view, notice)
    labels = notice.entered

    assert isinstance(labels, list)
    # A row that cannot be identified by ticker is dropped; a value the renderer
    # cannot use (NaN, bool) costs only that field, not the row.
    assert labels[:2] == ["3003 Multi line name E[r]+3.0%", "3004 " + "x" * 24 + " E[r]+2.0%"]
    assert sorted(labels[2:]) == ["3001 Broken", "3002 Boolish"]
    assert all("\n" not in label and len(label) <= 48 for label in labels)


def test_daily_delta_names_nothing_when_the_view_cannot_be_read(tmp_path: Path) -> None:
    notice = _Notice()
    _read_daily_delta(tmp_path / "missing.json", notice)

    assert notice.delta_measured is False
    assert notice.delta_unmeasured_reason == "view_unreadable"
    assert notice.entered == []


def test_daily_batch_writes_a_skipped_notice_on_a_non_business_day(tmp_path: Path) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: "0"})
    notice_path = tmp_path / "notice.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=None,
        runner=_notice_runner({}),
        notice_output=notice_path,
    )

    assert exit_code == 0
    notice = _load_notice(notice_path)
    assert notice["skipped"] is True
    assert notice["asof"] == today.isoformat()
    assert notice["failed_stage"] is None


def test_daily_batch_exits_3_and_names_the_first_deferred_macro_failure(
    tmp_path: Path,
) -> None:
    script = _success_script()
    script["macro refresh"] = [CommandResult(1, "", "provider down\n"), OK, OK]
    notice_path = tmp_path / "notice.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=_notice_runner(script),
        notice_output=notice_path,
    )

    assert exit_code == 3
    assert _load_notice(notice_path)["failed_stage"].startswith("macro-refresh-")


def test_daily_batch_names_the_failed_stage_in_the_notice_on_a_fatal_failure(
    tmp_path: Path,
) -> None:
    script = _success_script()
    script["screening run"] = [CommandResult(1, "", "boom\n")]
    notice_path = tmp_path / "notice.json"

    with pytest.raises(BatchStepError):
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=ASOF,
            runner=_notice_runner(script),
            notice_output=notice_path,
        )

    notice = _load_notice(notice_path)
    assert notice["failed_stage"] == "screening-run"
    # The stderr text stays in the log; the notice carries only the stage name.
    assert "boom" not in json.dumps(notice)


def test_daily_batch_names_the_calendar_stage_when_the_market_store_is_missing(
    tmp_path: Path,
) -> None:
    notice_path = tmp_path / "notice.json"

    with pytest.raises(CalendarCoverageError):
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=None,
            runner=_notice_runner({}),
            notice_output=notice_path,
        )

    assert _load_notice(notice_path)["failed_stage"] == "calendar"


def test_main_rejects_an_invalid_asof_before_writing_a_notice(tmp_path: Path, capsys) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "method").mkdir()
    notice_path = tmp_path / "notice.json"

    exit_code = main(
        [
            "--repo-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "serving"),
            "--asof",
            "2026-13-99",
            "--notice-output",
            str(notice_path),
        ]
    )

    assert exit_code == 1
    assert "not a valid YYYY-MM-DD date" in capsys.readouterr().err
    # No notice: the notifier reports the batch step's failure without one.
    assert not notice_path.exists()


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
    notice_path = tmp_path / "notice.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=runner,
        notice_output=notice_path,
    )

    keys = runner.call_keys()
    assert "screening select" in keys
    assert "export" in keys
    assert exit_code == 3
    assert _load_notice(notice_path)["failed_stage"] == "task-reconcile-earnings"
