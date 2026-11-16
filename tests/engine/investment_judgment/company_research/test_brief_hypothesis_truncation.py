"""末尾の限定・不確実性が見えない抜粋を全文と扱わない。"""

import pytest
from tests.engine.investment_judgment.company_research.test_brief_evidence import (
    _hypothesis,
    _save,
    _source,
)

from baibai_engine.investment_judgment.company_research.context import company_context_brief


@pytest.mark.parametrize("kind", ["research", "assessment"])
@pytest.mark.parametrize("length", [239, 240, 241])
def test_each_hypothesis_excerpt_reports_its_own_truncation_without_rewriting_evidence(
    tmp_path, kind, length
):
    hypothesis = _hypothesis("仮" * length, ["one"])
    hypothesis["reason"] = "根" * (480 - length)
    context = _save(tmp_path / "app", kind, [_source("one")], [hypothesis])
    original = context.model_dump_json()
    brief = company_context_brief(context)[kind]
    item = brief["hypotheses"][0]
    assert item["statement"] == hypothesis["statement"][:240]
    assert item["reason"] == hypothesis["reason"][:240]
    assert item["statement_truncated"] is (length > 240)
    assert item["reason_truncated"] is (480 - length > 240)
    assert item["result"] == "unresolved"
    assert "exact reference" in brief["display"]
    item["reason_truncated"] = not item["reason_truncated"]
    assert context.model_dump_json() == original


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_unicode_excerpt_retains_existing_codepoint_boundary_without_fetching_more_sources(
    tmp_path, kind
):
    hypothesis = _hypothesis("株" * 239 + "😀末尾の限定", [])
    hypothesis["reason"] = "未" * 239 + "株限定"
    context = _save(tmp_path / "app", kind, [], [hypothesis])
    item = company_context_brief(context)[kind]["hypotheses"][0]
    assert item["statement"] == "株" * 239 + "😀"
    assert item["reason"] == "未" * 239 + "株"
    assert item["statement_truncated"] is True
    assert item["reason_truncated"] is True
    assert item["sources"] == []
    assert item["sources_omitted"] == 0
