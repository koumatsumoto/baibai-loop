"""市場・業界から企業cashへの作者仮説を、採択せず再訪の材料にする。"""

from contextlib import closing
from datetime import datetime, timedelta

import pytest
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.company_research.store import (
    ResearchStoreService,
    load_research,
)
from baibai_engine.investment_judgment.workflow.selection import selection_sha256
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def _source(identity, day="2026-10-14"):
    return {
        "source_id": identity,
        "ticker": "1234",
        "source_tier": "primary",
        "ref": "https://example.test/report",
        "as_of": day,
        "retrieved_at": "2026-10-14T16:00:00+09:00",
        "used_for": "資料日と取得日。比較対象期・因果・独立性は未確認",
    }


def _save(path, note, sources):
    submitted = ResearchDraft.model_validate(
        {
            "research_id": "value-capture-only",
            "document": {
                "ticker": "1234",
                "as_of": "2026-10-14",
                "investigated_scope": "市場条件から企業cashへの未採択の伝達仮説のみ",
                "sources": sources,
                "value_capture_hypothesis": note,
            },
        }
    )
    record = ResearchStoreService(path, clock=lambda: NOW).save(submitted)
    context = read_company_contexts(path, tickers=("1234",), as_of=NOW.date(), known_at=NOW)["1234"]
    return submitted, record, context


def test_value_capture_only_research_reaches_catalog_and_selection_without_adoption(tmp_path):
    app, runs = tmp_path / "app", tmp_path / "runs"
    sources = [_source("unrelated"), _source("industry", "2026-08-31"), _source("company")]
    note = {
        "text": "外部の需要増→会社の数量/売価→追加費用・必要投資後の普通株cash。係数は未確認。",
        "source_ids": ["industry", "company"],
    }
    submitted, record, context = _save(app, note, sources)
    assert not record.document.hypotheses
    assert not record.document.unresolved_questions
    original = context.model_dump_json()
    selection = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    selection_digest = selection_sha256(selection)
    before = app.read_bytes(), runs.read_bytes()

    brief = research_catalog(app, as_of=NOW.date(), known_at=NOW)["items"][0]["research"]
    assert brief["value_capture_hypothesis"] == {
        "text": note["text"],
        "text_truncated": False,
        "sources": [
            {
                **{
                    key: source[key]
                    for key in ("source_id", "source_tier", "as_of", "retrieved_at")
                },
                "same_external_ref_source_ids": [
                    item["source_id"] for item in sources if item != source
                ],
                "same_external_ref_sources_omitted": 0,
            }
            for source in sources[1:]
        ],
        "sources_omitted": 0,
    }
    assert brief["role"] == "unreviewed_research"
    assert brief["reference"] == record.reference.model_dump(mode="json")
    assert brief["hypotheses"] == []
    assert brief["unresolved_questions"] == []
    assert "exact reference" in brief["display"]
    assert not {"valuation", "stance", "adoption_status"} & brief.keys()

    selected = load_generated_selection_context(
        request=selection.request,
        generation_id=selection.inputs.generation.generation_id,
        screening_selection_id=selection.inputs.screening_selection_id,
        research_as_of=NOW.date(),
        known_at=NOW,
        runs_path=runs,
        db_path=app,
        comparison_tickers=("1234",),
    )
    assert selected["saved_company_context"]["1234"]["research"] == brief
    assert selected["candidate_tickers"] == []
    assert (
        research_catalog(app, as_of=NOW.date(), known_at=NOW - timedelta(seconds=1))["items"] == []
    )
    assert (
        ResearchStoreService(app, clock=lambda: NOW + timedelta(days=1)).save(submitted) == record
    )
    with closing(connect_read_only(app)) as connection:
        assert load_research(connection, record.reference.research_id) == record
        assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 0
    assert context.model_dump_json() == original
    assert selection_sha256(selection) == selection_digest
    assert (app.read_bytes(), runs.read_bytes()) == before


def test_value_capture_excerpt_and_citations_are_bounded_but_exact_record_is_not(tmp_path):
    sources = [_source(f"s{n}") for n in range(5)]
    text = "数量増\n→費用と必要資金\u2028→普通株cash。" * 40 + "未確認の限定は末尾にも残る"
    _, record, context = _save(
        tmp_path / "app", {"text": text, "source_ids": [s["source_id"] for s in sources]}, sources
    )
    original = context.model_dump_json()
    brief = company_context_brief(context)["research"]["value_capture_hypothesis"]
    assert brief["text"] == text[:400]
    assert brief["text_truncated"] is True
    assert [item["source_id"] for item in brief["sources"]] == ["s0", "s1", "s2"]
    assert brief["sources_omitted"] == 2
    brief["sources"][0]["as_of"] = "1900-01-01"
    assert record.document.value_capture_hypothesis.text == text
    assert context.model_dump_json() == original


@pytest.mark.parametrize("explicit_none", [False, True])
def test_legacy_partial_research_without_value_capture_has_unchanged_brief(tmp_path, explicit_none):
    document = {
        "ticker": "1234",
        "as_of": "2026-10-14",
        "investigated_scope": "未取得の市場条件を今後確認する",
        "unresolved_questions": ["転嫁条件は未確認"],
    }
    if explicit_none:
        document["value_capture_hypothesis"] = None
    record = ResearchStoreService(tmp_path / "app", clock=lambda: NOW).save(
        ResearchDraft.model_validate({"research_id": "legacy-partial", "document": document})
    )
    brief = research_catalog(tmp_path / "app", as_of=NOW.date(), known_at=NOW)["items"][0][
        "research"
    ]
    assert brief == {
        "status": "available",
        "role": "unreviewed_research",
        "reference": record.reference.model_dump(mode="json"),
        "as_of": "2026-10-14",
        "recorded_at": NOW.isoformat(),
        "investigated_scope": document["investigated_scope"],
        "unresolved_questions": document["unresolved_questions"],
        "hypotheses": [],
        "source_dates": [],
        "display": "brief_only; use exact reference for all evidence and counterarguments",
    }
