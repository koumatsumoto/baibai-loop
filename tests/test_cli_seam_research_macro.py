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

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators.cli import main as macro_main
from baibai_engine.research.assessment import BargainAssessment, assessment_draft_sha256
from baibai_engine.research.opportunity_cli import main as opportunity_main
from baibai_engine.screening.shortlist import SelectionBinding, Shortlist, ShortlistService
from tests.helpers.fixed_now import FIXED_NOW
from tests.helpers.macro_context import macro_context_payload

ROOT = Path(__file__).resolve().parents[1]
THESIS_FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision.yaml"
REVIEW_FIXTURE = ROOT / "tests/fixtures/thesis/2331-decision-review.yaml"

# The thesis the `app_method_root` fixture publishes into the seeded application DB.
SEEDED_THESIS_ID = "thesis-20260714-2331-r1"
SHORTLIST_ID = "shortlist-20260721-cli-seam"
ASSESSMENT_ID = "bargain-assessment-20260722-cli-seam"
ASSESSMENT_ASOF = "2026-07-22"
PUBLISHED_AT = datetime(2026, 7, 22, 15, 0, tzinfo=JST)
RESEARCH_QUESTION = "受注残を確認"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _app_db(app_method_root: Path) -> Path:
    return app_method_root / "data/app/baibai.sqlite"


def _publish_shortlist(db_path: Path) -> str:
    """Seed the selected lane an assessment round is scaffolded and published against."""
    shortlist = Shortlist.model_validate(
        {
            "schema_version": 4,
            "kind": "shortlist",
            "shortlist_id": SHORTLIST_ID,
            "selection_id": "selection-cli-seam",
            "run_revision_id": "runrev-cli-seam",
            "as_of": "2026-07-21",
            "published_at": "2026-07-21T15:00:00+09:00",
            "profile": "value",
            "macro_context_id": "macro-context-2026-07-21-cli-seam",
            "entries": [
                {
                    "ticker": "2331",
                    "decision": "selected",
                    "rank": 1,
                    "reason": "一次IRへ進める",
                    "narrative": {
                        "ploss": "中低",
                        "why": "受注端境",
                        "temporary": "翌期に戻る",
                        "structural": "毀損はない",
                        "survive": "net cashで耐える",
                        "unlock": "還元強化",
                        "upside": "正常化でPER12倍相当",
                        "downside": "簿価が床",
                        "rr": "下値が資産で支えられる",
                        "catalyst": "2Q決算",
                        "catalyst_date": None,
                        "macro": "sizing cautionは該当なし",
                        "counter": "構造鈍化",
                        "research": RESEARCH_QUESTION,
                        "value": "FV乖離が大きい",
                        "prov": "深掘り最優先",
                    },
                },
                {
                    "ticker": "0001",
                    "decision": "rejected",
                    "reason": "根拠が弱い",
                    "reject_class": "other",
                },
            ],
        }
    )
    ShortlistService(db_path).publish(
        shortlist,
        selection=SelectionBinding(
            selection_id=shortlist.selection_id,
            run_revision_id=shortlist.run_revision_id,
            as_of=shortlist.as_of,
            profile=shortlist.profile,
            macro_context_id=shortlist.macro_context_id,
            candidate_tickers=frozenset({"2331", "0001"}),
            candidate_er={"2331": 0.12, "0001": 0.04},
        ),
    )
    return SHORTLIST_ID


def _scaffold_assessment_draft(db_path: Path, out: Path, shortlist_id: str) -> int:
    return opportunity_main(
        [
            "assessment-scaffold",
            "--db",
            str(db_path),
            "--assessment-id",
            ASSESSMENT_ID,
            "--asof",
            ASSESSMENT_ASOF,
            "--shortlist-id",
            shortlist_id,
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
    lane = draft["lanes"][0]
    lane["disposition"] = "reject"
    lane["disposition_reason"] = "5年期待値が要求利回りに届かない"
    lane["reject_class"] = "price_already_converged"
    for field in (
        "business_model",
        "value_capture",
        "growth_quality",
        "financial_resilience",
        "strongest_countercase",
        "catalyst",
    ):
        lane[field] = f"{field} の判断"
    for question in lane["research_questions"]:
        question["answer"] = "翌期の受注残は横ばい"
        question["status"] = "answered"
    draft["review"]["reviewer_identity"] = "independent-reviewer"
    draft["review"]["draft_sha256"] = assessment_draft_sha256(
        BargainAssessment.model_validate(draft)
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

    assert opportunity_main(["evaluate", str(thesis)], now=FIXED_NOW) == 0

    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["decision_readiness"] == "ready"
    assert payload["thesis_status"] == "ready_with_warnings"


# --------------------------------------------------------------------------- #
# research assessment-scaffold
# --------------------------------------------------------------------------- #


def test_assessment_scaffold_writes_a_draft_from_argv(
    app_method_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`assessment-scaffold` must turn its flags into a draft file on disk.

    `--asof` is a string on the command line and a `date` in the call, and
    `--thesis-id` is a repeatable flag that has to arrive as a list; both only
    meet the scaffold through this command.
    """
    db_path = _app_db(app_method_root)
    shortlist_id = _publish_shortlist(db_path)
    out = app_method_root / "assessment-draft.yaml"

    assert _scaffold_assessment_draft(db_path, out, shortlist_id) == 0

    draft = safe_load(out.read_text(encoding="utf-8"))
    assert draft["assessment_id"] == ASSESSMENT_ID
    assert draft["as_of"] == ASSESSMENT_ASOF
    assert draft["published_at"] == PUBLISHED_AT.isoformat()
    assert draft["shortlist_id"] == shortlist_id
    assert [lane["ticker"] for lane in draft["lanes"]] == ["2331"]
    assert draft["lanes"][0]["research_questions"][0]["question"] == RESEARCH_QUESTION
    assert yaml.safe_load(capsys.readouterr().out)["assessment_id"] == ASSESSMENT_ID


# --------------------------------------------------------------------------- #
# research assessment-publish
# --------------------------------------------------------------------------- #


def test_assessment_publish_stores_the_round_from_argv(
    app_method_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`assessment-publish <draft>` must land the round in the application DB.

    The draft reaches the store as a positional path, so the command owns reading
    the file, validating it against the contract, and writing the row.
    """
    db_path = _app_db(app_method_root)
    shortlist_id = _publish_shortlist(db_path)
    draft_path = app_method_root / "assessment-draft.yaml"
    assert _scaffold_assessment_draft(db_path, draft_path, shortlist_id) == 0
    capsys.readouterr()
    _write_yaml(draft_path, _fill_judgment(safe_load(draft_path.read_text(encoding="utf-8"))))

    assert opportunity_main(["assessment-publish", str(draft_path), "--db", str(db_path)]) == 0

    assert yaml.safe_load(capsys.readouterr().out)["assessment_id"] == ASSESSMENT_ID
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT assessment_id, result, shortlist_id FROM bargain_assessment"
        ).fetchall()
    assert rows == [(ASSESSMENT_ID, "no_actionable_bargain", shortlist_id)]


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
