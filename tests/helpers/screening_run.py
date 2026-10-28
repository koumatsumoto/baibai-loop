"""旧保存runの明示fixture。現在の候補政策や新規公開writerを再現しない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from typing import Any

from baibai_engine.primitives.json import canonical_json
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.contracts import APPROACH_IDS
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import load_screening_rules
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rules_identity import production_rules_contract_hash
from baibai_engine.storage.sqlite.machine_analysis import initialize_run_store


class _Omitted:
    """Distinguishes "not asked for" from an explicit `None`."""


OMIT = _Omitted()

RULES_HASH = production_rules_contract_hash(load_screening_rules().model_dump_json())
MODEL_ID = "expected-return-v1"


def stock_analysis(
    ticker: str = "1301",
    *,
    name: str = "極洋",
    sector_33: str = "水産・農林業",
    market_cap_oku: float = 1000.0,
    avg_turnover_oku: float = 10.0,
    price_history_span_days: int = 1000,
    jpx_flags: Sequence[str] = (),
    metrics: Mapping[str, Any] | _Omitted | None = OMIT,
    **extra: Any,
) -> dict[str, Any]:
    """One Stock Analysis entry of a Screening Run payload.

    `metrics` has three states the store distinguishes and so does this: left out it
    carries the default reading, `{}` carries none, and `None` leaves the key off the
    payload entirely — which is a shape the store accepts and a reader has to survive.
    """

    payload: dict[str, Any] = {
        "ticker": ticker,
        "name": name,
        "sector_33": sector_33,
        "market_cap_oku": market_cap_oku,
        "avg_turnover_oku": avg_turnover_oku,
        "price_history_span_days": price_history_span_days,
        "jpx_flags": list(jpx_flags),
        **extra,
    }
    if metrics is OMIT:
        payload["metrics"] = {"er_annual": 0.13}
    elif metrics is not None:
        payload["metrics"] = dict(metrics)
    return payload


def screening_run_payload(
    *,
    as_of: str = "2026-07-08",
    run_at: str | None = None,
    run_id: str | None = None,
    universe_size: int = 3744,
    rules_hash: str = RULES_HASH,
    model_id: str = MODEL_ID,
    stock_analyses: Sequence[Mapping[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """指定as-ofの保存形式fixture。新しい旧runをproductionへ公開しない。"""

    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "stock_analysis_run",
        "run_id": run_id or f"screening-{as_of.replace('-', '')}",
        "run_date": as_of,
        "asof_date": as_of,
        "run_at": run_at or f"{as_of}T18:00:00+09:00",
        "universe_size": universe_size,
        "screening_rules_hash": rules_hash,
        "er_model_version": model_id,
        "stock_analyses": [
            dict(analysis)
            for analysis in ([stock_analysis()] if stock_analyses is None else stock_analyses)
        ],
    }
    payload.update(extra)
    return payload


def saved_candidate(*, eligible=True, rank=None):
    """明示された履歴候補を表すだけで、metricsから順位や適格性を決めない。"""
    return {
        "common_eligible": eligible,
        "normalized_per_3fy_sector_gap": None,
        "reasons": []
        if rank is None
        else [
            {
                "valuation_approach_id": approach,
                "valuation_method_id": f"synthetic-{approach}",
                "rank": rank,
            }
            for approach in APPROACH_IDS
        ],
    }


def seed_saved_run(path, payload, *, run_revision_id):
    """すでに確定した履歴行をSQL seedする。idempotencyや再選定は持たない。"""
    rows = [
        {**row, "candidate": row.get("candidate", saved_candidate())}
        for row in payload["stock_analyses"]
    ]
    header = {
        key: value
        for key, value in payload.items()
        if key not in {"stock_analyses", "run_revision_id"}
    }
    header.setdefault(
        "candidate_discovery",
        {
            "method": {
                "method_id": "synthetic-retained",
                "method_hash": "a" * 64,
                "nomination_depth": 40,
            },
            "diagnostics": {
                "common_eligible_count": sum(row["candidate"]["common_eligible"] for row in rows),
                "candidate_count": sum(bool(row["candidate"]["reasons"]) for row in rows),
                "candidate_counts_by_approach": {
                    approach: sum(
                        any(
                            reason["valuation_approach_id"] == approach
                            for reason in row["candidate"]["reasons"]
                        )
                        for row in rows
                    )
                    for approach in APPROACH_IDS
                },
            },
        },
    )
    initialize_run_store(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO screening_run VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_revision_id,
                header["run_id"],
                header["run_date"],
                header["asof_date"],
                header["run_at"],
                header["universe_size"],
                header.get("rules_ref"),
                header["run_at"],
                canonical_json(header),
            ),
        )
        for ordinal, row in enumerate(rows):
            metrics = row.get("metrics") or {}
            connection.execute(
                "INSERT INTO stock_analysis VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_revision_id,
                    ordinal,
                    row["ticker"],
                    row.get("sector_33"),
                    row.get("per_forward"),
                    row.get("per_trailing"),
                    row.get("pbr"),
                    metrics.get("dividend_yield"),
                    metrics.get("er_annual"),
                    canonical_json(row),
                ),
            )
    return run_revision_id
