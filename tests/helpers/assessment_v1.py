"""Synthetic Stock Assessment and independent Review inputs."""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)

APPENDIX_ASSESSMENT_PUBLICATION = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/domain-assessment-proposal-synthetic.json"
    ).read_text(encoding="utf-8")
)["assessment_publication"]


def assessment_id(number=1):
    return f"stock-assessment-00000000-0000-4000-8000-{number:012x}"


def review_id(number=2):
    return f"assessment-review-00000000-0000-4000-8000-{number:012x}"


def pair_payload(
    *,
    ticker="1234",
    as_of="2026-10-14",
    quote_as_of=None,
    review_number=2,
    price_case="supported_at_quote",
):
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
    if price_case == "undetermined":
        document["price_value_case"] = undetermined_case()
    elif price_case is not None:
        # Newly authored synthetic pairs have an independent receipt; the original appendix stays exact.
        snapshot["facts"].append(
            {
                "fact_id": "owner_receipt",
                "fact_kind": "other",
                "value": 120,
                "unit": "JPY_per_share",
                "observed_at": None,
                "price_basis": None,
                "as_of": as_of,
                "source_ids": ["annual"],
            }
        )
        if price_case == "supported_at_quote":
            # The archived Base is not evidence of cheapness; this is an added, synthetic third path.
            document["investment_case"]["explanation"] = (
                "合成例：継続事業に加え、契約製品の採用時だけ税・追加投資控除後の一株純額が残る。"
                "権利は普通株に帰属し、不採用時は継続事業だけに戻る。"
            )
            document["findings"]["value_gap_explanation"] = (
                "継続事業の中心だけでは要求率に届かない。"
                "契約先の検収記録から別経路の普通株への見返りを支持する。"
            )
        document["price_value_case"] = {
            "status": price_case,
            "price_compatible_conditions": "合成例：現値を正当化するには継続事業の維持が必要。追加契約の寄与ゼロでも説明できるか比較した。",
            "value_fact_ids": ["owner_receipt"],
            "price_only_condition": None,
        }
    for risk in document["permanent_loss_risks"]:
        risk["as_of"] = as_of
    document["conclusion"]["assessed_at"] = f"{as_of}T16:00:00+09:00"
    review["review_id"] = review_id(review_number)
    review["reviewed_at"] = f"{as_of}T16:05:00+09:00"
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(document)
    )
    return document, review


def undetermined_case():
    return dict(
        status="undetermined",
        price_compatible_conditions=None,
        value_fact_ids=[],
        price_only_condition=None,
    )


def update_review_hash(document, review):
    review["reviewed_assessment_sha256"] = assessment_core_hash(
        StockAssessmentDocument.model_validate(document)
    )


NOW = datetime.fromisoformat("2026-10-14T16:06:00+09:00")
