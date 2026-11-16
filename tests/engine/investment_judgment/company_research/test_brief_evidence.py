"""再訪の有限表示でも仮説と出典の対応を保つ。独立性や比較可能性は認定しない。"""

from datetime import datetime

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def _source(identity, *, day="2026-10-14", local=False):
    return {
        "source_id": identity,
        "ticker": "1234",
        "source_tier": "local_data" if local else "primary",
        **(
            {"provider": "synthetic", "dataset": "retained-external"}
            if local
            else {"ref": "https://example.test/ir"}
        ),
        "as_of": day,
        "retrieved_at": "2026-10-14T16:00:00+09:00",
        "used_for": "合成根拠。資料日・取得日は予想の対象期や更新時刻を保証しない",
    }


def _hypothesis(statement, source_ids, *, result="unresolved"):
    return {
        "statement": statement,
        "origin": "research",
        "result": result,
        "reason": "対象期・提供元更新時刻は未確認。会社計画への独立した賛成票にはしない。",
        "source_ids": source_ids,
    }


def _save(path, kind, sources, hypotheses):
    if kind == "research":
        ResearchStoreService(path, clock=lambda: NOW).save(
            ResearchDraft.model_validate(
                {
                    "research_id": "synthetic-evidence-brief",
                    "document": {
                        "ticker": "1234",
                        "as_of": "2026-10-14",
                        "investigated_scope": "出典共有と時計の違いを確認する合成研究",
                        "sources": sources,
                        "hypotheses": hypotheses,
                    },
                }
            )
        )
    else:
        document, review = pair_payload()
        document["input_snapshot"]["sources"].extend(sources)
        document["findings"]["hypotheses"] = hypotheses
        review["checked_source_ids"].extend(item["source_id"] for item in sources)
        update_review_hash(document, review)
        AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
            assessment_id(), document, review
        )
    return read_company_contexts(path, tickers=("1234",), as_of=NOW.date(), known_at=NOW)["1234"]


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_revisit_keeps_shared_reference_and_each_source_clock_without_upgrading_unknowns(
    tmp_path, kind
):
    path = tmp_path / "app"
    sources = [_source("company"), _source("external", day="2026-08-31", local=True)]
    hypotheses = [
        _hypothesis("会社資料は利益計画を掲げる", ["company"], result="supported"),
        _hypothesis("外部値は同じ対象期・基準の独立予想か", ["external", "company"]),
        _hypothesis("未取得の業界見通しが計画を支持するか", []),
    ]
    context = _save(path, kind, sources, hypotheses)
    retained = context.model_dump_json()
    before = path.read_bytes()
    brief = research_catalog(path, as_of=NOW.date(), known_at=NOW)["items"][0][kind]
    expected = {
        source["source_id"]: {
            key: source[key] for key in ("source_id", "source_tier", "as_of", "retrieved_at")
        }
        for source in sources
    }
    first, second, unknown = brief["hypotheses"]
    assert first["sources"] == [expected["company"]]
    assert second["sources"] == [expected["external"], expected["company"]]
    assert second["result"] == "unresolved"
    assert second["reason"] == hypotheses[1]["reason"]
    assert unknown["sources"] == []
    assert unknown["sources_omitted"] == second["sources_omitted"] == 0
    assert unknown["result"] == "unresolved"
    assert brief["source_dates"] == ["2026-08-31", "2026-10-14"]
    assert "exact reference" in brief["display"]
    # IDs stay local to the exact record; neither locator deduplication nor extra votes.
    assert first["sources"][0] == second["sources"][1]
    first["sources"][0]["as_of"] = "1900-01-01"
    assert second["sources"][1]["as_of"] == "2026-10-14"
    assert context.model_dump_json() == retained
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_hypothesis_citations_are_bounded_explicitly_without_changing_retained_context(
    tmp_path, kind
):
    sources = [_source(f"source-{index}") for index in range(6)]
    hypotheses = [
        _hypothesis(f"仮説{index}" * 100, [source["source_id"] for source in sources])
        for index in range(6)
    ]
    context = _save(tmp_path / "app", kind, sources, hypotheses)
    retained = context.model_dump_json()
    brief = company_context_brief(context)[kind]
    assert len(brief["hypotheses"]) == 3
    for hypothesis in brief["hypotheses"]:
        assert len(hypothesis["statement"]) == 240
        assert [source["source_id"] for source in hypothesis["sources"]] == [
            "source-0",
            "source-1",
            "source-2",
        ]
        assert hypothesis["sources_omitted"] == 3
    assert context.model_dump_json() == retained
