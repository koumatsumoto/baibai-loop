"""CLI seam coverage for the screening analysis subcommands.

Every test here drives ``baibai_engine.screening.cli.main`` with the argv a human
types, so argparse's own ``type=``/``action=`` conversion and the dispatch that turns
those strings into command arguments are inside what is exercised. A test that builds
the command's arguments itself skips exactly that layer, so it stays green while the
typed command line is unusable.

Each test asserts the command's effect rather than its console text: the run-store
row, the application-DB row, or the file the command was told to write. Exit code 0
alone does not distinguish a command that ran from one that produced nothing.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from baibai_engine.read_api import list_shortlist_payloads
from baibai_engine.screening.cli import main as screening_main
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.screening.sqlite_cache import open_connection
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

# The market store a panel can actually be built from: master snapshot, financial
# summaries and bars, seeded the way the calibration tests already seed it.
from tests.test_calibration_panel import ASOF as PANEL_ASOF
from tests.test_calibration_panel import _build_fixture_sqlite

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "method/screening-rules/2026-07-06T000000+0900.yaml"

RUN_REVISION_ID = "run-revision-cli-seam"
RUN_ASOF = date(2026, 7, 8)
SHORTLIST_ID = "shortlist-20260708-cli-seam"

# The month-end grid reads a trading day off the bar store's own breadth, so a store
# that only holds the two fixture names has no trading days at all. This is the
# following month, which the grid needs in order to call the target month complete.
NEXT_MONTH_END = date(2026, 7, 31)


def _narrative() -> dict[str, str]:
    return {
        "ploss": "中低",
        "why": "一時的な受注端境で売られている",
        "temporary": "翌期受注残は積み上がる",
        "structural": "構造的な需要毀損はない",
        "survive": "net cashで5年耐える",
        "unlock": "還元強化の余地",
        "upside": "受注が平年並みなら正常利益ベースでPER12倍相当",
        "downside": "受注半減でも営業黒字を保ち簿価が床になる",
        "rr": "下値が資産で支えられ上値は倍近い",
        "catalyst": "2Q決算で受注残の回復を確認する",
        "macro": "connectionのsizing cautionは該当なし",
        "counter": "受注が構造鈍化する可能性",
        "research": "受注残と粗利率を一次IRで確認",
        "value": "FV乖離が大きい",
        "prov": "深掘り最優先",
    }


def _candidate(ticker: str, *, name: str, sector: str, er_annual: float) -> dict[str, object]:
    return {
        "ticker": ticker,
        "name": name,
        "sector_33": sector,
        "market_cap_oku": 300,
        "avg_turnover_oku": 2.0,
        "listing_span_days": 1200,
        "jpx_flags": [],
        "metrics": {"er_annual": er_annual},
        "evidence_hits": [
            {
                "name": "valuation-reversion",
                "playbook_id": "cashflow-yield-discount",
                "source_status": "ok",
                "sizing_eligible": True,
            }
        ],
    }


def _publish_run(runs_db: Path) -> None:
    """Seed the immutable run the selection is drawn from."""
    ScreeningRunStore(runs_db).publish_run(
        {
            "run_id": "screening-20260708",
            "run_date": RUN_ASOF.isoformat(),
            "asof_date": RUN_ASOF.isoformat(),
            "run_at": f"{RUN_ASOF.isoformat()}T18:00:00+09:00",
            "universe_size": 2,
            "rules_ref": str(RULES_PATH),
            "candidates": [
                _candidate("1111", name="seam candidate", sector="機械", er_annual=0.12),
                _candidate("2222", name="seam alternate", sector="サービス業", er_annual=0.04),
            ],
        },
        run_revision_id=RUN_REVISION_ID,
    )


def _select_argv(*, runs_db: Path, market_sqlite: Path, output_path: Path) -> list[str]:
    return [
        "select",
        "--asof",
        RUN_ASOF.isoformat(),
        "--run-revision-id",
        RUN_REVISION_ID,
        "--runs-db",
        str(runs_db),
        "--top",
        "2",
        "--longlist-top",
        "2",
        "--rules-path",
        str(RULES_PATH),
        "--sqlite-path",
        str(market_sqlite),
        "--output-path",
        str(output_path),
    ]


def _publish_selection(runs_db: Path, tmp_path: Path) -> dict[str, object]:
    """Publish a selection through the CLI so the shortlist commands have their input."""
    _publish_run(runs_db)
    output_path = tmp_path / "selection-setup.yaml"
    assert (
        screening_main(
            _select_argv(
                runs_db=runs_db,
                market_sqlite=tmp_path / "absent-market.sqlite",
                output_path=output_path,
            )
        )
        == 0
    )
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_shortlist_draft(path: Path, selection: dict[str, object]) -> None:
    block = selection["selection"]
    assert isinstance(block, dict)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "kind": "shortlist",
                "shortlist_id": SHORTLIST_ID,
                "selection_id": selection["selection_id"],
                "run_revision_id": RUN_REVISION_ID,
                "as_of": RUN_ASOF.isoformat(),
                "published_at": f"{RUN_ASOF.isoformat()}T15:00:00+09:00",
                "profile": block["profile"],
                "macro_context_id": None,
                "entries": [
                    {
                        "ticker": "1111",
                        "decision": "selected",
                        "rank": 1,
                        "reason": "一次IRへ進める",
                        "narrative": _narrative(),
                    },
                    {
                        "ticker": "2222",
                        "decision": "rejected",
                        "reason": "根拠が弱い",
                        "reject_class": "other",
                    },
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def test_select_cli_publishes_the_selection_into_the_run_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_db = tmp_path / "runs.sqlite"
    _publish_run(runs_db)
    output_path = tmp_path / "selection.yaml"

    code = screening_main(
        _select_argv(
            runs_db=runs_db,
            market_sqlite=tmp_path / "absent-market.sqlite",
            output_path=output_path,
        )
    )
    capsys.readouterr()

    assert code == 0
    emitted = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    published = ScreeningRunReader(runs_db).get_selection(str(emitted["selection_id"]))
    assert published is not None
    assert published.run_revision_id == RUN_REVISION_ID
    assert published.as_of_date == RUN_ASOF.isoformat()
    # --top and --longlist-top go through the parser's own int conversion; a string
    # reaching the store would slice nothing and leave both blocks empty.
    recommendations = published.payload["recommendations"]
    longlist = published.payload["longlist"]
    assert isinstance(recommendations, list)
    assert isinstance(longlist, list)
    assert 1 <= len(recommendations) <= 2
    assert 1 <= len(longlist) <= 2


def test_shortlist_publish_cli_writes_the_judgment_into_the_application_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_db = tmp_path / "runs.sqlite"
    app_db = tmp_path / "app.sqlite"
    selection = _publish_selection(runs_db, tmp_path)
    draft = tmp_path / "shortlist-draft.yaml"
    _write_shortlist_draft(draft, selection)

    code = screening_main(
        ["shortlist", "publish", str(draft), "--db", str(app_db), "--runs-db", str(runs_db)]
    )
    capsys.readouterr()

    assert code == 0
    stored = [
        item for item in list_shortlist_payloads(app_db) if item["shortlist_id"] == SHORTLIST_ID
    ]
    assert len(stored) == 1
    assert stored[0]["selection_id"] == selection["selection_id"]
    entries = stored[0]["entries"]
    assert isinstance(entries, list)
    assert {str(entry["ticker"]) for entry in entries} == {"1111", "2222"}


def test_shortlist_outcome_cli_writes_the_cohort_comparison_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs_db = tmp_path / "runs.sqlite"
    app_db = tmp_path / "app.sqlite"
    selection = _publish_selection(runs_db, tmp_path)
    draft = tmp_path / "shortlist-outcome-draft.yaml"
    _write_shortlist_draft(draft, selection)
    assert (
        screening_main(
            ["shortlist", "publish", str(draft), "--db", str(app_db), "--runs-db", str(runs_db)]
        )
        == 0
    )
    # Prices only up to a fixed past day, so the observed window the payload reports is
    # the same on every run rather than following the wall clock.
    market_sqlite = tmp_path / "market.sqlite"
    observed_end = RUN_ASOF + timedelta(days=23)
    for ticker in ("1111", "2222"):
        insert_daily_bars_from_closes(
            market_sqlite, ticker, [1000.0] * 30, end_date=observed_end, turnover_value=2e8
        )
    out_path = tmp_path / "outcome.yaml"

    code = screening_main(
        [
            "shortlist",
            "outcome",
            "--db",
            str(app_db),
            "--runs-db",
            str(runs_db),
            "--sqlite-path",
            str(market_sqlite),
            "--horizon",
            "3m",
            "--out",
            str(out_path),
        ]
    )
    capsys.readouterr()

    assert code == 0
    payload = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "shortlist-judgment-outcome"
    assert payload["horizons"] == ["3m"]
    assert payload["cohort_count"] == 1
    assert payload["judgment_count"] == 2
    result = payload["results"][0]
    assert result["shortlist_id"] == SHORTLIST_ID
    # The drawdown window comes out of --sqlite-path; a store the command never opened
    # leaves the block out of the payload entirely.
    assert result["drawdown_window_end"] == observed_end.isoformat()


@pytest.fixture(scope="module")
def panel_market_sqlite(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A market store whose breadth lets the month-end grid resolve a cohort date."""
    sqlite_path = tmp_path_factory.mktemp("panel-market") / "market.sqlite"
    _build_fixture_sqlite(sqlite_path)
    breadth = [
        (f"{3000 + index:04d}", day.isoformat(), 100.0, 100.0)
        for day in (PANEL_ASOF, NEXT_MONTH_END)
        for index in range(2100)
    ]
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars("
            "ticker, traded_at, close, adjustment_close) VALUES (?, ?, ?, ?)",
            breadth,
        )
        conn.commit()
    finally:
        conn.close()
    return sqlite_path


def _calibration_build_argv(*, sqlite_path: Path, calibration_dir: Path) -> list[str]:
    return [
        "calibration-build",
        "--start",
        PANEL_ASOF.replace(day=1).isoformat(),
        "--end",
        PANEL_ASOF.isoformat(),
        "--sqlite-path",
        str(sqlite_path),
        "--calibration-dir",
        str(calibration_dir),
        "--rules-path",
        str(RULES_PATH),
        "--panel-variant",
        "production",
    ]


def test_calibration_build_cli_writes_the_panel_and_forward_store(
    panel_market_sqlite: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calibration_dir = tmp_path / "calibration"

    code = screening_main(
        _calibration_build_argv(sqlite_path=panel_market_sqlite, calibration_dir=calibration_dir)
    )
    captured = capsys.readouterr()

    assert code == 0
    panel = calibration_dir / f"panel-{PANEL_ASOF.isoformat()}.csv"
    meta = calibration_dir / f"panel-{PANEL_ASOF.isoformat()}.meta.yaml"
    forward = calibration_dir / f"forward-{PANEL_ASOF.isoformat()}.csv"
    assert panel.is_file()
    assert meta.is_file()
    assert forward.is_file()
    # The panel holds the fixture's own names, so an empty grid or an unread store
    # cannot pass as a build.
    assert "9001" in panel.read_text(encoding="utf-8")
    assert yaml.safe_load(meta.read_text(encoding="utf-8"))["panel_variant"] == "production"
    assert "panels built=1" in captured.out


def test_calibration_evaluate_cli_writes_the_evaluation_yaml(
    panel_market_sqlite: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calibration_dir = tmp_path / "calibration"
    assert (
        screening_main(
            _calibration_build_argv(
                sqlite_path=panel_market_sqlite, calibration_dir=calibration_dir
            )
        )
        == 0
    )
    out_path = tmp_path / "evaluation.yaml"

    code = screening_main(
        [
            "calibration-evaluate",
            "--calibration-dir",
            str(calibration_dir),
            "--horizon",
            "3m",
            "--run-purpose",
            "diagnostic",
            "--out",
            str(out_path),
        ]
    )
    capsys.readouterr()

    assert code == 0
    payload = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "estimate-calibration-evaluation"
    assert payload["scope"]["run_purpose"] == "diagnostic"
    assert payload["scope"]["requested_horizons"] == ["3m"]
    cohorts = payload["results"]["3m"]["cohorts"]
    assert [cohort["asof"] for cohort in cohorts] == [PANEL_ASOF.isoformat()]
