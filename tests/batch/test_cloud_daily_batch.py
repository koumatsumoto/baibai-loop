from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from baibai_batch.jobs.daily import (
    BatchStepError,
    CommandResult,
    _economic_refresh_groups,
    _Notice,
    _parse_economic_sources,
    _read_daily_delta,
    _read_run_view,
    build_parser,
    main,
    run_daily_batch,
    run_daily_batch_structured,
)
from baibai_engine.collection.economic.acquisition import (
    DEFAULT_LATEST_LOOKBACK_DAYS,
    LATEST_FETCH_LOOKBACK_DAYS,
)
from baibai_engine.collection.economic.sources import load_sources
from baibai_engine.market.jquants_writer import store_jquants_market_calendar
from baibai_engine.market_context.indicators.definitions import load_definitions

JST = ZoneInfo("Asia/Tokyo")
ASOF = date(2026, 7, 21)

OK = CommandResult(0, "", "")
REFRESH_METRICS_OK = CommandResult(
    0,
    "EDINET extraction summary: selected=1 reused=1 parsed=0 "
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


def _economic_source_row(source_id: str) -> dict[str, object]:
    """One Collection list row with the catalog's complete source fields."""
    return asdict(load_sources()[source_id])


ECONOMIC_LIST_OK = CommandResult(
    0,
    json.dumps(
        [
            _economic_source_row("fed-h15"),
            _economic_source_row("jquants-trades-spec"),
            _economic_source_row("estat-cpi"),
            _economic_source_row("fred-a191rl1q225sbea"),
            _economic_source_row("tsr-bankruptcies"),
        ]
    ),
    "",
)


def _key(argv: list[str]) -> str:
    if "baibai_web.materialize" in argv:
        return "export"
    if argv[1:3] == ["collection", "economic"]:
        return " ".join(argv[1:4])
    return " ".join(argv[1:3])


def _run_yaml_writer(revision_id: str | None) -> Callable[[list[str]], None]:
    """Mimic `screening run --output-path`: write the YAML view the batch reads."""

    def _writer(argv: list[str]) -> None:
        idx = argv.index("--output-path")
        payload = {
            "schema_version": 1,
            "kind": "stock_analysis_run",
            "run_revision_id": revision_id,
            "asof_date": "2026-07-21",
            "universe_size": 3800,
            "stock_analyses": [
                {
                    "ticker": "2331",
                    "candidate": {
                        "common_eligible": True,
                        "reasons": [
                            {
                                "valuation_approach_id": "current-earnings-power",
                                "valuation_method_id": "synthetic-method",
                                "rank": 1,
                            }
                        ],
                        "normalized_per_3fy_sector_gap": None,
                    },
                },
                {
                    "ticker": "0001",
                    "candidate": {
                        "common_eligible": False,
                        "reasons": [],
                        "normalized_per_3fy_sector_gap": None,
                    },
                },
            ],
            "candidate_discovery": {
                "method": {
                    "method_id": "synthetic-method",
                    "method_hash": "a" * 64,
                    "nomination_depth": 20,
                },
                "diagnostics": {
                    "common_eligible_count": 1,
                    "candidate_count": 1,
                    "candidate_counts_by_approach": {
                        "current-earnings-power": 1,
                        "normalized-earnings-power": 0,
                        "asset-value": 0,
                        "reinvestment-value": 0,
                    },
                },
            },
        }
        Path(argv[idx + 1]).write_text(yaml.safe_dump(payload), encoding="utf-8")

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
        "screening bootstrap-cache": [OK],
        "screening refresh-edinet-metrics": [REFRESH_METRICS_OK],
        "screening run": [RUN_OK],
        "task reconcile-earnings": [OK],
        "screening refresh-edinet-facts": [OK],
        "collection economic list": [ECONOMIC_LIST_OK],
        "collection economic refresh": [OK, OK, OK],
        "export": [OK],
        "screening prune": [OK],
    }


def _runner(
    results: dict[str, list[CommandResult]] | None = None,
    *,
    run_revision_id: str | None = "rev-1",
) -> ScriptedRunner:
    return ScriptedRunner(
        _success_script() if results is None else results,
        writers={"screening run": _run_yaml_writer(run_revision_id)},
    )


def _seed_calendar(root: Path, rows: dict[date, str]) -> None:
    db = root / "stores/market/market.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    days = sorted(rows)
    store_jquants_market_calendar(
        db,
        [{"Date": day.isoformat(), "HolDiv": division} for day, division in rows.items()],
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
        "screening bootstrap-cache",
        "screening refresh-edinet-metrics",
        "screening verify-cache-coverage",
        "screening run",
        "screening refresh-edinet-facts",
        "collection economic list",
        "collection economic refresh",
        "collection economic refresh",
        "collection economic refresh",
        "export",
        "screening prune",
        "task reconcile-earnings",
    ]

    run_argv = _call(runner, "screening run")
    assert run_argv[:3] == ["baibai-engine", "screening", "run"]
    assert run_argv[3:5] == ["--asof", "2026-07-21"]
    assert "--output-path" in run_argv

    assert "screening review-set" not in runner.call_keys()

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


def test_structured_and_human_daily_paths_have_normalized_equivalent_results(
    tmp_path: Path,
) -> None:
    human = _runner()
    structured = _runner()

    human_exit = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "human",
        asof=ASOF,
        runner=human,
    )
    result = run_daily_batch_structured(
        root=tmp_path,
        output_dir=tmp_path / "structured",
        asof=ASOF,
        runner=structured,
        quiet=True,
    )

    assert result.exit_code == human_exit == 0
    assert result.status == "machine_complete"
    assert result.run_revision_id == "rev-1"
    assert result.candidate_count == 1
    assert human.call_keys() == structured.call_keys()
    assert [step.name for step in result.steps] == [
        "refresh-edinet-documents",
        "bootstrap-cache",
        "refresh-edinet-metrics",
        "verify-cache-coverage",
        "screening-run",
        "edinet-research-facts",
        "economic-source-list",
        "economic-source-refresh-14d",
        "economic-source-refresh-60d",
        "economic-source-refresh-370d",
        "export-read-models",
        "screening-prune",
        "task-reconcile-earnings",
    ]


def test_daily_batch_refreshes_sources_by_frequency_window(tmp_path: Path) -> None:
    runner = _runner()

    run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)

    refresh_calls = [argv for argv in runner.calls if _key(argv) == "collection economic refresh"]
    assert [argv[4:] for argv in refresh_calls] == [
        ["fed-h15", "--start", (ASOF - timedelta(days=14)).isoformat(), "--end", "2026-07-21"],
        [
            "jquants-trades-spec",
            "--start",
            (ASOF - timedelta(days=60)).isoformat(),
            "--end",
            "2026-07-21",
        ],
        [
            "estat-cpi",
            "fred-a191rl1q225sbea",
            "tsr-bankruptcies",
            "--start",
            (ASOF - timedelta(days=370)).isoformat(),
            "--end",
            "2026-07-21",
        ],
    ]


def test_daily_batch_defers_remaining_cache_gap_after_bootstrap(tmp_path: Path) -> None:
    script = _success_script()
    script["screening verify-cache-coverage"] = [
        CommandResult(1, "SQLite cache coverage incomplete\n", "missing input"),
    ]
    runner = _runner(script)
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert "screening bootstrap-cache" in runner.call_keys()
    assert "screening run" not in runner.call_keys()
    assert runner.call_keys().count("collection economic list") == 1
    assert runner.call_keys().count("export") == 1
    assert "--skip-history" in _call(runner, "export")


def test_daily_batch_bootstraps_cache_when_coverage_is_complete(tmp_path: Path) -> None:
    runner = _runner()

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 0
    assert runner.call_keys()[:5] == [
        "screening refresh-edinet-documents",
        "screening bootstrap-cache",
        "screening refresh-edinet-metrics",
        "screening verify-cache-coverage",
        "screening run",
    ]


def test_daily_batch_defers_metric_refresh_without_quarantine_counters(
    tmp_path: Path, capsys
) -> None:
    script = _success_script()
    script["screening refresh-edinet-metrics"] = [OK]
    runner = _runner(script)
    assert (
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)
        == 3
    )
    assert "without quarantine counters" in capsys.readouterr().err
    assert "screening run" not in runner.call_keys()
    assert "collection economic refresh" in runner.call_keys()
    assert "export" in runner.call_keys()


def test_daily_batch_defers_verifier_crash_without_publishing_screening(tmp_path: Path) -> None:
    script = _success_script()
    script["screening verify-cache-coverage"] = [CommandResult(1, "", "broken rules")]
    runner = _runner(script)
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert "screening run" not in runner.call_keys()
    assert "collection economic refresh" in runner.call_keys()
    assert "export" in runner.call_keys()


def test_daily_batch_acquires_shared_market_after_edinet_failure(tmp_path: Path) -> None:
    script = _success_script()
    script["screening refresh-edinet-documents"] = [CommandResult(1, "", "EDINET unavailable")]
    runner = _runner(script)
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )
    assert result.exit_code == 3
    assert "screening bootstrap-cache" in runner.call_keys()
    assert "screening run" not in runner.call_keys()
    assert runner.call_keys().count("export") == 1


def test_daily_batch_continues_economic_sources_after_screening_fatal(
    tmp_path: Path, capsys
) -> None:
    script = _success_script()
    script["screening run"] = [CommandResult(1, "", "boom\nprovider unavailable\n")]
    runner = _runner(script)
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert result.run_revision_id is None
    assert result.candidate_count is None
    error = capsys.readouterr().err
    assert "provider unavailable" in error
    assert "read models exported locally" in error
    assert "export published" not in error
    assert runner.call_keys().count("collection economic list") == 1
    assert runner.call_keys().count("export") == 1
    assert "screening prune" not in runner.call_keys()
    assert "--skip-history" in _call(runner, "export")


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


def test_daily_batch_defers_economic_source_refresh_failure_until_after_export(
    tmp_path: Path, capsys
) -> None:
    script = _success_script()
    script["collection economic refresh"] = [
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
    assert keys.count("collection economic refresh") == 3
    assert "export" in keys
    # The deferred detail is surfaced immediately, not only in the final summary.
    assert "deferred failure" in capsys.readouterr().err


def test_daily_batch_defers_economic_source_list_failure_and_still_exports(tmp_path: Path) -> None:
    script = _success_script()
    script["collection economic list"] = [CommandResult(1, "", "boom\n")]
    runner = _runner(script)

    exit_code = run_daily_batch(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )

    assert exit_code == 3
    keys = runner.call_keys()
    assert "collection economic refresh" not in keys
    assert "export" in keys


def test_daily_batch_defers_run_view_without_revision(tmp_path: Path) -> None:
    runner = _runner(run_revision_id=None)
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert "collection economic list" in runner.call_keys()
    assert "--skip-history" in _call(runner, "export")


def test_daily_batch_export_failure_remains_fatal_after_economic_sources(tmp_path: Path) -> None:
    script = _success_script()
    script["export"] = [CommandResult(1, "", "untrusted store")]
    runner = _runner(script)
    with pytest.raises(BatchStepError, match="export-read-models"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)
    assert "collection economic refresh" in runner.call_keys()
    assert "screening prune" not in runner.call_keys()


def test_daily_batch_skips_only_screening_on_non_business_day(tmp_path: Path, capsys) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: "0"})
    runner = _runner()
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )
    assert result.exit_code == 0
    assert result.screening_status == "skipped_non_business_day"
    assert result.market_updated is False
    assert "screening bootstrap-cache" not in runner.call_keys()
    assert "screening run" not in runner.call_keys()
    assert runner.call_keys().count("collection economic list") == 1
    assert runner.call_keys().count("export") == 1
    assert "--skip-history" in _call(runner, "export")
    assert f"skip Screening: {today.isoformat()} は非営業日" in capsys.readouterr().out


def test_analysis_structured_path_gates_an_explicit_non_business_day(tmp_path: Path) -> None:
    _seed_calendar(tmp_path, {ASOF: "0"})
    runner = _runner()

    result = run_daily_batch_structured(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=runner,
        gate_explicit_asof=True,
        quiet=True,
    )

    assert result.screening_status == "skipped_non_business_day"
    assert result.exit_code == 0
    assert "screening run" not in runner.call_keys()
    assert "collection economic refresh" in runner.call_keys()


@pytest.mark.parametrize("scheduled", [False, True])
def test_saved_input_integration_gates_workflow_resolved_weekend(
    tmp_path: Path, scheduled: bool
) -> None:
    target = date(2026, 10, 24)
    _seed_calendar(tmp_path, {target: "0"})
    runner = _runner()
    args = build_parser().parse_args(
        ["--saved-inputs", "--asof", target.isoformat()]
        + (["--scheduled"] if scheduled else [])
    )
    result = run_daily_batch_structured(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=date.fromisoformat(args.asof),
        scheduled=args.scheduled,
        acquire_market=not args.saved_inputs,
        refresh_economic=not args.saved_inputs,
        runner=runner,
    )
    assert result.exit_code == 0
    assert result.screening_status == "skipped_non_business_day"
    assert "screening run" not in runner.call_keys()
    assert "screening verify-cache-coverage" not in runner.call_keys()
    assert "export" in runner.call_keys()
    assert "--skip-history" in _call(runner, "export")
    assert "2026-10-23" not in _call(runner, "export")


def test_saved_input_historical_business_day_still_runs_screening(tmp_path: Path) -> None:
    _seed_calendar(tmp_path, {ASOF: "1"})
    runner = _runner()
    result = run_daily_batch_structured(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        acquire_market=False,
        refresh_economic=False,
        runner=runner,
    )
    assert result.screening_status == "published"
    assert "screening verify-cache-coverage" in runner.call_keys()
    assert "screening run" in runner.call_keys()
    assert _call(runner, "screening run")[3:5] == ["--asof", ASOF.isoformat()]


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


def test_scheduled_batch_uses_cron_target_and_keeps_business_day_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled_target = date(2026, 8, 27)
    _seed_calendar(tmp_path, {scheduled_target: "1"})
    monkeypatch.setattr(
        "baibai_batch.jobs.daily.batch_target_date", lambda _instant: scheduled_target
    )
    runner = _runner()

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=None,
        runner=runner,
        scheduled=True,
    )

    assert exit_code == 0
    assert runner.calls[0][3:] == ["--asof", "2026-08-27"]


def test_scheduled_gate_accepts_a_fixed_integration_target() -> None:
    args = build_parser().parse_args(
        ["--scheduled", "--asof", "2026-08-27", "--output-dir", "/tmp/serving"]
    )
    assert args.scheduled is True
    assert args.asof == "2026-08-27"


def test_daily_batch_errors_when_calendar_does_not_cover_the_date(tmp_path: Path, capsys) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today - timedelta(days=30): "1"})
    runner = _runner()
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert "does not cover" in capsys.readouterr().err
    assert "screening run" not in runner.call_keys()
    assert "collection economic refresh" in runner.call_keys()
    assert "--skip-history" in _call(runner, "export")


def test_daily_batch_errors_when_calendar_table_is_missing(tmp_path: Path, capsys) -> None:
    import sqlite3

    db = tmp_path / "stores/market/market.sqlite"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    runner = _runner()
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert "unreadable" in capsys.readouterr().err
    assert "screening run" not in runner.call_keys()
    assert "collection economic refresh" in runner.call_keys()
    assert "--skip-history" in _call(runner, "export")


def test_daily_batch_errors_when_market_sqlite_is_absent(tmp_path: Path, capsys) -> None:
    runner = _runner()
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert "does not exist" in capsys.readouterr().err
    assert "screening run" not in runner.call_keys()
    assert "collection economic refresh" in runner.call_keys()
    assert "--skip-history" in _call(runner, "export")


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
        _success_script() if results is None else results,
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
        "screening_status": "published",
        "market_updated": None,
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
        "holdings": [],
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


def test_daily_delta_keeps_all_entered_tickers_in_view_order(
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
    # Only the final renderer caps names; no entry is lost before then.
    assert notice.entered == [
        "1001 Alpha E[r]+8.0%",
        "1002 Bravo E[r]+22.5%",
        "1003 Charlie E[r]+12.0%",
        "1004 Delta E[r]+30.0%",
        "1005 Echo E[r]+15.0%",
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
    # are reported and the unknown ones are left out without changing order.
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

    # Both sides preserve their incoming order.
    assert notice.exited == ["9001 Zulu E[r]+6.0%", "9002 Yankee E[r]+11.0%"]
    assert notice.entered == ["1001 Alpha E[r]+8.0%"]


def test_daily_delta_reports_an_empty_list_when_nothing_entered(tmp_path: Path) -> None:
    view = _delta_view(tmp_path / "daily-delta.json", [])

    notice = _Notice()
    _read_daily_delta(view, notice)

    # Measured and empty is what "no new name today" has to look like; the
    # notifier renders it as なし rather than as an unmeasured delta.
    assert notice.delta_measured is True
    assert notice.entered == []


def test_daily_delta_keeps_identified_rows_with_unusable_optional_fields(
    tmp_path: Path,
) -> None:
    view = _delta_view(
        tmp_path / "daily-delta.json",
        [
            _entry("3001", "Broken", float("nan")),
            _entry("3002", "Boolish", True),
            _entry("3003", "Multi\nline {name}", 3.0),
            _entry("3004", "x" * 200, 2.0),
            _entry("3005", "Overflow", 10**400),
        ],
    )

    notice = _Notice()
    _read_daily_delta(view, notice)
    labels = notice.entered

    assert isinstance(labels, list)
    assert notice.delta_measured
    # An unusable optional value costs only that field, not an identified row.
    assert labels[2:] == [
        "3003 Multi line name E[r]+3.0%",
        "3004 " + "x" * 24 + " E[r]+2.0%",
        "3005 Overflow",
    ]
    assert labels[:2] == ["3001 Broken", "3002 Boolish"]
    assert all("\n" not in label and len(label) <= 48 for label in labels)


@pytest.mark.parametrize("content", [None, b"not json", b"\xff"])
def test_daily_delta_names_nothing_when_the_view_cannot_be_read(tmp_path: Path, content) -> None:
    path = tmp_path / "delta.json"
    if content is not None:
        path.write_bytes(content)
    notice = _Notice()
    _read_daily_delta(path, notice)

    assert notice.delta_measured is False
    assert notice.delta_unmeasured_reason == "view_unreadable"
    assert notice.entered == []


def test_daily_batch_reports_screening_skip_without_skipping_economic_sources(
    tmp_path: Path,
) -> None:
    today = datetime.now(JST).date()
    _seed_calendar(tmp_path, {today: "0"})
    notice_path = tmp_path / "notice.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=None,
        runner=_notice_runner(),
        notice_output=notice_path,
    )

    assert exit_code == 0
    notice = _load_notice(notice_path)
    assert notice["screening_status"] == "skipped_non_business_day"
    assert notice["asof"] == today.isoformat()
    assert notice["failed_stage"] is None


def test_daily_batch_exits_3_and_names_the_first_deferred_macro_failure(
    tmp_path: Path,
) -> None:
    script = _success_script()
    script["collection economic refresh"] = [CommandResult(1, "", "provider down\n"), OK, OK]
    notice_path = tmp_path / "notice.json"

    exit_code = run_daily_batch(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=_notice_runner(script),
        notice_output=notice_path,
    )

    assert exit_code == 3
    assert _load_notice(notice_path)["failed_stage"].startswith("economic-source-refresh-")


def test_daily_batch_names_screening_failure_without_claiming_fresh_result(tmp_path: Path) -> None:
    script = _success_script()
    script["screening run"] = [CommandResult(1, "", "boom\n")]
    notice_path = tmp_path / "notice.json"
    assert (
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=ASOF,
            runner=_notice_runner(script),
            notice_output=notice_path,
        )
        == 3
    )
    notice = _load_notice(notice_path)
    assert notice["failed_stage"] == "screening-run"
    assert notice["screening_status"] == "failed"
    assert notice["delta_measured"] is False
    assert "boom" not in json.dumps(notice)


def test_daily_batch_names_missing_calendar_and_still_exports_economic_sources(
    tmp_path: Path,
) -> None:
    notice_path = tmp_path / "notice.json"
    assert (
        run_daily_batch(
            root=tmp_path,
            output_dir=tmp_path / "serving",
            asof=None,
            runner=_notice_runner(),
            notice_output=notice_path,
        )
        == 3
    )
    assert _load_notice(notice_path)["failed_stage"] == "calendar"
    assert _load_notice(notice_path)["screening_status"] == "failed"
    assert (tmp_path / "serving/views/meta.json").is_file()


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


def test_parse_economic_sources_reads_json_list() -> None:
    stdout = json.dumps(
        [
            _economic_source_row("fed-h15"),
            _economic_source_row("spglobal-jp-pmi-mfg"),
        ]
    )

    parsed = _parse_economic_sources(stdout)

    assert [item.source_id for item in parsed] == ["fed-h15", "spglobal-jp-pmi-mfg"]


def test_parse_economic_sources_rejects_non_json() -> None:
    with pytest.raises(BatchStepError, match="not parseable JSON"):
        _parse_economic_sources("fed-h15\tdaily\tfrb_h15")


def test_economic_refresh_groups_buckets_every_source_by_frequency_window() -> None:
    parsed = _parse_economic_sources(
        json.dumps(
            [
                _economic_source_row("fed-h15"),
                _economic_source_row("jquants-trades-spec"),
                _economic_source_row("spglobal-jp-pmi-mfg"),
                _economic_source_row("estat-cpi"),
            ]
        )
    )

    groups = _economic_refresh_groups(parsed)

    assert groups == [
        (LATEST_FETCH_LOOKBACK_DAYS["daily"], ["fed-h15"]),
        (LATEST_FETCH_LOOKBACK_DAYS["weekly"], ["jquants-trades-spec"]),
        (DEFAULT_LATEST_LOOKBACK_DAYS, ["spglobal-jp-pmi-mfg", "estat-cpi"]),
    ]


def test_collection_catalog_has_no_acquisition_entry_for_derived_l2_series() -> None:
    sources = load_sources()
    derived = [item for item in load_definitions().series if item.provider == "derived"]
    assert derived
    assert all(item.source_id not in sources for item in derived)
    assert all(source.provider != "derived" for source in sources.values())


def test_a_reconcile_failure_degrades_the_batch_after_local_export(
    tmp_path: Path,
) -> None:
    """A reconciliation failure is deferred after a successful local export.

    The scripted runner tests orchestration and notice state, not remote publication.
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
    assert "screening run" in keys
    assert "export" in keys
    assert exit_code == 3
    assert _load_notice(notice_path)["failed_stage"] == "task-reconcile-earnings"


@pytest.mark.parametrize("section", ["screening_run", "previous_screening_run", "candidates"])
def test_daily_delta_does_not_report_missing_comparison_as_empty(tmp_path, section):
    view = _delta_view(tmp_path / "delta.json", [])
    payload = json.loads(view.read_text(encoding="utf-8"))
    payload["unavailable"] = [section]
    view.write_text(json.dumps(payload), encoding="utf-8")
    notice = _Notice()
    _read_daily_delta(view, notice)
    assert not notice.delta_measured
    assert notice.delta_unmeasured_reason == section


def test_daily_delta_distinguishes_method_change(tmp_path):
    view = _delta_view(tmp_path / "delta.json", [])
    payload = json.loads(view.read_text(encoding="utf-8"))
    payload["method_changed"] = True
    view.write_text(json.dumps(payload), encoding="utf-8")
    notice = _Notice()
    _read_daily_delta(view, notice)
    assert not notice.delta_measured
    assert notice.delta_unmeasured_reason == "手法変更"


def test_research_fact_failure_is_deferred_until_after_local_export(tmp_path):
    script = _success_script()
    script["screening refresh-edinet-facts"] = [CommandResult(1, "", "XBRL unavailable")]
    runner = _runner(script)
    assert (
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner)
        == 3
    )
    assert "screening run" in runner.call_keys()
    assert "export" in runner.call_keys()
    assert "--initialize" not in _call(runner, "screening refresh-edinet-facts")


def test_committed_market_chunk_survives_bootstrap_failure_and_requests_lake_publish(
    tmp_path: Path,
) -> None:
    import sqlite3

    _seed_calendar(tmp_path, {ASOF: "1"})
    script = _success_script()
    script["screening bootstrap-cache"] = [CommandResult(1, "", "later source chunk failed")]
    runner = _notice_runner(script)
    market = tmp_path / "stores/market/market.sqlite"
    next_day = ASOF + timedelta(days=1)
    runner._writers["screening bootstrap-cache"] = lambda _argv: store_jquants_market_calendar(
        market,
        [{"Date": next_day.isoformat(), "HolDiv": "1"}],
        requested_start=next_day,
        requested_end=next_day,
    )
    notice_path = tmp_path / "notice.json"
    result = run_daily_batch_structured(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=runner,
        notice_output=notice_path,
    )
    assert result.exit_code == 3
    assert result.market_updated is True
    assert result.screening_status == "failed"
    assert _load_notice(notice_path)["market_updated"] is True
    with closing(sqlite3.connect(market)) as connection:
        assert connection.execute(
            "SELECT holiday_division IN ('1', '2') FROM jquants_market_calendar WHERE day = ?",
            (next_day.isoformat(),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT holiday_division IN ('1', '2') FROM jquants_market_calendar WHERE day = ?",
            (ASOF.isoformat(),),
        ).fetchone() == (1,)
    assert "screening run" not in runner.call_keys()
    assert (
        runner.call_keys().count("collection economic list")
        == runner.call_keys().count("export")
        == 1
    )
    assert (tmp_path / "serving/views/meta.json").is_file()


def test_corrupt_market_store_does_not_block_economic_sources_and_export_stays_fatal(
    tmp_path: Path,
) -> None:
    market = tmp_path / "stores/market/market.sqlite"
    market.parent.mkdir(parents=True)
    market.write_bytes(b"injected corrupt sqlite")
    script = _success_script()
    script["export"] = [CommandResult(1, "", "market store schema/hydration invalid")]
    runner = _runner(script)
    with pytest.raises(BatchStepError, match="export-read-models"):
        run_daily_batch(root=tmp_path, output_dir=tmp_path / "serving", asof=None, runner=runner)
    assert "screening run" not in runner.call_keys()
    assert "collection economic refresh" in runner.call_keys()
    assert not (tmp_path / "serving/views/meta.json").exists()


def test_daily_output_uses_single_run_export_and_zero_candidates_is_published(
    tmp_path: Path,
) -> None:
    runner = _runner()
    original_writer = _run_yaml_writer("rev-1")

    def empty_candidates(argv: list[str]) -> None:
        original_writer(argv)
        output = Path(argv[argv.index("--output-path") + 1])
        payload = yaml.safe_load(output.read_text(encoding="utf-8"))
        payload["stock_analyses"][0]["candidate"]["reasons"] = []
        payload["candidate_discovery"]["diagnostics"]["candidate_count"] = 0
        payload["candidate_discovery"]["diagnostics"]["candidate_counts_by_approach"][
            "current-earnings-power"
        ] = 0
        output.write_text(yaml.safe_dump(payload), encoding="utf-8")

    runner._writers["screening run"] = empty_candidates
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )
    assert result.exit_code == 0
    assert result.screening_status == "published"
    assert result.candidate_count == 0
    assert result.run_revision_id == "rev-1"
    assert "review_set_id" not in result.to_json()
    assert "screening prune" in runner.call_keys()


@pytest.mark.parametrize("mutation", ["header", "rows", "context", "counts", "method", "universe"])
def test_daily_rejects_invalid_committed_run_export_and_keeps_economic_sources_independent(
    tmp_path: Path, mutation: str
) -> None:
    runner = _runner()
    original_writer = _run_yaml_writer("rev-1")

    def broken_export(argv: list[str]) -> None:
        original_writer(argv)
        output = Path(argv[argv.index("--output-path") + 1])
        payload = yaml.safe_load(output.read_text(encoding="utf-8"))
        if mutation == "header":
            payload.pop("candidate_discovery")
        elif mutation == "rows":
            payload["stock_analyses"] = None
        elif mutation == "context":
            payload["stock_analyses"][0]["candidate"] = None
        elif mutation == "counts":
            payload["candidate_discovery"]["diagnostics"]["candidate_count"] = 0
        elif mutation == "method":
            payload["candidate_discovery"]["method"]["method_hash"] = "invalid"
        else:
            payload["universe_size"] = True
        output.write_text(yaml.safe_dump(payload), encoding="utf-8")

    runner._writers["screening run"] = broken_export
    result = run_daily_batch_structured(
        root=tmp_path, output_dir=tmp_path / "serving", asof=ASOF, runner=runner
    )
    assert result.exit_code == 3
    assert result.screening_status == "failed"
    assert result.candidate_count is None
    assert "collection economic refresh" in runner.call_keys()
    assert "--skip-history" in _call(runner, "export")
    assert "screening prune" not in runner.call_keys()


def test_run_export_counts_match_stock_rows_without_review_publication_id(tmp_path: Path) -> None:
    output = tmp_path / "run.yaml"
    _run_yaml_writer("rev-1")(["--output-path", str(output)])
    view = _read_run_view(output)
    assert view.run_revision_id == "rev-1"
    assert view.universe_size == 3800
    assert view.analyzed_count == 2
    assert view.candidate_count == 1


@pytest.mark.parametrize("failure", ["connect", "before", "after"])
def test_unreadable_change_probe_does_not_stop_batch_or_claim_no_change(
    tmp_path, monkeypatch, failure
):
    import sqlite3

    market = tmp_path / "stores/market/market.sqlite"
    market.parent.mkdir(parents=True)
    market.touch()
    reads = []
    closed = []

    class Probe:
        def execute(self, query):
            assert query == "PRAGMA data_version"
            reads.append(query)
            if failure == "before" or (failure == "after" and len(reads) == 2):
                raise sqlite3.OperationalError("probe unavailable")
            return self

        def fetchone(self):
            return (1,)

        def close(self):
            closed.append(True)

    def connect(*args, **kwargs):
        if failure == "connect":
            raise sqlite3.OperationalError("probe unavailable")
        return Probe()

    monkeypatch.setattr("baibai_batch.jobs.daily.sqlite3.connect", connect)
    runner = _notice_runner()
    notice_path = tmp_path / "notice.json"
    result = run_daily_batch_structured(
        root=tmp_path,
        output_dir=tmp_path / "serving",
        asof=ASOF,
        runner=runner,
        notice_output=notice_path,
    )
    assert result.exit_code == 0
    assert result.market_updated is None
    assert _load_notice(notice_path)["market_updated"] is None
    assert "collection economic refresh" in runner.call_keys()
    assert "export" in runner.call_keys()
    assert closed == ([] if failure == "connect" else [True])


@pytest.mark.parametrize("section", ["entered", "exited"])
@pytest.mark.parametrize("row", ["not-a-row", {"ticker": " "}, {"company_name": "No ticker"}])
@pytest.mark.parametrize("mixed", [False, True])
def test_unidentified_delta_row_is_not_reported_as_no_change_or_a_complete_list(
    tmp_path, section, row, mixed
):
    path = _delta_view(tmp_path / "delta.json", [])
    payload = json.loads(path.read_text())
    payload[section] = [*([_entry("3001", "Known", 5.0)] if mixed else []), row]
    path.write_text(json.dumps(payload))
    notice = _Notice()
    _read_daily_delta(path, notice)
    assert not notice.delta_measured
    assert notice.delta_unmeasured_reason == "view_unreadable"
    assert notice.entered == notice.exited == []


def test_saved_economic_mode_does_not_reacquire_any_economic_source(tmp_path):
    runner = _runner()
    result = run_daily_batch_structured(
        root=tmp_path,
        output_dir=tmp_path / "out",
        asof=ASOF,
        runner=runner,
        refresh_economic=False,
    )
    assert result.exit_code == 0
    assert not any("collection economic" in key for key in runner.call_keys())
