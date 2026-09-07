"""Thesis v4 fixture authoring; expected values are independent literals."""

from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash


def pair_payload(*, ticker="1234", as_of="2026-09-07", quote_as_of="2026-09-04"):
    from datetime import date

    date.fromisoformat(as_of)
    date.fromisoformat(quote_as_of)
    axes = (
        "funding_liquidity",
        "debt_repayment",
        "cash_flow",
        "dilution",
        "customer_concentration",
        "structural_decline",
        "governance_accounting",
    )
    thesis = {
        "schema_version": 4,
        "input_snapshot": {
            "snapshot_version": 1,
            "producer_model_version": "research-v4",
            "ticker": "1234",
            "company_name": "架空会社",
            "sector": "機械",
            "common_factors": [],
            "as_of": "2026-09-07",
            "sources": [
                {
                    "source_id": "ir",
                    "ticker": "1234",
                    "source_tier": "primary",
                    "ref": "https://example.com/ir",
                    "retrieved_at": "2026-09-07T15:00:00+09:00",
                    "as_of": "2026-09-04",
                    "used_for": "事業と価格",
                }
            ],
            "facts": [
                {
                    "fact_id": "price",
                    "fact_kind": "market_price",
                    "value": 1000,
                    "unit": "JPY_per_share",
                    "as_of": "2026-09-04",
                    "source_ids": ["ir"],
                    "observed_at": "2026-09-04T15:30:00+09:00",
                    "price_basis": "last_close_unadjusted",
                }
            ],
        },
        "derived": {"metrics": []},
        "valuation": {
            "status": "resolved",
            "market_price_fact_id": "price",
            "horizon_months": 12,
            "required_annual_return_pct": 12,
            "base": {
                "terminal_value_per_share_yen": 1204,
                "cash_distribution_per_share_yen": 30,
                "calculation": "(30億円×6%−800万円)×70%÷100万株×PER10=1204円、分配後価値",
                "source_ids": ["ir"],
            },
            "downside": {
                "terminal_value_per_share_yen": 600,
                "cash_distribution_per_share_yen": 0,
                "calculation": "受注喪失で株主利益6000万円、100万株、PER10、無配",
                "source_ids": ["ir"],
            },
            "unresolved_reason": None,
        },
        "investment_case": {
            "explanation": "受注回復が価格へ未反映",
            "invalidation_conditions": ["主要契約喪失"],
            "status": "intact",
            "status_reason": "契約維持を確認",
            "source_ids": ["ir"],
        },
        "permanent_loss_risks": [
            {
                "axis": axis,
                "assessment": "acceptable",
                "evidence_status": "verified",
                "summary": "一次資料で確認",
                "as_of": "2026-09-04",
                "source_ids": ["ir"],
            }
            for axis in axes
        ],
        "judgment": {
            "disposition": "candidate",
            "proposed_at": "2026-09-07T16:00:00+09:00",
            "strongest_countercase": "回復遅延で資金調達が必要",
        },
    }
    review = {
        "review_id": "review-1",
        "reviewer_role": "independent_second_pass",
        "reviewer_identity": "independent",
        "reviewed_at": "2026-09-07T17:00:00+09:00",
        "reviewed_thesis_sha256": thesis_core_hash(ThesisDocument.model_validate(thesis)),
        "primary_source_check": "verified",
        "checked_source_ids": ["ir"],
        "recalculated_projections": [
            {
                "name": "base",
                "terminal_value_per_share_yen": 1204,
                "cash_distribution_per_share_yen": 30,
            },
            {
                "name": "downside",
                "terminal_value_per_share_yen": 600,
                "cash_distribution_per_share_yen": 0,
            },
        ],
        "strongest_countercase": "顧客の設備計画が遅延すれば利益回復せず",
    }
    import json

    thesis = json.loads(
        json.dumps(thesis)
        .replace("1234", ticker)
        .replace("2026-09-07", as_of)
        .replace("2026-09-04", quote_as_of)
    )
    review = json.loads(json.dumps(review).replace("2026-09-07", as_of))
    review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
    return thesis, review
