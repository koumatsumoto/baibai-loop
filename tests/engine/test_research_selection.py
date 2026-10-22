"""依頼・policy・固定入力の構造と意味を検証する。"""

from datetime import datetime

import pytest
from tests.helpers.research_selection import draft_for, selected_target, selection

from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.selection import (
    ResearchSelection,
    ResearchSelectionDraft,
    parse_selection,
    selection_sha256,
)
from baibai_engine.research.selection_policy import ResearchCapacity, validate_capacity

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


@pytest.mark.parametrize("count", [0, 1, 3])
def test_capacity_accepts_saved_policy_and_hash_covers_the_entire_input(count):
    value = selection(now=NOW, tickers=tuple(str(1000 + i) for i in range(count)))
    assert parse_selection(value.model_dump(mode="json")) == value
    assert selection_sha256(value) != selection_sha256(
        value.model_copy(update={"selection_summary": "different reason"})
    )


@pytest.mark.parametrize(
    "change",
    [
        {"request": {"text": " ", "scope": "discover_new"}},
        {"request": {"text": "request", "scope": "specified_stocks"}},
        {"request": {"text": "request", "scope": "discover_new", "requested_tickers": ["1234"]}},
        {
            "request": {
                "text": "request",
                "scope": "specified_stocks",
                "requested_tickers": ["1234", "1234"],
            }
        },
        {"input_known_at": "2026-10-14T18:00:00"},
        {"research_as_of": "2026-10-15"},
        {"request": {"text": "request", "scope": "review_positions"}},
        {
            "targets": [
                {
                    "ticker": "1234",
                    "purpose": "new",
                    "rationale": " ",
                    "research_question": "q",
                    "key_risk": "r",
                }
            ]
        },
    ],
)
def test_invalid_request_scope_time_and_reason_rejected(change):
    raw = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
    raw.update(change)
    with pytest.raises(ValueError, match=r"validation error|capacity"):
        parse_selection(raw)


def test_draft_four_parses_but_canonical_and_new_write_reject_it(tmp_path):
    value = selection(now=NOW, tickers=("1000", "1001", "1002"))
    raw = draft_for(value).model_dump(mode="json")
    raw["targets"].append(selected_target("1003").model_dump(mode="json"))
    draft = ResearchSelectionDraft.model_validate(raw)
    assert len(draft.targets) == 4
    with pytest.raises(ValueError, match=r"validation error|capacity"):
        validate_capacity(4, ResearchCapacity())
    with pytest.raises(ValueError, match=r"validation error|capacity"):
        ResearchSelection.model_validate(
            {**value.model_dump(mode="json"), "targets": raw["targets"]}
        )
    expanded = value.model_copy(update={"capacity": ResearchCapacity(max_deep_research_targets=4)})
    with pytest.raises(ValueError, match="current capacity"):
        OperationService(tmp_path / "app").start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(
                checkpoint="start", artifacts=(expanded.model_dump(mode="json"),)
            ),
        )
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize(
    "modify",
    [
        lambda p: p["inputs"]["analysis"]["unavailable"].clear(),
        lambda p: p["inputs"]["securities_context"].clear(),
        lambda p: p["inputs"]["securities_context"]["1234"].update(machine_valuation={}),
        lambda p: p["inputs"]["analysis"].update(run_revision_id="run"),
        lambda p: p["inputs"]["analysis"].update(analyses={"1234": {"ticker": "5678"}}),
    ],
)
def test_snapshot_requires_exact_coverage_and_no_duplicate_analysis(modify):
    raw = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
    modify(raw)
    with pytest.raises(ValueError, match=r"validation error|capacity"):
        parse_selection(raw)


def test_technical_context_allows_old_snapshot_but_rejects_unknown_domain_keys():
    old = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
    assert "technical_analysis" not in parse_selection(old).inputs.securities_context["1234"]
    old["inputs"]["securities_context"]["1234"]["technical_analysis"] = {"bar_count": 0}
    assert parse_selection(old).inputs.securities_context["1234"]["technical_analysis"] == {
        "bar_count": 0
    }
    old["inputs"]["securities_context"]["1234"]["technical_score"] = 1
    with pytest.raises(ValueError, match="security context"):
        parse_selection(old)


def test_normalization_todo_text_and_legacy_rejection():
    draft = draft_for(selection(now=NOW, tickers=("285A",), scope="specified_stocks")).model_dump(
        mode="json"
    )
    draft["targets"][0]["ticker"] = "285a"
    draft["targets"][0]["rationale"] = " TODO means an open research question "
    assert ResearchSelectionDraft.model_validate(draft).targets[0].rationale.startswith("TODO")
    with pytest.raises(ValueError, match="explicit V2 cutover"):
        parse_selection({"kind": "research_selection", "request": "old", "targets": []})
