"""過去の公表に書かれた順・理由だけを読む。新規選定の抽選はない。"""

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_technical_financial import row
from tests.helpers.archived_technical_financial import stored_result

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)


@pytest.mark.parametrize(
    ("version", "ties"),
    [
        (2, "ticker_code_order_not_quality_or_return"),
        (3, "stable_company_hash_then_ticker_not_quality_or_return"),
        (12, "stable_company_hash_then_ticker_not_quality_or_return"),
    ],
)
def test_original_order_and_its_historical_label_survive(version, ties):
    rows = tuple(row(t, state="input_unresolved") for t in ("1001", "1002", "1003"))
    order = ("1003", "1001", "1002")
    value = stored_result(
        rows,
        order=order,
        policy=TechnicalFinancialPolicy(
            method=f"technical-financial-discovery-v{version}", page_size=2
        ),
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    raw = codec.dump_json(value)
    decoded = codec.validate_json(raw)
    pages = [
        technical_financial_screening_brief(decoded, lane="input_review", page=p) for p in (0, 1)
    ]
    assert tuple(c["ticker"] for page in pages for c in page["cards"]) == order
    assert all(page["presentation"]["ties"] == ties for page in pages)
    assert codec.dump_json(decoded) == raw
