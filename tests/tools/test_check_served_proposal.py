"""照合は同じ正式記録と配信本文に限定し、実在の銘柄情報を表示しない。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.engine.investment_judgment.workflow.test_selection_evidence import evidence_case
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal
from tools.diagnostics.check_served_proposal import main

from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.sources.proposal import read_proposal_views

STAGES = [
    "app_publication",
    "captured_latest",
    "captured_history",
    "matching_view_timestamps",
    "fixed_case",
]


def _capture(app, path):
    latest, history = read_proposal_views(app, generated_at=PUBLISHED + timedelta(days=4))
    paths = [path / "private-latest.json", path / "private-history.json"]
    for name, body in zip(paths, (latest, history), strict=True):
        name.write_text(body.model_dump_json(), encoding="utf-8")
    return paths


@pytest.fixture
def fixed(tmp_path):
    case = backend_case(tmp_path)
    published = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
    ).publish(reviewed_proposal(case))
    case.operations.complete(
        case.operation.operation_id,
        proposal_id=published.proposal_id,
        result="合成の調査結果",
        next="別途依頼",
        completed_at=PUBLISHED,
    )
    return case, published, _capture(case.app, tmp_path)


def _args(db, latest, history):
    return ["--db", str(db), "--latest-json", str(latest), "--history-json", str(history)]


def test_one_publication_projected_from_fixed_db_and_completed_case(fixed, capsys):
    case, published, paths = fixed
    before = [file.read_bytes() for file in (case.app, *paths)]
    assert (
        main(
            [
                *_args(case.app, *paths),
                "--proposal-id",
                published.proposal_id,
                "--require-completed",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == ["PASS " + stage for stage in [*STAGES, "completed_case"]]
    assert before == [file.read_bytes() for file in (case.app, *paths)]


@pytest.mark.parametrize(
    ("damage", "stage"),
    [
        ("different_id", "app_publication"),
        ("foreign_ticker_both", "captured_latest"),
        ("changed_limit", "captured_history"),
        ("wrong_selection", "captured_history"),
        ("duplicate_history", "captured_history"),
        ("partial", "captured_history"),
        ("different_capture", "matching_view_timestamps"),
        ("malformed", "captured_latest"),
    ],
)
def test_incomplete_mixed_or_foreign_reports_fail_without_leaking_inputs(
    fixed, capsys, damage, stage
):
    case, _, paths = fixed
    latest, history = (json.loads(path.read_bytes()) for path in paths)
    args = _args(case.app, *paths)
    if damage == "different_id":
        args += ["--proposal-id", "private-unexpected-ID"]
    elif damage == "foreign_ticker_both":
        for row in (latest["proposal"], history["proposals"][0]):
            row["items"][0]["ticker"] = "private-unexpected-ticker"
    elif damage == "changed_limit":
        history["proposals"][0]["items"][0]["terms"]["limit_price_yen"] = "999"
    elif damage == "wrong_selection":
        history["proposals"][0]["saved_market_context"]["selection_sha256"] = "0" * 64
    elif damage == "duplicate_history":
        history["proposals"].append(history["proposals"][0])
    elif damage == "partial":
        history["status"] = "partial"
        history["unavailable_proposal_ids"] = ["private-unreadable-ID"]
    elif damage == "different_capture":
        history["generated_at"] = (PUBLISHED + timedelta(days=7)).isoformat()
    for path, document in zip(paths, (latest, history), strict=True):
        path.write_text(json.dumps(document), encoding="utf-8")
    if damage == "malformed":
        paths[0].write_text("private-unexpected-body", encoding="utf-8")
    assert main(args) != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        *["PASS " + name for name in STAGES[: STAGES.index(stage)]],
        "FAIL " + stage,
    ]


def test_migrated_case_and_corrected_latest_are_not_rebound_to_last_history(
    tmp_path, monkeypatch, capsys
):
    case, original, _, _ = evidence_case(tmp_path, monkeypatch)
    paths = _capture(case.app, tmp_path)
    assert json.loads(paths[1].read_bytes())["proposals"][0]["proposal_id"] != original.proposal_id
    args = [*_args(case.app, *paths), "--proposal-id", original.proposal_id]
    assert main(args) == 0
    assert capsys.readouterr().out.splitlines() == ["PASS " + name for name in STAGES]
    assert main([*args, "--require-completed"]) != 0
    assert capsys.readouterr().out.splitlines()[-1] == "FAIL completed_case"


def test_generation_cannot_predate_later_corrected_publication_in_history(tmp_path, capsys):
    case = backend_case(tmp_path)
    first = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
    ).publish(reviewed_proposal(case))
    second = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED + timedelta(minutes=1),
    ).publish(
        reviewed_proposal(
            case,
            action="no_action",
            proposal_id="investment-proposal-00000000-0000-4000-8000-000000000004",
        )
    )
    erroneous = ProposalReference(
        proposal_id=second.proposal_id, draft_sha256=second.review.draft_sha256
    )
    ProposalRecordService(case.app, clock=lambda: PUBLISHED + timedelta(minutes=2)).correct(
        ProposalCorrectionDraft(
            correction_id="synthetic-clock-order",
            erroneous_ref=erroneous,
            replacement_ref=ProposalReference(
                proposal_id=first.proposal_id, draft_sha256=first.review.draft_sha256
            ),
            reason="合成の訂正",
        ),
        expected_latest_ref=erroneous,
    )
    paths = _capture(case.app, tmp_path)
    latest, history = (json.loads(path.read_bytes()) for path in paths)
    assert latest["proposal"]["proposal_id"] == first.proposal_id
    assert [row["proposal_id"] for row in history["proposals"]] == [
        second.proposal_id,
        first.proposal_id,
    ]
    assert main(_args(case.app, *paths)) == 0
    assert capsys.readouterr().out.splitlines() == ["PASS " + stage for stage in STAGES]

    early = PUBLISHED + timedelta(seconds=30)
    assert first.published_at < early < second.published_at
    for path, document in zip(paths, (latest, history), strict=True):
        document["generated_at"] = early.isoformat()
        path.write_text(json.dumps(document), encoding="utf-8")
    assert main(_args(case.app, *paths)) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        *["PASS " + stage for stage in STAGES[:3]],
        "FAIL matching_view_timestamps",
    ]


@pytest.mark.parametrize(
    "damage",
    [
        "reverse_order",
        "change_nonlatest_time",
        "numeric_nonlatest_time",
        "numeric_string_nonlatest_time",
        "nonpublisher_separator",
        "empty_nonlatest_items",
        "missing_corrections",
        "missing_human_reports",
    ],
)
def test_history_keeps_the_app_publication_index_and_selectable_rows(tmp_path, capsys, damage):
    case = backend_case(tmp_path)
    first = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
    ).publish(reviewed_proposal(case))
    second = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED + timedelta(microseconds=1),
    ).publish(
        reviewed_proposal(
            case,
            action="no_action",
            proposal_id="investment-proposal-00000000-0000-4000-8000-000000000004",
        )
    )
    paths = _capture(case.app, tmp_path)
    history = json.loads(paths[1].read_bytes())
    assert [row["proposal_id"] for row in history["proposals"]] == [
        second.proposal_id,
        first.proposal_id,
    ]
    assert main(_args(case.app, *paths)) == 0
    capsys.readouterr()

    if damage == "reverse_order":
        # Same IDs and bodies, but the browser displays its history in wire order.
        history["proposals"].reverse()
    elif damage == "change_nonlatest_time":
        # Still sorted, but not the publication instant saved for the pinned report.
        history["proposals"][1]["published_at"] = (
            first.published_at - timedelta(microseconds=1)
        ).isoformat()
    elif damage == "numeric_nonlatest_time":
        # This denotes the correct instant but is not a display date string.
        history["proposals"][1]["published_at"] = first.published_at.timestamp()
    elif damage == "numeric_string_nonlatest_time":
        history["proposals"][1]["published_at"] = str(int(first.published_at.timestamp()))
    elif damage == "nonpublisher_separator":
        history["proposals"][1]["published_at"] = first.published_at.isoformat(sep="_")
    elif damage == "empty_nonlatest_items":
        history["proposals"][1]["items"] = []
    else:
        del history["proposals"][1][damage.removeprefix("missing_")]
    paths[1].write_text(json.dumps(history), encoding="utf-8")
    assert main(_args(case.app, *paths)) == 1
    assert capsys.readouterr().out.splitlines() == [
        "PASS app_publication",
        "PASS captured_latest",
        "FAIL captured_history",
    ]


def test_same_two_reports_cannot_hide_a_different_loaded_case(tmp_path, capsys):
    case = backend_case(tmp_path)
    TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
    ).publish(reviewed_proposal(case))
    # 合成DBのactive Caseだけを変える。2表示が同じAppから再生成できても原選定の証明にはならない。
    with closing(sqlite3.connect(case.app)) as connection, connection:
        text = connection.execute("SELECT payload FROM operation_session").fetchone()[0]
        payload = json.loads(text)
        artifact = next(row for row in payload["artifacts"] if row["kind"] == "research_selection")
        artifact["selection_summary"] = "private-different-selection"
        connection.execute("UPDATE operation_session SET payload=?", (json.dumps(payload),))
    paths = _capture(case.app, tmp_path)
    before = case.app.read_bytes()
    assert main(_args(case.app, *paths)) != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        *["PASS " + name for name in STAGES[:-1]],
        "FAIL fixed_case",
    ]
    assert before == case.app.read_bytes()


def test_absent_app_is_not_created_or_treated_as_empty(tmp_path, capsys):
    absent = tmp_path / "private-missing.sqlite"
    assert main(_args(absent, tmp_path / "one", tmp_path / "two")) != 0
    assert not absent.exists()
    assert capsys.readouterr().out == "FAIL app_publication\n"


def test_identity_alone_does_not_make_an_unrelated_history_row_readable(
    tmp_path, monkeypatch, capsys
):
    case, original, _, _ = evidence_case(tmp_path, monkeypatch)
    paths = _capture(case.app, tmp_path)
    history = json.loads(paths[1].read_bytes())
    other = next(row for row in history["proposals"] if row["proposal_id"] != original.proposal_id)
    key = other["proposal_id"]
    other.clear()
    other["proposal_id"] = key
    paths[1].write_text(json.dumps(history), encoding="utf-8")
    assert main(_args(case.app, *paths)) != 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "PASS app_publication",
        "PASS captured_latest",
        "FAIL captured_history",
    ]


def test_usage_error_does_not_echo_private_argument_values(capsys):
    with pytest.raises(SystemExit) as error:
        main(["--db", "one", "--latest-json", "two", "--history-json", "three", "private-value"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == "FAIL arguments\n"
    assert captured.out == ""
