from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from tests.helpers.shortlist import rejected_entry, shortlist_payload

from baibai_engine.appdb.write import connect_rw
from baibai_engine.screening.cli.query import select_command
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.screening.selection import PreviousRankedSetError, load_previous_ranked_set


def _write_ranked_set_history(
    directory: Path,
    *,
    as_of: str,
    tickers: tuple[str, ...],
    kind: str = "daily-ranked-set-membership",
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
    ranked_set_history_dir: Path | None = None,
    previous_shortlist_id: str | None = None,
) -> tuple[int, dict[str, object]]:
    stdout = io.StringIO()
    code = select_command(
        asof_date=date.fromisoformat(asof),
        run_revision_id=run_revision_id,
        runs_db_path=root / "stores/screening/runs.sqlite",
        app_db_path=root / "stores/application/baibai.sqlite",
        previous_shortlist_id=previous_shortlist_id,
        ranked_set_history_dir=ranked_set_history_dir,
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


def test_select_reads_previous_candidates_from_ranked_set_history_when_the_run_store_has_none(
    app_method_root: Path,
) -> None:
    run_revision_id, asof = _pruned_to_latest_run(app_method_root / "stores/screening/runs.sqlite")
    history_dir = app_method_root / "history/ranked_sets"
    record = _write_ranked_set_history(history_dir, as_of="2026-07-07", tickers=("2331", "0001"))

    code, payload = _select(
        app_method_root,
        run_revision_id=run_revision_id,
        asof=asof,
        ranked_set_history_dir=history_dir,
    )

    assert code == 0
    overlap = _previous_overlap(payload)
    assert overlap["previous_candidates_ref"] == record.as_posix()
    assert overlap["previous_candidates_source"] == "ranked_set_history"
    assert overlap["previous_candidates_count"] == 2


def test_select_reads_pruned_canonical_previous_from_shortlist(
    app_method_root: Path,
) -> None:
    run_revision_id, asof = _pruned_to_latest_run(app_method_root / "stores/screening/runs.sqlite")
    shortlist_id = "shortlist-20260707-canonical"
    payload = shortlist_payload(
        shortlist_id=shortlist_id,
        selection_id="selection-pruned",
        run_revision_id="run-pruned",
        as_of="2026-07-07",
        published_at="2026-07-07T18:00:00+09:00",
        entries=[rejected_entry("2331"), rejected_entry("0001")],
    )
    with connect_rw(app_method_root / "stores/application/baibai.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO shortlist (
                shortlist_id, selection_id, run_revision_id, as_of, published_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                shortlist_id,
                "selection-pruned",
                "run-pruned",
                "2026-07-07",
                "2026-07-07T18:00:00+09:00",
                json.dumps(payload),
            ),
        )

    code, selection = _select(
        app_method_root,
        run_revision_id=run_revision_id,
        asof=asof,
        previous_shortlist_id=shortlist_id,
    )

    assert code == 0
    overlap = _previous_overlap(selection)
    assert overlap["previous_candidates_ref"] == shortlist_id
    assert overlap["previous_candidates_source"] == "canonical_shortlist"
    assert overlap["previous_candidates_count"] == 2


def test_select_reports_no_previous_candidates_without_a_ranked_set_history_directory(
    app_method_root: Path,
) -> None:
    run_revision_id, asof = _pruned_to_latest_run(app_method_root / "stores/screening/runs.sqlite")

    code, payload = _select(app_method_root, run_revision_id=run_revision_id, asof=asof)

    assert code == 0
    overlap = _previous_overlap(payload)
    assert overlap["previous_candidates_ref"] is None
    assert overlap["previous_candidates_source"] is None
    assert overlap["previous_candidates_count"] == 0


def test_select_prefers_the_run_store_over_the_ranked_set_history(
    app_method_root: Path,
) -> None:
    runs_path = app_method_root / "stores/screening/runs.sqlite"
    run_revision_id, asof = _latest_run(runs_path)
    previous = ScreeningRunReader(runs_path).previous_run(before_as_of_date=asof)
    assert previous is not None
    history_dir = app_method_root / "history/ranked_sets"
    _write_ranked_set_history(history_dir, as_of="2026-07-07", tickers=("2331",))

    code, payload = _select(
        app_method_root,
        run_revision_id=run_revision_id,
        asof=asof,
        ranked_set_history_dir=history_dir,
    )

    assert code == 0
    overlap = _previous_overlap(payload)
    assert overlap["previous_candidates_source"] == "run_revision"
    assert overlap["previous_candidates_ref"] == previous.run_revision_id


def test_select_fails_loudly_on_a_ranked_set_history_with_an_unsupported_contract(
    app_method_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_revision_id, asof = _pruned_to_latest_run(app_method_root / "stores/screening/runs.sqlite")
    history_dir = app_method_root / "history/ranked_sets"
    _write_ranked_set_history(history_dir, as_of="2026-07-07", tickers=("2331",), schema_version=2)

    code, _ = _select(
        app_method_root,
        run_revision_id=run_revision_id,
        asof=asof,
        ranked_set_history_dir=history_dir,
    )

    assert code == 1
    assert "unsupported contract" in capsys.readouterr().err


def test_load_previous_ranked_set_skips_a_day_that_published_no_ranked_set(tmp_path: Path) -> None:
    _write_ranked_set_history(tmp_path, as_of="2026-07-30", tickers=())
    older = _write_ranked_set_history(tmp_path, as_of="2026-07-29", tickers=("2331", "0001"))

    previous = load_previous_ranked_set(tmp_path, asof_date=date(2026, 7, 31))

    assert previous.ref_path == older.as_posix()
    assert previous.tickers == ("2331", "0001")


def test_load_previous_ranked_set_rejects_a_record_whose_as_of_contradicts_its_name(
    tmp_path: Path,
) -> None:
    _write_ranked_set_history(
        tmp_path, as_of="2026-07-30", tickers=("2331",), record_as_of="2026-07-29"
    )

    with pytest.raises(PreviousRankedSetError, match="does not match its name"):
        load_previous_ranked_set(tmp_path, asof_date=date(2026, 7, 31))


def test_load_previous_ranked_set_ignores_records_at_or_after_the_target_as_of(
    tmp_path: Path,
) -> None:
    _write_ranked_set_history(tmp_path, as_of="2026-07-31", tickers=("2331",))

    previous = load_previous_ranked_set(tmp_path, asof_date=date(2026, 7, 31))

    assert previous.ref_path is None
    assert previous.source is None
    assert previous.tickers == ()
