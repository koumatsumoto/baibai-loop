from __future__ import annotations

import io
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from baibai_app.api.server import create_app
from baibai_engine.proposals.cli import main as proposal_main
from baibai_engine.read_api import (
    list_holding_review_payloads,
    list_proposal_payloads,
    list_shortlist_payloads,
    list_thesis_payloads,
)
from baibai_engine.screening.cli.query import select_command
from baibai_engine.screening.run_store import ScreeningRunReader, ScreeningRunStore
from baibai_engine.screening.shortlist_cli import publish_shortlist


def _selected_narrative() -> dict[str, str]:
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


def test_select_and_shortlist_publish_from_explicit_run_revision(
    app_method_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_path = app_method_root / "data/screening/runs.sqlite"
    app_path = app_method_root / "data/app/baibai.sqlite"
    run = ScreeningRunReader(runs_path).latest_run()
    assert run is not None
    outputs: list[dict[str, object]] = []
    for _ in range(2):
        stdout = io.StringIO()
        assert (
            select_command(
                asof_date=date.fromisoformat(run.as_of_date),
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

    draft = app_method_root / "shortlist.yaml"
    draft.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "kind": "shortlist",
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
                        "rank": 1,
                        "reason": "一次IRへ進める",
                        "narrative": _selected_narrative(),
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    assert publish_shortlist(draft, app_db_path=app_path, runs_db_path=runs_path) == 0
    newer_shortlist = yaml.safe_load(draft.read_text(encoding="utf-8"))
    newer_shortlist["shortlist_id"] = "shortlist-20260708-test-newer"
    newer_shortlist["published_at"] = "2026-07-08T16:00:00+09:00"
    draft.write_text(
        yaml.safe_dump(newer_shortlist, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    assert publish_shortlist(draft, app_db_path=app_path, runs_db_path=runs_path) == 0
    emitted = yaml.safe_load(capsys.readouterr().out)
    assert emitted["shortlist_id"] == "shortlist-20260708-test-newer"
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/screening/latest")
    assert response.status_code == 200
    assert len(response.json()["selections"]) == 2
    assert len(response.json()["shortlists"]) == 1
    assert response.json()["shortlists"][0]["shortlist_id"] == ("shortlist-20260708-test-newer")
    assert response.json()["shortlists"][0]["as_of"] == run.as_of_date


def test_shortlist_publish_prints_reevaluation_task_suggestions(
    app_method_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_path = app_method_root / "data/screening/runs.sqlite"
    app_path = app_method_root / "data/app/baibai.sqlite"
    run_payload = yaml.safe_load(
        """\
run_id: screening-20260715
run_date: "2026-07-15"
asof_date: "2026-07-15"
universe_size: 3
run_at: "2026-07-15T12:00:00+09:00"
candidates:
  - ticker: "2331"
    name: ALSOK
    sector_33: サービス業
    per_trailing: 12.0
    metrics: {er_annual: 0.12}
    evidence_hits: []
    next_earnings_date: "2026-07-30"
  - ticker: "0001"
    name: Sample One
    sector_33: 情報・通信業
    metrics: {}
    evidence_hits: []
    next_earnings_date: "2026-08-06"
  - ticker: "0002"
    name: Sample Two
    sector_33: 小売業
    metrics: {}
    evidence_hits: []
"""
    )
    run_revision_id = ScreeningRunStore(runs_path).publish_run(run_payload).publication_id
    stdout = io.StringIO()
    assert (
        select_command(
            asof_date=date(2026, 7, 15),
            top=10,
            run_revision_id=run_revision_id,
            runs_db_path=runs_path,
            app_db_path=app_path,
            stdout=stdout,
        )
        == 0
    )
    selection_payload = yaml.safe_load(stdout.getvalue())
    assert isinstance(selection_payload, dict)
    selection_id = str(selection_payload["selection_id"])
    profile = str(selection_payload["selection"]["profile"])

    draft = app_method_root / "shortlist-triggers.yaml"
    draft.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "kind": "shortlist",
                "shortlist_id": "shortlist-20260715-trigger",
                "selection_id": selection_id,
                "run_revision_id": run_revision_id,
                "as_of": "2026-07-15",
                "published_at": "2026-07-15T15:00:00+09:00",
                "profile": profile,
                "macro_context_id": None,
                "entries": [
                    {
                        "ticker": "2331",
                        "decision": "selected",
                        "rank": 1,
                        "reason": "一次IRへ進める",
                        "narrative": _selected_narrative(),
                    },
                    {
                        "ticker": "0001",
                        "decision": "rejected",
                        "reason": "決算前で見送り",
                        "reject_class": "event_wait",
                    },
                    {
                        "ticker": "0002",
                        "decision": "rejected",
                        "reason": "決算日が読めない",
                        "reject_class": "event_wait",
                    },
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert publish_shortlist(draft, app_db_path=app_path, runs_db_path=runs_path) == 0
    captured = capsys.readouterr()

    yaml.safe_load(captured.out)  # stdout stays a single machine-readable YAML document
    assert (
        "baibai-engine task add --kind follow-up --ticker 0001 "
        '--title "0001 決算で見送り判断を再評価" '
        "--due 2026-08-06 --event-date 2026-08-06 "
        '--event-label "0001 決算"'
    ) in captured.err
    assert "0002" in captured.err
    assert "決算日未公表" in captured.err
    assert "2331" not in captured.err


def test_screening_api_falls_back_to_selection_bound_run(app_method_root: Path) -> None:
    """A selection-less newer revision (determinism re-run) must not blank the Baibai App."""

    runs_path = app_method_root / "data/screening/runs.sqlite"
    app_path = app_method_root / "data/app/baibai.sqlite"
    reader = ScreeningRunReader(runs_path)
    run = reader.latest_run()
    assert run is not None
    output = io.StringIO()
    assert (
        select_command(
            asof_date=date.fromisoformat(run.as_of_date),
            top=10,
            run_revision_id=run.run_revision_id,
            runs_db_path=runs_path,
            app_db_path=app_path,
            stdout=output,
        )
        == 0
    )
    repeated = dict(run.payload)
    repeated["candidates"] = list(run.candidates)
    repeated["run_at"] = "2026-07-08T14:00:00+09:00"
    newer = ScreeningRunStore(runs_path).publish_run(repeated)
    assert newer.publication_id != run.run_revision_id
    latest = reader.latest_run()
    assert latest is not None
    assert latest.run_revision_id == newer.publication_id

    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        payload = client.get("/api/screening/latest").json()
    assert payload["run"]["run_revision_id"] == run.run_revision_id
    assert payload["selections"]
    assert all(item["run_revision_id"] == run.run_revision_id for item in payload["selections"])


def test_select_rejects_unknown_run_revision(app_method_root: Path) -> None:
    assert (
        select_command(
            asof_date=date(2026, 7, 8),
            top=10,
            run_revision_id="run-revision-missing",
            runs_db_path=app_method_root / "data/screening/runs.sqlite",
            app_db_path=app_method_root / "data/app/baibai.sqlite",
            stdout=io.StringIO(),
        )
        == 1
    )


def test_select_requires_explicit_previous_revision_when_prior_asof_is_ambiguous(
    app_method_root: Path,
) -> None:
    runs_path = app_method_root / "data/screening/runs.sqlite"
    reader = ScreeningRunReader(runs_path)
    current = reader.latest_run()
    assert current is not None
    previous = reader.previous_run(before_as_of_date=current.as_of_date)
    assert previous is not None
    repeated = dict(previous.payload)
    repeated["candidates"] = list(previous.candidates)
    repeated["run_at"] = "2026-07-01T13:00:00+09:00"
    second = ScreeningRunStore(runs_path).publish_run(repeated)

    assert (
        select_command(
            asof_date=date.fromisoformat(current.as_of_date),
            top=10,
            run_revision_id=current.run_revision_id,
            runs_db_path=runs_path,
            app_db_path=app_method_root / "data/app/baibai.sqlite",
            stdout=io.StringIO(),
        )
        == 1
    )
    assert (
        select_command(
            asof_date=date.fromisoformat(current.as_of_date),
            top=10,
            run_revision_id=current.run_revision_id,
            runs_db_path=runs_path,
            app_db_path=app_method_root / "data/app/baibai.sqlite",
            stdout=io.StringIO(),
            previous_run_revision_id=second.publication_id,
        )
        == 0
    )


def test_pruned_run_is_a_weak_reference_for_all_application_reads(
    app_method_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_path = app_method_root / "data/screening/runs.sqlite"
    app_path = app_method_root / "data/app/baibai.sqlite"
    reader = ScreeningRunReader(runs_path)
    source_run = reader.latest_run()
    assert source_run is not None
    output = io.StringIO()
    assert (
        select_command(
            asof_date=date.fromisoformat(source_run.as_of_date),
            top=10,
            run_revision_id=source_run.run_revision_id,
            runs_db_path=runs_path,
            app_db_path=app_path,
            stdout=output,
        )
        == 0
    )
    selection_payload = yaml.safe_load(output.getvalue())
    selection_id = str(selection_payload["selection_id"])
    selection = selection_payload["selection"]
    assert isinstance(selection, dict)
    input_refs = selection["input_refs"]
    assert isinstance(input_refs, dict)
    draft = app_method_root / "shortlist-weak-ref.yaml"
    draft.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "kind": "shortlist",
                "shortlist_id": "shortlist-20260708-weak-ref",
                "selection_id": selection_id,
                "run_revision_id": source_run.run_revision_id,
                "as_of": source_run.as_of_date,
                "published_at": "2026-07-08T16:00:00+09:00",
                "profile": selection["profile"],
                "macro_context_id": input_refs["macro_context_ref"],
                "entries": [
                    {
                        "ticker": "2331",
                        "decision": "selected",
                        "rank": 1,
                        "reason": "一次IRへ進める",
                        "narrative": _selected_narrative(),
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    assert publish_shortlist(draft, app_db_path=app_path, runs_db_path=runs_path) == 0
    with sqlite3.connect(app_path) as connection:
        thesis_id, review_id = connection.execute(
            """
            SELECT p.thesis_id, r.review_id
            FROM thesis AS p
            JOIN thesis_review AS r USING (thesis_id)
            LIMIT 1
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO holding_review (
                holding_review_id, ticker, as_of, thesis_id, candidate_thesis_id, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "holding-review-weak-ref",
                "2331",
                "2026-07-20",
                thesis_id,
                None,
                json.dumps({"action": "hold", "note": "継続監視"}, ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            INSERT INTO proposal (
                proposal_id, ticker, thesis_id, review_id, created_at,
                status, decided_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proposal-weak-ref",
                "2331",
                thesis_id,
                review_id,
                "2026-07-20T12:00:00+09:00",
                "pending",
                None,
                "{}",
            ),
        )
    canonical_before = {
        "shortlists": list_shortlist_payloads(app_path),
        "research": list_thesis_payloads(app_path),
        "proposals": list_proposal_payloads(app_path),
        "holding_reviews": list_holding_review_payloads(app_path),
    }
    newer = dict(source_run.payload)
    newer.update(
        {
            "run_id": "screening-20260709",
            "run_date": "2026-07-09",
            "asof_date": "2026-07-09",
            "run_at": "2026-07-09T15:00:00+09:00",
            "candidates": list(source_run.candidates),
        }
    )
    store = ScreeningRunStore(runs_path)
    store.publish_run(newer, run_revision_id="run-newer")
    store.prune(keep=1)

    assert reader.get_run(source_run.run_revision_id) is None
    assert reader.get_selection(selection_id) is None
    assert canonical_before == {
        "shortlists": list_shortlist_payloads(app_path),
        "research": list_thesis_payloads(app_path),
        "proposals": list_proposal_payloads(app_path),
        "holding_reviews": list_holding_review_payloads(app_path),
    }
    capsys.readouterr()
    assert (
        proposal_main(
            [
                "--db",
                str(app_path),
                "--market-db",
                str(app_method_root / "data/screening/market.sqlite"),
                "show",
                "proposal-weak-ref",
            ]
        )
        == 0
    )
    assert yaml.safe_load(capsys.readouterr().out)["proposal_id"] == "proposal-weak-ref"
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        responses = {
            path: client.get(path)
            for path in (
                "/api/health",
                "/api/dashboard",
                "/api/screening/latest",
                "/api/macro",
                "/api/operations",
                "/api/securities/2331",
            )
        }
    assert all(response.status_code == 200 for response in responses.values())
    screening = responses["/api/screening/latest"].json()
    assert screening["run"]["run_revision_id"] == "run-newer"
    assert "runs" not in screening
    assert screening["shortlists"][0]["shortlist_id"] == ("shortlist-20260708-weak-ref")
    assert responses["/api/operations"].json()["proposals"][0]["proposal_id"] == (
        "proposal-weak-ref"
    )
    security = responses["/api/securities/2331"].json()
    assert security["revisions"][0]["thesis_id"] == thesis_id
    assert security["holding_reviews"][0]["holding_review_id"] == "holding-review-weak-ref"


def test_screening_api_returns_bounded_empty_state_after_pruning_all_runs(
    app_method_root: Path,
) -> None:
    runs_path = app_method_root / "data/screening/runs.sqlite"
    result = ScreeningRunStore(runs_path).prune(keep=0)

    assert result.kept_runs == 0
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/screening/latest")

    assert response.status_code == 200
    assert response.json()["run"] is None
    assert response.json()["rows"] == []
    assert response.json()["selections"] == []
    assert len(response.json()["shortlists"]) <= 1
