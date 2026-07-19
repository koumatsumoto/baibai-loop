from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from baibai_app.api.server import create_app
from baibai_engine.screening.cli.query import select_command
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.screening.shortlist_cli import publish_shortlist


def test_select_and_reviewed_shortlist_publish_from_explicit_run_revision(
    app_records_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_path = app_records_root / "data/screening/runs.sqlite"
    app_path = app_records_root / "data/app/baibai.sqlite"
    run = ScreeningRunReader(runs_path).latest_run()
    assert run is not None
    outputs: list[dict[str, object]] = []
    for _ in range(2):
        stdout = io.StringIO()
        assert (
            select_command(
                asof_date=date.fromisoformat(run.as_of_date),
                macro_context_path=None,
                top=10,
                run_revision_id=run.run_revision_id,
                runs_db_path=runs_path,
                app_db_path=app_path,
                stdout=stdout,
            )
            == 0
        )
        payload = yaml.safe_load(stdout.getvalue())
        assert isinstance(payload, dict)
        outputs.append(payload)

    assert outputs[0]["selection_id"] != outputs[1]["selection_id"]
    selection = outputs[0]["selection"]
    assert isinstance(selection, dict)
    refs = selection["input_refs"]
    previous = ScreeningRunReader(runs_path).previous_run(before_as_of_date=run.as_of_date)
    assert previous is not None
    assert refs == {
        "candidates_ref": run.run_revision_id,
        "macro_context_ref": None,
        "previous_candidates_ref": previous.run_revision_id,
    }

    draft = app_records_root / "shortlist.yaml"
    draft.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "kind": "reviewed-shortlist",
                "shortlist_id": "shortlist-20260708-test",
                "selection_id": outputs[0]["selection_id"],
                "run_revision_id": run.run_revision_id,
                "as_of": run.as_of_date,
                "published_at": "2026-07-08T15:00:00+09:00",
                "profile": selection["profile"],
                "macro_context_id": None,
                "entries": [
                    {
                        "ticker": "2331",
                        "decision": "selected",
                        "reason": "一次IRへ進める",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    assert publish_shortlist(draft, app_db_path=app_path, runs_db_path=runs_path) == 0
    emitted = yaml.safe_load(capsys.readouterr().out)
    assert emitted["shortlist_id"] == "shortlist-20260708-test"
    with TestClient(create_app(app_records_root), base_url="http://127.0.0.1") as client:
        response = client.get(
            "/api/screening/latest",
            params={"run_revision_id": run.run_revision_id},
        )
    assert response.status_code == 200
    assert len(response.json()["selections"]) == 2
    assert response.json()["reviewed_shortlists"][0]["shortlist_id"] == ("shortlist-20260708-test")


def test_select_rejects_unknown_run_revision(app_records_root: Path) -> None:
    assert (
        select_command(
            asof_date=date(2026, 7, 8),
            macro_context_path=None,
            top=10,
            run_revision_id="run-revision-missing",
            runs_db_path=app_records_root / "data/screening/runs.sqlite",
            app_db_path=app_records_root / "data/app/baibai.sqlite",
            stdout=io.StringIO(),
        )
        == 1
    )


def test_select_requires_explicit_previous_revision_when_prior_asof_is_ambiguous(
    app_records_root: Path,
) -> None:
    runs_path = app_records_root / "data/screening/runs.sqlite"
    reader = ScreeningRunReader(runs_path)
    current = reader.latest_run()
    assert current is not None
    previous = reader.previous_run(before_as_of_date=current.as_of_date)
    assert previous is not None
    repeated = dict(previous.payload)
    repeated["run_at"] = "2026-07-01T13:00:00+09:00"
    second = ScreeningRunStore(runs_path).publish_run(repeated)

    assert (
        select_command(
            asof_date=date.fromisoformat(current.as_of_date),
            macro_context_path=None,
            top=10,
            run_revision_id=current.run_revision_id,
            runs_db_path=runs_path,
            app_db_path=app_records_root / "data/app/baibai.sqlite",
            stdout=io.StringIO(),
        )
        == 1
    )
    assert (
        select_command(
            asof_date=date.fromisoformat(current.as_of_date),
            macro_context_path=None,
            top=10,
            run_revision_id=current.run_revision_id,
            runs_db_path=runs_path,
            app_db_path=app_records_root / "data/app/baibai.sqlite",
            stdout=io.StringIO(),
            previous_run_revision_id=second.publication_id,
        )
        == 0
    )
