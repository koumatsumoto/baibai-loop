"""CLI-seam tests for the research and macro authoring commands.

A test that builds a model in Python and hands it to a service proves the model,
not the command: argv parsing, the ``--flag`` to typed-value conversion, and the
routed hand-offs to another module's parser all sit outside it. These drive
``main([...])`` with the argv an operator types and assert the write the command
is there to make, so the whole seam is on the path under test.

One normal path per subcommand. The question these answer is whether the request
travels end to end, not whether every argument combination behaves.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.helpers.fixed_now import FIXED_NOW
from tests.helpers.macro_context import macro_context_payload

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators.cli import main as macro_main
from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.workspace_cli import main as research_main
from baibai_engine.screening.research_triage import ResearchTriage, ResearchTriageService

ROOT = Path(__file__).resolve().parents[2]
THESIS_FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW_FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"

# The thesis the `app_method_root` fixture publishes into the seeded application DB.
SEEDED_THESIS_ID = "thesis-20260714-2331-r1"
RESEARCH_TRIAGE_ID = "research_triage-20260721-cli-seam"
ASSESSMENT_ID = "capital-allocation-assessment-20260722-cli-seam"
ASSESSMENT_ASOF = "2026-07-22"
PUBLISHED_AT = datetime(2026, 7, 22, 15, 0, tzinfo=JST)
RESEARCH_QUESTION = "受注残を確認"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _app_db(app_method_root: Path) -> Path:
    return app_method_root / "stores/application/baibai.sqlite"


def _publish_research_triage(db_path: Path) -> str:
    """Seed the selected case an assessment round is scaffolded and published against."""
    research_triage = ResearchTriage.model_validate(
        {
            "schema_version": 1,
            "kind": "research_triage",
            "research_triage_id": RESEARCH_TRIAGE_ID,
            "review_set_id": "review-set-cli-seam",
            "run_revision_id": "runrev-cli-seam",
            "as_of": "2026-07-21",
            "published_at": "2026-07-21T15:00:00+09:00",
            "macro_context_id": "macro-context-2026-07-21-cli-seam",
            "review_basis_research_triage_id": None,
            "triage_contract_id": "research-triage-v1",
            "entries": [
                {
                    "ticker": "2331",
                    "decision": "research",
                    "priority": 1,
                    "rationale": "deep research",
                    "research_question": RESEARCH_QUESTION,
                    "key_risk": "demand",
                },
                {
                    "ticker": "0001",
                    "decision": "skip",
                    "priority": None,
                    "rationale": "insufficient evidence",
                    "research_question": None,
                    "key_risk": None,
                },
            ],
        }
    )
    ResearchTriageService(db_path).publish(
        research_triage,
        review_set={
            "review_set_id": research_triage.review_set_id,
            "run_revision_id": research_triage.run_revision_id,
            "as_of": research_triage.as_of.isoformat(),
            "review_basis": {"judged_through_research_triage_id": None},
            "entries": [
                {
                    "ticker": ticker,
                    "review_position": position,
                    "nominations": [{"valuation_approach_id": "current-earnings-power"}],
                    "support_count": 1,
                    "analysis": {"expected_return": {"er_annual": er}, "data_quality": {}},
                }
                for position, (ticker, er) in enumerate((("2331", 0.12), ("0001", 0.04)), start=1)
            ],
        },
    )
    return RESEARCH_TRIAGE_ID


def _scaffold_capital_allocation_draft(db_path: Path, out: Path, research_triage_id: str) -> int:
    return research_main(
        [
            "capital-allocation-scaffold",
            "--db",
            str(db_path),
            "--capital-allocation-assessment-id",
            ASSESSMENT_ID,
            "--asof",
            ASSESSMENT_ASOF,
            "--research-triage-id",
            research_triage_id,
            "--thesis-id",
            SEEDED_THESIS_ID,
            "--out",
            str(out),
        ],
        now=PUBLISHED_AT,
    )


def _fill_judgment(draft: dict[str, Any]) -> dict[str, Any]:
    """Fill the prose the scaffold leaves blank, the way the operator does.

    The machine values stay untouched: publish rederives them and rejects an edit.
    """
    draft["headline"] = "現時点で買うに値する候補はない"
    draft["comparison"] = "唯一の深掘り候補が要求利回りを満たさなかった"
    draft["forgone"] = "2331 は決算後に再評価する"
    alternative = draft["alternatives"][0]
    alternative["disposition"] = "decline"
    alternative["rationale"] = "5年期待値が要求利回りに届かない"
    draft["review"]["reviewer_identity"] = "independent-reviewer"
    draft["review"]["draft_sha256"] = capital_allocation_draft_sha256(
        CapitalAllocationAssessment.model_validate(draft, strict=False)
    )
    return draft


def _write_yaml(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# research evaluate (routed to the decision CLI)
# --------------------------------------------------------------------------- #


def test_research_evaluate_routes_argv_to_the_decision_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`research evaluate <thesis>` must reach the decision CLI with its own argv.

    The domain parser only lists the command; the hand-off happens before parsing.
    A hand-off that went through the domain parser instead would reject the
    positional thesis path as an unknown argument.
    """
    thesis = tmp_path / "2026-07-03-2331-decision.yaml"
    thesis.write_text(THESIS_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "2331-decision-review.yaml").write_text(
        REVIEW_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert research_main(["evaluate", str(thesis)], now=FIXED_NOW) == 0

    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["decision_readiness"] == "ready"
    assert payload["thesis_status"] == "ready_with_warnings"


# --------------------------------------------------------------------------- #
# research capital-allocation-scaffold
# --------------------------------------------------------------------------- #


def test_assessment_scaffold_writes_a_draft_from_argv(
    app_method_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`capital-allocation-scaffold` must turn its flags into a draft file on disk.

    `--asof` is a string on the command line and a `date` in the call, and
    `--thesis-id` is a repeatable flag that has to arrive as a list; both only
    meet the scaffold through this command.
    """
    db_path = _app_db(app_method_root)
    research_triage_id = _publish_research_triage(db_path)
    out = app_method_root / "assessment-draft.yaml"

    assert _scaffold_capital_allocation_draft(db_path, out, research_triage_id) == 0

    draft = safe_load(out.read_text(encoding="utf-8"))
    assert draft["capital_allocation_assessment_id"] == ASSESSMENT_ID
    assert draft["as_of"] == ASSESSMENT_ASOF
    assert draft["published_at"] == PUBLISHED_AT.isoformat()
    assert draft["research_triage_id"] == research_triage_id
    assert [alternative["ticker"] for alternative in draft["alternatives"]] == ["2331"]
    assert "research_questions" not in draft["alternatives"][0]
    assert (
        yaml.safe_load(capsys.readouterr().out)["capital_allocation_assessment_id"] == ASSESSMENT_ID
    )


# --------------------------------------------------------------------------- #
# research capital-allocation-publish
# --------------------------------------------------------------------------- #


def test_assessment_publish_stores_the_round_from_argv(
    app_method_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`capital-allocation-publish <draft>` must land the round in the application DB.

    The draft reaches the store as a positional path, so the command owns reading
    the file, validating it against the contract, and writing the row.
    """
    db_path = _app_db(app_method_root)
    research_triage_id = _publish_research_triage(db_path)
    draft_path = app_method_root / "assessment-draft.yaml"
    assert _scaffold_capital_allocation_draft(db_path, draft_path, research_triage_id) == 0
    capsys.readouterr()
    _write_yaml(draft_path, _fill_judgment(safe_load(draft_path.read_text(encoding="utf-8"))))

    assert research_main(["capital-allocation-publish", str(draft_path), "--db", str(db_path)]) == 0

    assert (
        yaml.safe_load(capsys.readouterr().out)["capital_allocation_assessment_id"] == ASSESSMENT_ID
    )
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT capital_allocation_assessment_id, result, research_triage_id FROM capital_allocation_assessment"
        ).fetchall()
    assert rows == [(ASSESSMENT_ID, "no_allocation", research_triage_id)]


# --------------------------------------------------------------------------- #
# macro context (routed to the context CLI)
# --------------------------------------------------------------------------- #


def test_macro_context_publish_routes_argv_and_stores_the_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`macro context publish <draft>` must reach the context CLI and write the head.

    The macro parser only lists `context`; the group's own `--db`, its subcommand,
    and the positional draft are parsed by the routed module. `--check` never
    touches the store, so a real publish is the only way this seam is exercised
    all the way to the row and the head pointer.
    """
    db_path = tmp_path / "app.sqlite"
    payload = macro_context_payload()
    draft = _write_yaml(tmp_path / "macro-context-draft.yaml", payload)

    assert macro_main(["context", "--db", str(db_path), "publish", str(draft)]) == 0

    assert yaml.safe_load(capsys.readouterr().out)["context_id"] == payload["context_id"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT context_id FROM macro_context").fetchall() == [
            (payload["context_id"],)
        ]
        assert connection.execute("SELECT context_id FROM macro_context_head").fetchall() == [
            (payload["context_id"],)
        ]
