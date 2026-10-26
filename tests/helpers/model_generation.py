"""五モデルと候補抽出の固定合成入力。実企業・実判断ではない。"""

import sqlite3
from contextlib import closing
from datetime import timedelta

from baibai_engine.machine_analysis.company_growth.models import CompanyGrowth
from baibai_engine.machine_analysis.generation_store import ModelGenerationStore
from baibai_engine.machine_analysis.model_generation import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.model_projection import select_generation
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.machine_analysis.screening.views import TwoViewPolicy


def seed_generation(path, *, now, as_of, with_price_path=False):
    payloads = {
        "company_valuation": {"earnings_price": {"earnings_yield": 0.12}, "financial": None},
        "technical_analysis": {"price_momentum": {"value": None, "reason": "missing_endpoint"}},
        "company_growth": CompanyGrowth(
            ticker="1234", status="unavailable", requested_start=None, requested_end=as_of
        ).model_dump(mode="json"),
        "external_assessment": {"forecast": None, "reason": "not_covered"},
        "market_context": {"price": {"avg_turnover_20d_oku": 1.25}},
    }
    if with_price_path:
        payloads["technical_analysis"]["price_path"] = {
            "calculation_revision": "synthetic-price-path",
            "annotation": {"status": "matched", "matched_refs": ["G60"]},
        }
        payloads["market_context"]["price_path_relative"] = {"comparisons": {}}
    models = [
        GeneratedModel(
            model_id=name, subject="1234", calculation_revision="synthetic-v1", payload=payload
        )
        for name, payload in payloads.items()
    ]
    models.append(
        GeneratedModel(
            model_id="market_context",
            subject="market",
            calculation_revision="synthetic-v1",
            payload={"reading": {"rates": 0.02}},
        )
    )
    generation = ModelGeneration(
        generation_id="fixed-synthetic",
        as_of=as_of,
        evaluation_time=now - timedelta(minutes=1),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="synthetic", sha256="a" * 64)
        },
        models=tuple(models),
    )
    ModelGenerationStore(path).publish(generation)
    ScreeningSelectionStore(path).publish(
        "policy-1",
        select_generation(
            generation,
            tickers=("1234", "5678"),
            policy=TwoViewPolicy(earnings_limit=20, combined_limit=20),
        ),
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET published_at=?",
            ((now - timedelta(seconds=30)).isoformat(),),
        )
    return generation
