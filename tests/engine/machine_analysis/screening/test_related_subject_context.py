"""保存された共有材料との関連を表示し、候補条件や調査母集団にはしない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.helpers.archived_technical_financial import SIX_STOCK_ORDER, archived_from_models
from tests.helpers.technical_financial import _seed

from baibai_engine.machine_analysis.generation.contracts import (
    GenerationMetadata,
    GenerationSubject,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialRow,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    project_technical_financial_row,
)

SUBJECT = "industry:synthetic-survey"
OTHER = "industry:another-survey"
BASIS = "One group business; not whole-company exposure or survey membership"


@pytest.mark.parametrize("basis", [None, {}, {SUBJECT: ""}, {SUBJECT: BASIS, OTHER: "B"}])
def test_only_recorded_relation_is_added_without_changing_paths_gates_or_order(tmp_path, basis):
    fixed = _seed(tmp_path / "runs")
    original = archived_from_models(fixed, order=SIX_STOCK_ORDER)
    ticker = original.result.rows[0].ticker
    plan = deepcopy(fixed.subject_plan)
    plan[ticker] = plan[ticker].model_copy(
        update={"related_subjects": (SUBJECT, OTHER), "related_subject_basis": basis}
    )
    plan[SUBJECT] = GenerationSubject(scope="sector", model_ids=("industry_trends",))
    plan[OTHER] = GenerationSubject(scope="sector", model_ids=("industry_trends",))
    linked = fixed.model_copy(update={"subject_plan": plan})
    result = archived_from_models(linked, order=SIX_STOCK_ORDER)
    codec = TypeAdapter(TechnicalFinancialRow)
    expected = {"related_subjects": [SUBJECT, OTHER]}
    if basis is not None:
        expected["related_subject_basis"] = deepcopy(basis)
    for before, after in zip(original.result.rows, result.result.rows, strict=True):
        saved = codec.dump_python(after, mode="json")
        if after.ticker == ticker:
            assert saved.pop("related_subject_context") == expected
        else:
            assert "related_subject_context" not in saved
        assert saved == codec.dump_python(before, mode="json")
    assert result.result.display_order == original.result.display_order
    assert result.result.summary == original.result.summary
    assert result.price_inputs == original.price_inputs
    if basis:
        basis[SUBJECT] = "later current explanation"
        assert result.result.rows[0].related_subject_context == expected


@pytest.mark.parametrize("scope", ["legacy", "unplanned", "unlinked"])
def test_missing_relation_stays_absent_and_legacy_row_roundtrips(tmp_path, scope):
    fixed = _seed(tmp_path / "runs")
    metadata = GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
    if scope == "legacy":
        metadata = metadata.model_copy(update={"schema_version": 1, "subject_plan": None})
    ticker = "9999" if scope == "unplanned" else next(iter(metadata.subject_plan or {"1234": None}))
    row = project_technical_financial_row(
        ticker, models=tuple(m for m in fixed.models if m.subject == ticker), metadata=metadata
    )
    codec = TypeAdapter(TechnicalFinancialRow)
    canonical = codec.dump_json(row)
    assert b"related_subject_context" not in canonical
    assert codec.dump_json(codec.validate_json(canonical)) == canonical
