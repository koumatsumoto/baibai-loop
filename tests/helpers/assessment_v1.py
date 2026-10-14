"""Stock Assessment v1: issue #1533 appendix, entirely synthetic inputs."""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from baibai_engine.research.assessment import StockAssessmentDocument, assessment_core_hash

APPENDIX_ASSESSMENT_PUBLICATION = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/domain-assessment-proposal-synthetic.json"
    ).read_text(encoding="utf-8")
)["assessment_publication"]


def assessment_id(number=1):
    return f"stock-assessment-00000000-0000-4000-8000-{number:012x}"


def review_id(number=2):
    return f"assessment-review-00000000-0000-4000-8000-{number:012x}"


def pair_payload(*, ticker="1234", as_of="2026-10-14", quote_as_of=None, review_number=2):
    document = deepcopy(APPENDIX_ASSESSMENT_PUBLICATION["document"])
    review = deepcopy(APPENDIX_ASSESSMENT_PUBLICATION["review"])
    snapshot = document["input_snapshot"]
    snapshot["ticker"] = ticker
    snapshot["as_of"] = as_of
    quote_as_of = quote_as_of or as_of
    for source in snapshot["sources"]:
        source["ticker"] = ticker
        source["as_of"] = quote_as_of if source["source_id"] == "price" else as_of
        source["retrieved_at"] = f"{as_of}T16:00:00+09:00"
    for fact in snapshot["facts"]:
        fact["as_of"] = quote_as_of
        fact["observed_at"] = f"{quote_as_of}T15:30:00+09:00"
    for risk in document["permanent_loss_risks"]:
        risk["as_of"] = as_of
    document["conclusion"]["assessed_at"] = f"{as_of}T16:00:00+09:00"
    review["review_id"] = review_id(review_number)
    review["reviewed_at"] = f"{as_of}T16:05:00+09:00"
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(document)
    )
    return document, review


def update_review_hash(document, review):
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(document)
    )


NOW = datetime.fromisoformat("2026-10-14T16:06:00+09:00")
