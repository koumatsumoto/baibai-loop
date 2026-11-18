"""旧公表で実際に保存した資本異動だけを表示する。現モデルへは接続しない。"""

import pytest
from pydantic import TypeAdapter
from tests.helpers.archived_technical_financial import stored_result, stored_row

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)


def saved(paths, version=24):
    row = stored_row("1234", lane="growth_research", paths=paths)
    return stored_result(
        (row,),
        order=(row.ticker,),
        policy=TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
    )


def first(result):
    return technical_financial_screening_brief(result)["cards"][0]["financial_paths"]


@pytest.mark.parametrize("version", [17, 24, 25])
def test_missing_null_and_old_reason_label_stay_as_recorded(version):
    codec = TypeAdapter(TechnicalFinancialSelection)
    for paths in (
        {},
        {"capital_action_context": {"observations": None}},
        {"capital_action_context": {"capital_basis_reason": "unavailable_annual_profit"}},
    ):
        raw = codec.dump_json(saved(paths, version))
        history = codec.validate_json(raw)
        assert {k: v for k, v in first(history).items() if k == "capital_action_context"} == paths
        assert codec.dump_json(history) == raw


def test_first_page_bounds_each_array_and_never_reorders_or_changes_the_recorded_details():
    events = [{"issued_shares": 130, "received_at": "2026-10-02T00:00:00Z"}] * 4 + [
        {"issued_shares": None, "effective_date": "2026-11-20"}
    ]
    adjustments = [{"factor": i} for i in (1, 8, 2, 3)]
    result = saved(
        {"capital_action_context": {"observations": events, "share_basis_adjustments": adjustments}}
    )
    shown = first(result)["capital_action_context"]
    assert shown == {
        "observations": events[:3],
        "observations_omitted_count": 2,
        "share_basis_adjustments": adjustments[:3],
        "share_basis_adjustments_omitted_count": 1,
    }
    shown["observations"][0]["issued_shares"] = 999
    assert result.rows[0].financial_paths["capital_action_context"] == {
        "observations": events,
        "share_basis_adjustments": adjustments,
    }
