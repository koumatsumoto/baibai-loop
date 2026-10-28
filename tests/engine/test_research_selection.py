"""依頼・policy・固定入力の構造と意味を検証する。"""

from datetime import datetime

import pytest
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.research_selection import selected_target, selection

from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelection,
    GeneratedResearchSelectionDraft,
    ResearchSelection,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_policy import ResearchCapacity, validate_capacity

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
    raw = value.model_dump(mode="json")
    raw["targets"].append(selected_target("1003").model_dump(mode="json"))
    inputs = raw["inputs"]
    gap = {**inputs["input_gaps"][0], "ticker": "1003"}
    inputs["input_gaps"].append(gap)
    inputs["analysis"]["unavailable"]["1003"] = gap["reason"]
    inputs["securities_context"]["1003"] = {
        "market_context": None,
        "external_assessment": None,
    }
    with pytest.raises(ValueError, match="saved research capacity"):
        validate_capacity(4, ResearchCapacity())
    with pytest.raises(ValueError, match="saved research capacity"):
        ResearchSelection.model_validate(raw)
    # A positive control proves that capacity is the only invalid dimension.
    raw["capacity"]["max_deep_research_targets"] = 4
    expanded = ResearchSelection.model_validate(raw)
    assert len(expanded.targets) == 4
    current = seed_generated_selection(
        runs=tmp_path / "runs", now=NOW, tickers=("1000", "1001", "1002")
    )
    draft_raw = generated_draft_for(current).model_dump(mode="json")
    draft_raw["targets"].append(selected_target("1003").model_dump(mode="json"))
    assert len(GeneratedResearchSelectionDraft.model_validate(draft_raw).targets) == 4
    current_raw = current.model_dump(mode="json")
    current_raw["targets"] = draft_raw["targets"]
    current_raw["inputs"]["generation"]["subjects"].append("1003")
    with pytest.raises(ValueError, match="saved research capacity"):
        GeneratedResearchSelection.model_validate(current_raw)
    current_raw["capacity"]["max_deep_research_targets"] = 4
    expanded = GeneratedResearchSelection.model_validate(current_raw)
    with pytest.raises(ValueError, match="current capacity"):
        OperationService(tmp_path / "app", runs_path=tmp_path / "runs").start(
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


@pytest.mark.parametrize(
    "optional_domains",
    [(), ("technical_analysis",), ("company_growth",), ("technical_analysis", "company_growth")],
)
def test_optional_domain_contexts_preserve_retained_snapshot_shape(optional_domains):
    raw = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
    context = raw["inputs"]["securities_context"]["1234"]
    for domain in optional_domains:
        context[domain] = {"status": "unavailable"}
    assert parse_selection(raw).model_dump(mode="json") == raw
    context["technical_score"] = 1
    with pytest.raises(ValueError, match="security context"):
        parse_selection(raw)
    del context["technical_score"]
    del context["market_context"]
    with pytest.raises(ValueError, match="security context"):
        parse_selection(raw)


def test_normalization_todo_text_and_legacy_rejection(tmp_path):
    draft = generated_draft_for(
        seed_generated_selection(
            runs=tmp_path / "runs", now=NOW, tickers=("285A",), scope="specified_stocks"
        )
    ).model_dump(mode="json")
    draft["targets"][0]["ticker"] = "285a"
    draft["targets"][0]["rationale"] = " TODO means an open research question "
    assert (
        GeneratedResearchSelectionDraft.model_validate(draft)
        .targets[0]
        .rationale.startswith("TODO")
    )
    with pytest.raises(ValueError, match="explicit V2 cutover"):
        parse_selection({"kind": "research_selection", "request": "old", "targets": []})


def test_saved_legacy_selection_is_readable_but_cannot_start_new_operation(tmp_path):
    saved = selection(now=NOW, tickers=("1234",))
    assert parse_selection(saved.model_dump(mode="json")) == saved
    with pytest.raises(ValueError, match="resume-only"):
        OperationService(tmp_path / "app", runs_path=tmp_path / "absent-runs").start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(
                checkpoint="new request", artifacts=(saved.model_dump(mode="json"),)
            ),
        )
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "absent-runs").exists()
