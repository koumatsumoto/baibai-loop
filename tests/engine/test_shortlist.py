from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from baibai_engine.screening.shortlist import (
    SelectionBinding,
    Shortlist,
    ShortlistConflictError,
    ShortlistService,
)
from baibai_engine.screening.shortlist_cli import reevaluation_task_suggestions


def _narrative() -> dict[str, object]:
    return {
        "ploss": "中低",
        "why": "一時的な受注端境で売られている",
        "temporary": "翌期の受注残は積み上がっている",
        "structural": "構造的な需要毀損の証拠はない",
        "survive": "net cashで5年の下振れに耐えられる",
        "unlock": "自己株買いと増配で還元余地がある",
        "upside": "受注が平年並みに戻れば正常利益ベースでPER12倍相当まで",
        "downside": "受注が半減しても営業黒字を保ち、簿価純資産が下値を支える",
        "rr": "下値が資産で支えられる一方、正常化の上値が倍近い",
        "catalyst": "2Q決算で受注残の回復が確認できるか",
        "catalyst_date": "2026-08-06",
        "macro": "connectionのsizing cautionに該当なし。research優先度ヒントの内需回復系に合致",
        "counter": "受注が構造的に鈍化している可能性",
        "research": "受注残と粗利率の推移を一次IRで確認",
        "value": "FV乖離が大きく深掘り価値が高い",
        "prov": "深掘り最優先",
    }


def _shortlist() -> Shortlist:
    return Shortlist.model_validate(
        {
            "schema_version": 4,
            "kind": "shortlist",
            "shortlist_id": "shortlist-20260719-base",
            "selection_id": "selection-test",
            "run_revision_id": "runrev-test",
            "as_of": "2026-07-19",
            "published_at": "2026-07-19T14:00:00+09:00",
            "profile": "default",
            "macro_context_id": "macro-context-2026-07-19-base",
            "entries": [
                {
                    "ticker": "2331",
                    "decision": "selected",
                    "rank": 1,
                    "reason": "一次IRへ進める",
                    "narrative": _narrative(),
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


def _longlist_row(ticker: str, rank: int) -> dict[str, object]:
    """One selection longlist row, in the shape the publisher reads."""
    return {
        "rank": rank,
        "ticker": ticker,
        "name": f"name-{ticker}",
        "screening_playbook": "cashflow-yield-discount",
        "expected_return_pct": 12.0,
        "fair_value_anchor_yen": 1250.0,
        "market_price_yen": 1000.0,
        "liquidity_status": "pass",
        "durability_warnings": [],
        "event_warnings": ["stale_financials"],
        "selection_reasons": ["valuation_reversion"],
        # 判断時の入力であって焼き込み対象ではない。allowlist が落とすことを下の test が固定する。
        "buyback_authorization": {
            "status": "stale_filing",
            "latest_filing_date": "2026-04-13",
            "filing_age_days": 113,
            "observed_from": "2025-08-01",
        },
        "fv_convergence": {
            "status": "clear",
            "warning_code": None,
            "market_price_yen": 1000.0,
            "anchors_yen": {"fv_sector_median_yen": 1250.0},
            "er_reversion_annual": 0.011,
        },
        # The research hand-off block rides along on the same row and must not be
        # required by the judgment record.
        "estimate_snapshot": {"as_of": "2026-07-21"},
    }


def _binding(machine_rows: dict[str, dict[str, object]] | None = None) -> SelectionBinding:
    shortlist = _shortlist()
    return SelectionBinding(
        selection_id=shortlist.selection_id,
        run_revision_id=shortlist.run_revision_id,
        as_of=shortlist.as_of,
        profile=shortlist.profile,
        macro_context_id=shortlist.macro_context_id,
        candidate_tickers=frozenset({"2331", "0001"}),
        candidate_er={"2331": 0.12, "0001": 0.04},
        candidate_machine_rows=machine_rows or {},
    )


def test_shortlist_publish_is_immutable_and_identical_retry_is_no_change(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    service = ShortlistService(path)
    shortlist = _shortlist()
    service.publish(shortlist, selection=_binding())
    service.publish(shortlist, selection=_binding())

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM shortlist").fetchone()[0] == 1
    changed = shortlist.model_copy(
        update={"entries": shortlist.entries[:1]},
    )
    with pytest.raises(ShortlistConflictError):
        service.publish(changed, selection=_binding())


def test_publish_keeps_the_machine_estimate_the_judgment_was_made_against(
    tmp_path: Path,
) -> None:
    # The run store keeps a few generations and the horizon takes months, so the
    # ranking the judgment started from is gone by the time it could be compared
    # unless publish writes it into the judgment.
    path = tmp_path / "app.sqlite"
    ShortlistService(path).publish(_shortlist(), selection=_binding())

    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM shortlist").fetchone()[0])
    assert {entry["ticker"]: entry["er_annual"] for entry in payload["entries"]} == {
        "2331": 0.12,
        "0001": 0.04,
    }


def test_publish_keeps_the_machine_coordinates_the_judgment_was_compared_against(
    tmp_path: Path,
) -> None:
    # The selection that ranked these tickers is deleted with its run after three
    # generations, and the review surface has nothing else to show beside the
    # narrative. Publishing has to carry the coordinates into the judgment.
    path = tmp_path / "app.sqlite"
    rows = {"2331": _longlist_row("2331", 3)}
    ShortlistService(path).publish(_shortlist(), selection=_binding(rows))

    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM shortlist").fetchone()[0])
    by_ticker = {entry["ticker"]: entry["machine_snapshot"] for entry in payload["entries"]}

    assert by_ticker["2331"]["rank"] == 3
    assert by_ticker["2331"]["fair_value_anchor_yen"] == 1250.0
    assert by_ticker["2331"]["market_price_yen"] == 1000.0
    assert by_ticker["2331"]["event_warnings"] == ["stale_financials"]
    assert by_ticker["2331"]["fv_convergence"]["status"] == "clear"
    assert "estimate_snapshot" not in by_ticker["2331"]
    # longlist view が増えても判断記録は追随しない。取得枠 annotation は判断時に
    # selection から読む入力であり、shortlist entry へは焼き込まない。
    assert "buyback_authorization" not in by_ticker["2331"]
    # A ticker the selection did not rank has nothing to burn in.
    assert by_ticker["0001"] is None


def test_a_selection_without_a_longlist_burns_nothing_in(tmp_path: Path) -> None:
    # `select` emits a longlist only when asked for one. Nothing to record is a
    # normal state, not a reason to fail the publication.
    path = tmp_path / "app.sqlite"
    ShortlistService(path).publish(_shortlist(), selection=_binding())

    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM shortlist").fetchone()[0])

    assert all(entry["machine_snapshot"] is None for entry in payload["entries"])


def test_a_published_snapshot_is_not_recomputed_by_a_later_run(tmp_path: Path) -> None:
    # Same discipline as er_annual: the record says what the judgment saw, so a
    # newer ranking must not overwrite it through a retry.
    path = tmp_path / "app.sqlite"
    service = ShortlistService(path)
    service.publish(_shortlist(), selection=_binding({"2331": _longlist_row("2331", 3)}))

    with pytest.raises(ShortlistConflictError):
        service.publish(_shortlist(), selection=_binding({"2331": _longlist_row("2331", 9)}))

    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM shortlist").fetchone()[0])
    snapshot = next(
        entry["machine_snapshot"] for entry in payload["entries"] if entry["ticker"] == "2331"
    )

    assert snapshot["rank"] == 3


def test_shortlist_rejects_duplicate_ticker() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {"ticker": "2331", "decision": "rejected", "reason": "a", "reject_class": "other"},
        {"ticker": "2331", "decision": "rejected", "reason": "b", "reject_class": "other"},
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def _no_selected_shortlist() -> Shortlist:
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "rejected",
            "reason": "正常利益ベースでも割高",
            "reject_class": "price_already_converged",
        },
        {
            "ticker": "0001",
            "decision": "rejected",
            "reason": "一時益で見かけ上安いだけ",
            "reject_class": "one_off_earnings",
        },
    ]
    return Shortlist.model_validate(payload)


def test_shortlist_records_a_cycle_where_nothing_was_worth_researching(tmp_path: Path) -> None:
    shortlist = _no_selected_shortlist()

    assert shortlist.selected_by_rank() == ()
    assert [entry.reason for entry in shortlist.entries] == [
        "正常利益ベースでも割高",
        "一時益で見かけ上安いだけ",
    ]

    path = tmp_path / "app.sqlite"
    ShortlistService(path).publish(shortlist, selection=_binding())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM shortlist").fetchone()[0] == 1


def test_no_selected_cycle_suggests_a_reevaluation_trigger_for_every_entry() -> None:
    suggestions = reevaluation_task_suggestions(
        _no_selected_shortlist(),
        {
            "2331": {"next_earnings_date": "2026-08-06"},
            "0001": {},
        },
    )

    assert len(suggestions) == 2
    assert "--due 2026-08-06" in suggestions[0]
    assert suggestions[1].startswith("#")


def test_shortlist_selected_entry_requires_narrative() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {"ticker": "2331", "decision": "selected", "rank": 1, "reason": "深掘りへ"}
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_shortlist_rejected_entry_forbids_narrative() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "selected",
            "rank": 1,
            "reason": "深掘りへ",
            "narrative": _narrative(),
        },
        {
            "ticker": "0001",
            "decision": "rejected",
            "reason": "弱い",
            "reject_class": "other",
            "narrative": _narrative(),
        },
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_shortlist_rejected_entry_requires_a_known_reject_class() -> None:
    payload = _shortlist().payload()
    rejected = payload["entries"][1]
    del rejected["reject_class"]
    with pytest.raises(ValidationError, match="must include a reject_class"):
        Shortlist.model_validate(payload)

    rejected["reject_class"] = "future_guess"
    with pytest.raises(ValidationError, match="Input should be"):
        Shortlist.model_validate(payload)


def test_shortlist_selected_entry_forbids_reject_class() -> None:
    payload = _shortlist().payload()
    payload["entries"][0]["reject_class"] = "other"
    with pytest.raises(ValidationError, match="must not include a reject_class"):
        Shortlist.model_validate(payload)


@pytest.mark.parametrize(
    "missing_field",
    ["upside", "downside", "rr", "catalyst", "macro"],
)
def test_shortlist_selected_entry_requires_each_risk_reward_field(missing_field: str) -> None:
    narrative = _narrative()
    del narrative[missing_field]
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "selected",
            "rank": 1,
            "reason": "深掘りへ",
            "narrative": narrative,
        }
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_shortlist_selected_entry_requires_provisional_rank() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "selected",
            "reason": "深掘りへ",
            "narrative": _narrative(),
        }
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_shortlist_rejected_entry_forbids_provisional_rank() -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "selected",
            "rank": 1,
            "reason": "深掘りへ",
            "narrative": _narrative(),
        },
        {
            "ticker": "0001",
            "decision": "rejected",
            "rank": 2,
            "reason": "弱い",
            "reject_class": "other",
        },
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


@pytest.mark.parametrize("ranks", [(1, 3), (1, 1), (2, 3)])
def test_shortlist_rejects_non_contiguous_provisional_ranks(ranks: tuple[int, int]) -> None:
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "selected",
            "rank": ranks[0],
            "reason": "深掘りへ",
            "narrative": _narrative(),
        },
        {
            "ticker": "0001",
            "decision": "selected",
            "rank": ranks[1],
            "reason": "深掘りへ",
            "narrative": _narrative(),
        },
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


@pytest.mark.parametrize("catalyst_date", ["2026-07-18", "2028-07-19"])
def test_shortlist_rejects_catalyst_date_outside_the_reevaluation_window(
    catalyst_date: str,
) -> None:
    narrative = _narrative()
    narrative["catalyst_date"] = catalyst_date
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "selected",
            "rank": 1,
            "reason": "深掘りへ",
            "narrative": narrative,
        }
    ]
    with pytest.raises(ValidationError):
        Shortlist.model_validate(payload)


def test_shortlist_allows_undated_catalyst_and_orders_selected_by_rank() -> None:
    narrative = _narrative()
    narrative["catalyst_date"] = None
    payload = _shortlist().payload()
    payload["entries"] = [
        {
            "ticker": "2331",
            "decision": "selected",
            "rank": 2,
            "reason": "深掘りへ",
            "narrative": narrative,
        },
        {
            "ticker": "0001",
            "decision": "selected",
            "rank": 1,
            "reason": "深掘り最優先",
            "narrative": _narrative(),
        },
    ]
    shortlist = Shortlist.model_validate(payload)

    assert [entry.ticker for entry in shortlist.selected_by_rank()] == ["0001", "2331"]


def test_reevaluation_suggestion_emits_runnable_task_add_for_rejected_with_earnings_date() -> None:
    suggestions = reevaluation_task_suggestions(
        _shortlist(),
        {
            "2331": {"next_earnings_date": "2026-07-30"},
            "0001": {"next_earnings_date": "2026-08-06"},
        },
    )

    assert suggestions == [
        (
            "baibai-engine task add --kind follow-up --ticker 0001 "
            '--title "0001 決算で見送り判断を再評価" '
            "--due 2026-08-06 --event-date 2026-08-06 "
            '--event-label "0001 決算"'
        )
    ]


def test_reevaluation_suggestion_notes_missing_earnings_date() -> None:
    suggestions = reevaluation_task_suggestions(
        _shortlist(),
        {"2331": {"next_earnings_date": "2026-07-30"}, "0001": {}},
    )

    assert len(suggestions) == 1
    note = suggestions[0]
    assert note.startswith("# 0001")
    assert "将来の決算日なし" in note
    assert "次回決算日の公表" in note


def test_reevaluation_suggestion_skips_consumed_event_and_uses_future_estimate() -> None:
    shortlist = _no_selected_shortlist().model_copy(
        update={
            "as_of": date(2026, 8, 7),
            "published_at": _no_selected_shortlist().published_at.replace(
                year=2026, month=8, day=9
            ),
        }
    )

    suggestions = reevaluation_task_suggestions(
        shortlist,
        {
            "2331": {
                "next_earnings_date": "2026-08-06",
                "fin_latest_disclosed_date": "2026-08-07",
                "next_earnings_estimated_date": "2026-11-06",
            },
            "0001": {
                "next_earnings_date": "2026-08-05",
                "fin_latest_disclosed_date": "2026-08-05",
            },
        },
    )

    assert "--due 2026-11-06" in suggestions[0]
    assert 'event-label "2331 決算（推定）"' in suggestions[0]
    assert "2026-08-06" not in "\n".join(suggestions)
    assert "2026-08-05" not in "\n".join(suggestions)
    assert suggestions[1].startswith("# 0001")


def test_reevaluation_suggestion_uses_jst_publication_date_for_due_boundary() -> None:
    shortlist = _no_selected_shortlist().model_copy(
        update={
            "as_of": date(2026, 8, 7),
            "published_at": datetime(2026, 8, 9, 16, 0, tzinfo=UTC),
        }
    )

    suggestions = reevaluation_task_suggestions(
        shortlist,
        {
            "2331": {"next_earnings_date": "2026-08-09"},
            "0001": {},
        },
    )

    assert all("--due 2026-08-09" not in suggestion for suggestion in suggestions)
    assert suggestions[0].startswith("# 2331")


def test_reevaluation_suggestion_does_not_repeat_an_open_followup() -> None:
    """同じ決算が 2 cycle 連続で trigger になるのは正常。2 度目は起票を勧めない。

    汎用 title の新しい task を隣へ並べると、既存 task が持つ具体的な確認事項が薄まる。
    """

    suggestions = reevaluation_task_suggestions(
        _shortlist(),
        {
            "2331": {"next_earnings_date": "2026-07-30"},
            "0001": {"next_earnings_date": "2026-08-06"},
        },
        existing_followups={("0001", "2026-08-06")},
    )

    assert len(suggestions) == 1
    note = suggestions[0]
    assert note.startswith("# 0001")
    assert "既に open" in note
    assert "task add" not in note


def test_a_followup_on_another_date_does_not_suppress_the_suggestion() -> None:
    """抑止は ticker と event 日付の対で決める。前回の別 event は今回を消さない。"""

    suggestions = reevaluation_task_suggestions(
        _shortlist(),
        {
            "2331": {"next_earnings_date": "2026-07-30"},
            "0001": {"next_earnings_date": "2026-08-06"},
        },
        existing_followups={("0001", "2026-05-08")},
    )

    assert len(suggestions) == 1
    assert suggestions[0].startswith("baibai-engine task add")
