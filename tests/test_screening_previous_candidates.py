from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from baibai_engine.screening.cli.query import select_command
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.screening.selection import PreviousLonglistError, load_previous_longlist


def _write_longlist_history(
    directory: Path,
    *,
    as_of: str,
    tickers: tuple[str, ...],
    kind: str = "daily-longlist-membership",
    schema_version: int = 1,
    record_as_of: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{as_of}.json"
    path.write_text(
        json.dumps(
            {
                "kind": kind,
                "schema_version": schema_version,
                "as_of": record_as_of or as_of,
                "run_revision_id": f"run-{as_of}",
                "selection_status": "available" if tickers else "selection_missing",
                "selection_id": f"selection-{as_of}" if tickers else None,
                "selection_created_at": f"{as_of}T18:00:00+09:00" if tickers else None,
                "members": [
                    {"ticker": ticker, "rank": rank, "er_annual": 0.12}
                    for rank, ticker in enumerate(tickers, start=1)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _latest_run(runs_path: Path) -> tuple[str, str]:
    reader = ScreeningRunReader(runs_path)
    latest = reader.latest_run()
    assert latest is not None
    return latest.run_revision_id, latest.as_of_date


def _pruned_to_latest_run(runs_path: Path) -> tuple[str, str]:
    """Reproduce retention evicting the prior as-of, which is what empties the diff."""

    ScreeningRunStore(runs_path).prune(keep=1)
    run_revision_id, asof = _latest_run(runs_path)
    assert ScreeningRunReader(runs_path).previous_run(before_as_of_date=asof) is None
    return run_revision_id, asof


def _select(
    root: Path,
    *,
    run_revision_id: str,
    asof: str,
    longlist_history_dir: Path | None = None,
) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    code = select_command(
        asof_date=date.fromisoformat(asof),
        top=10,
        run_revision_id=run_revision_id,
        runs_db_path=root / "data/screening/runs.sqlite",
        app_db_path=root / "data/app/baibai.sqlite",
        longlist_history_dir=longlist_history_dir,
        stdout=stdout,
    )
    if code != 0:
        return code, {}
    payload = yaml.safe_load(stdout.getvalue())
    assert isinstance(payload, dict)
    return code, payload


def _previous_overlap(payload: dict[str, object]) -> dict[str, object]:
    selection = payload["selection"]
    assert isinstance(selection, dict)
    diagnostics = selection["diagnostics"]
    assert isinstance(diagnostics, dict)
    overlap = diagnostics["previous_overlap"]
    assert isinstance(overlap, dict)
    return overlap


def test_select_reads_previous_candidates_from_longlist_history_when_the_run_store_has_none(
    app_method_root: Path,
) -> None:
    run_revision_id, asof = _pruned_to_latest_run(app_method_root / "data/screening/runs.sqlite")
    history_dir = app_method_root / "history/longlists"
    record = _write_longlist_history(history_dir, as_of="2026-07-07", tickers=("2331", "0001"))

    code, payload = _select(
        app_method_root,
        run_revision_id=run_revision_id,
        asof=asof,
        longlist_history_dir=history_dir,
    )

    assert code == 0
    overlap = _previous_overlap(payload)
    assert overlap["previous_candidates_ref"] == record.as_posix()
    assert overlap["previous_candidates_source"] == "longlist_history"
    assert overlap["previous_candidates_count"] == 2


def test_select_reports_no_previous_candidates_without_a_longlist_history_directory(
    app_method_root: Path,
) -> None:
    run_revision_id, asof = _pruned_to_latest_run(app_method_root / "data/screening/runs.sqlite")

    code, payload = _select(app_method_root, run_revision_id=run_revision_id, asof=asof)

    assert code == 0
    overlap = _previous_overlap(payload)
    assert overlap["previous_candidates_ref"] is None
    assert overlap["previous_candidates_source"] is None
    assert overlap["previous_candidates_count"] == 0


def test_select_prefers_the_run_store_over_the_longlist_history(
    app_method_root: Path,
) -> None:
    runs_path = app_method_root / "data/screening/runs.sqlite"
    run_revision_id, asof = _latest_run(runs_path)
    previous = ScreeningRunReader(runs_path).previous_run(before_as_of_date=asof)
    assert previous is not None
    history_dir = app_method_root / "history/longlists"
    _write_longlist_history(history_dir, as_of="2026-07-07", tickers=("2331",))

    code, payload = _select(
        app_method_root,
        run_revision_id=run_revision_id,
        asof=asof,
        longlist_history_dir=history_dir,
    )

    assert code == 0
    overlap = _previous_overlap(payload)
    assert overlap["previous_candidates_source"] == "run_revision"
    assert overlap["previous_candidates_ref"] == previous.run_revision_id


def test_select_fails_loudly_on_a_longlist_history_with_an_unsupported_contract(
    app_method_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_revision_id, asof = _pruned_to_latest_run(app_method_root / "data/screening/runs.sqlite")
    history_dir = app_method_root / "history/longlists"
    _write_longlist_history(history_dir, as_of="2026-07-07", tickers=("2331",), schema_version=2)

    code, _ = _select(
        app_method_root,
        run_revision_id=run_revision_id,
        asof=asof,
        longlist_history_dir=history_dir,
    )

    assert code == 1
    assert "unsupported contract" in capsys.readouterr().err


def test_load_previous_longlist_skips_a_day_that_published_no_longlist(tmp_path: Path) -> None:
    _write_longlist_history(tmp_path, as_of="2026-07-30", tickers=())
    older = _write_longlist_history(tmp_path, as_of="2026-07-29", tickers=("2331", "0001"))

    previous = load_previous_longlist(tmp_path, asof_date=date(2026, 7, 31))

    assert previous.ref_path == older.as_posix()
    assert previous.tickers == ("2331", "0001")


def test_load_previous_longlist_rejects_a_record_whose_as_of_contradicts_its_name(
    tmp_path: Path,
) -> None:
    _write_longlist_history(
        tmp_path, as_of="2026-07-30", tickers=("2331",), record_as_of="2026-07-29"
    )

    with pytest.raises(PreviousLonglistError, match="does not match its name"):
        load_previous_longlist(tmp_path, asof_date=date(2026, 7, 31))


def test_load_previous_longlist_ignores_records_at_or_after_the_target_as_of(
    tmp_path: Path,
) -> None:
    _write_longlist_history(tmp_path, as_of="2026-07-31", tickers=("2331",))

    previous = load_previous_longlist(tmp_path, asof_date=date(2026, 7, 31))

    assert previous.ref_path is None
    assert previous.source is None
    assert previous.tickers == ()
