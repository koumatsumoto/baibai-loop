"""新規Researchの観測時計と、旧snapshotの保存・再開契約。"""

from contextlib import closing
from datetime import datetime, timedelta

import pytest
from tests.helpers.assessment_v1 import pair_payload, update_review_hash
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.investment_judgment.company_research.document import (
    ResearchDraft,
    research_sha256,
)
from baibai_engine.investment_judgment.company_research.store import (
    ResearchStoreService,
    load_research,
)
from baibai_engine.investment_judgment.independent_review.contracts import AssessmentReview
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    UnpublishedAssessment,
    evaluate_assessment,
)
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import selection_sha256
from baibai_engine.investment_judgment.workflow.workspace import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")
OBSERVED = "2026-10-14T15:30:00+09:00"
BEFORE = "2026-10-14T15:00:00+09:00"


def price_case(retrievals, observed_at=OBSERVED):
    document, review = pair_payload()
    snapshot = document["input_snapshot"]
    price = next(source for source in snapshot["sources"] if source["source_id"] == "price")
    price["retrieved_at"] = retrievals[0]
    fact = snapshot["facts"][0]
    fact["observed_at"] = observed_at
    for index, retrieved_at in enumerate(retrievals[1:], 1):
        source_id = f"price-{index}"
        snapshot["sources"].append({**price, "source_id": source_id, "retrieved_at": retrieved_at})
        fact["source_ids"].append(source_id)
        review["checked_source_ids"].append(source_id)
    update_review_hash(document, review)
    draft = ResearchDraft(
        research_id="price-clock",
        document={
            "ticker": "1234",
            "as_of": snapshot["as_of"],
            "investigated_scope": "合成価格の観測時刻",
            "sources": snapshot["sources"],
            "facts": snapshot["facts"],
        },
    )
    return draft, document, review


@pytest.mark.parametrize(
    ("retrievals", "observed_at", "accepted"),
    [
        ((BEFORE,), OBSERVED, False),
        ((BEFORE, "2026-10-14T15:15:00+09:00"), OBSERVED, False),
        ((OBSERVED,), OBSERVED, True),
        (("2026-10-14T16:00:00+09:00",), OBSERVED, True),
        ((BEFORE, OBSERVED), OBSERVED, True),
        (("2026-10-14T06:30:00+00:00", BEFORE), OBSERVED, True),
        ((BEFORE,), None, True),
    ],
    ids=["before", "all-before", "equal", "after", "one-supports", "utc-equal", "date-only"],
)
def test_new_research_price_clock_matches_assessment(tmp_path, retrievals, observed_at, accepted):
    draft, document, review = price_case(retrievals, observed_at)
    evaluation = evaluate_assessment(
        StockAssessmentDocument.model_validate(document),
        review=AssessmentReview.model_validate(review),
        identity=UnpublishedAssessment.DRAFT,
        now=NOW,
    )
    error = "market_price close postdates all cited source retrievals"
    assert evaluation.errors == (() if accepted else (error,))
    path = tmp_path / "app"
    service = ResearchStoreService(path, clock=lambda: NOW)
    if accepted:
        assert service.save(draft).document == draft.document
    else:
        with pytest.raises(ValueError, match=error):
            service.save(draft)
        with closing(connect_read_only(path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM company_research").fetchone()[0] == 0


def test_legacy_price_clock_read_retry_and_fixed_context_are_unchanged(tmp_path):
    draft, _, _ = price_case((BEFORE,))
    path, runs = tmp_path / "app", tmp_path / "runs"
    payload = canonical_json(draft.document.model_dump(mode="json"))
    digest = research_sha256(draft.document)
    assert digest == "1b44ad69cff2d3433b02fb2851b551dc7fbdef2212c8e4eae122e76128b90d3d"
    initialize_database(path)
    # A pre-guard publication, not a new save that bypasses the current contract.
    with closing(connect_rw(path)) as connection, connection:
        connection.execute(
            "INSERT INTO company_research(research_id,ticker,as_of,recorded_at,prior_research_id,"
            "payload,payload_sha256) VALUES (?,?,?,?,?,?,?)",
            (
                draft.research_id,
                "1234",
                NOW.date().isoformat(),
                NOW.isoformat(),
                None,
                payload,
                digest,
            ),
        )
    with closing(connect_read_only(path)) as connection:
        legacy = load_research(connection, draft.research_id)
    assert legacy.reference.payload_sha256 == digest
    before = path.read_bytes()
    assert ResearchStoreService(path, clock=lambda: NOW + timedelta(days=1)).save(draft) == legacy
    assert path.read_bytes() == before
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=path)
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(selected),
        db_path=path,
        workspace=tmp_path / "work",
        runs_path=runs,
        now=NOW,
    )
    saved = selection_binding(OperationService(path).get(prepared.operation_id).payload)
    assert saved.inputs.saved_company_context["1234"].research.record == legacy
    fixed_hash = selection_sha256(saved)
    display = (prepared.workspace / "domain-inputs.yaml").read_bytes()
    before = path.read_bytes()
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=path,
        workspace=prepared.workspace,
        now=NOW,
    )
    assert resumed.selection_sha256 == fixed_hash
    assert (prepared.workspace / "domain-inputs.yaml").read_bytes() == display
    assert path.read_bytes() == before
    new_revision = draft.model_copy(
        update={"research_id": "new-price-clock", "prior_research_ref": legacy.reference}
    )
    with pytest.raises(ValueError, match="postdates all cited source retrievals"):
        ResearchStoreService(path, clock=lambda: NOW + timedelta(seconds=1)).save(new_revision)
    assert path.read_bytes() == before
