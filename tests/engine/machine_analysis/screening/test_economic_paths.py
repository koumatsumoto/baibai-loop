"""旧経済政策の保存型を読む。廃止した順位計算を再実行しない。"""

import gzip
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.helpers.model_generation import generation, publish_generation
from tests.helpers.retained_economic_selection import retained_economic_result

from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
)
from baibai_engine.machine_analysis.screening.policies.two_views import ViewSelection
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json


@pytest.mark.parametrize("invalid", [-1, 37, True])
def test_saved_candidate_limit_remains_a_finite_attention_cap(invalid):
    with pytest.raises(ValidationError):
        EconomicPathPolicy(candidate_limit=invalid)


def test_saved_card_evidence_context_and_missing_models_round_trip():
    result = retained_economic_result()
    card = result.cards[0]
    evidence = {
        "period": {"start": "2025-04-01", "end": "2026-03-31"},
        "price_date": "2026-10-28",
        "missing": None,
        "source": ["reported", {"document_id": "fixture"}],
    }
    path = replace(card.paths[0], price_value=0.100049, business_value=0.25, evidence=evidence)
    context = {
        "reported_loss": -50,
        "cash_is_not_surplus": True,
        "future_model": {"detail_reference": "fixed-generation"},
    }
    card = replace(
        card,
        paths=(path,),
        context=context,
        model_revisions={f"model_{i}": "v1" for i in range(7)},
        not_generated=("model_8",),
    )
    selected = ScreeningSelection(
        "fixed-generation", (card.ticker,), replace(result, cards=(card,))
    )
    codec = TypeAdapter(ScreeningSelection)
    restored = codec.validate_json(codec.dump_json(selected))
    assert restored == selected
    assert isinstance(restored.result, EconomicPathSelection)
    saved = restored.result.cards[0]
    assert saved.paths[0].price_value == 0.100049
    assert saved.paths[0].normalization is not None
    assert saved.paths[0].normalization.population == 1
    assert saved.paths[0].normalization.midrank == 1.0
    assert saved.context == context
    assert saved.paths[0].evidence == evidence
    assert saved.not_generated == ("model_8",)


@pytest.mark.parametrize("invalid", [True, float("nan"), float("inf")])
def test_saved_card_coordinates_must_remain_finite_numbers(invalid):
    codec = TypeAdapter(ScreeningSelection)
    payload = codec.dump_python(
        ScreeningSelection("g", ("1234",), retained_economic_result()), mode="json"
    )
    payload["result"]["cards"][0]["paths"][0]["price_value"] = invalid
    with pytest.raises(ValidationError):
        codec.validate_python(payload)


@pytest.mark.parametrize(
    "method",
    [
        "earnings-price-views-v1",
        "best-economic-path-v1",
        "best-economic-path-v2",
        "best-economic-path-v3",
        "best-economic-path-v4",
    ],
)
def test_historical_selection_payload_keeps_shape_and_idempotent_publication(tmp_path, method):
    path = tmp_path / "runs.sqlite"
    fixed = generation()
    publish_generation(fixed, path=path)
    # 合成した固定保存契約fixture。本番履歴ではなく、現行計算・codecから生成しない。
    # 新しい既定fieldに合わせて更新せず、旧bytesを維持できるかを検証する。
    result = {
        "policy": {
            "method": "earnings-price-views-v1",
            "earnings_limit": 1,
            "combined_limit": 1,
            "earnings_scale": 0.1,
            "momentum_scale": 0.5,
            "earnings_weight": 0.6,
        },
        "views": [
            {
                "view_id": "earnings-price",
                "limit": 1,
                "eligible_count": 1,
                "companies": [{"ticker": "1001", "rank": 1, "ranking_value": 0.1}],
            },
            {
                "view_id": "earnings-price-response",
                "limit": 1,
                "eligible_count": 1,
                "companies": [{"ticker": "1001", "rank": 1, "ranking_value": -0.123456}],
            },
        ],
    }
    if method != "earnings-price-views-v1":
        result = {
            "policy": {"method": method, "candidate_limit": 36},
            "eligible_count": 1,
            "cards": [
                {
                    "ticker": "1001",
                    "name": "1001",
                    "sector": "sector",
                    "rank": 1,
                    "priority": 0.5,
                    "strongest_path": "business_expansion",
                    "paths": [
                        {
                            "path_id": "business_expansion",
                            "variant": "quarter",
                            "price_value": 0.100049,
                            "business_value": 0.2,
                            "price_group": "sector",
                            "business_group": "sector",
                            "price_percentile": 0.5,
                            "business_percentile": 0.5,
                            "price_population": 1,
                            "business_population": 1,
                            "score": 0.5,
                            "evidence": {},
                        }
                    ],
                    "context": {},
                    "model_revisions": {},
                    "not_generated": [],
                }
            ],
        }
    if method == "best-economic-path-v4":
        result["cards"][0]["paths"][0]["normalization"] = {
            "percentile": 0.5,
            "population": 1,
            "midrank": 1.0,
        }
    expected = {
        "generation_id": "synthetic-generation",
        "universe_tickers": ["1001"],
        "result": result,
    }
    payload = canonical_json(expected).encode()
    compressed = gzip.compress(payload, mtime=0)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO screening_selection VALUES (?,?,?,?)",
            (
                "selection",
                fixed.generation_id,
                "2026-10-29T00:00:00+00:00",
                compressed,
            ),
        )
    store = ScreeningSelectionStore(path)
    restored = store.read("selection")
    assert restored is not None
    assert isinstance(
        restored.result,
        ViewSelection if method == "earnings-price-views-v1" else EconomicPathSelection,
    )
    assert restored.result.tickers == ("1001",)
    codec = TypeAdapter(ScreeningSelection)
    assert canonical_json(codec.dump_python(restored, mode="json")).encode() == payload
    assert not store.publish("selection", restored)
    with closing(sqlite3.connect(path)) as connection:
        saved = connection.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id='selection'"
        ).fetchone()[0]
    assert saved == compressed
    assert gzip.decompress(saved) == payload
