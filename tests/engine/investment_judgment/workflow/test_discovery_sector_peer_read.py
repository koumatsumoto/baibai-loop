"""現在の候補照会で、保存された未分類群を同業の価格根拠にしない。"""

import json
from datetime import datetime
from unittest.mock import Mock

from tests.helpers.model_generation import generation, publish_generation

from baibai_engine.cli.investment_judgment import main
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.machine_analysis.screening import selection_store
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)

_AT = datetime.fromisoformat("2026-10-23T18:00:00+09:00")


def _market(ticker, sector):
    # 同じ端点の比較でも、業種そのものが未分類なら同業の根拠ではない。
    endpoints = {
        "comparison_start_date": "2026-09-28",
        "comparison_end_date": "2026-10-23",
    }
    return GeneratedModel(
        model_id="market_context",
        subject=ticker,
        calculation_revision="saved-market-fixture",
        payload={
            "master": {"sector_33": sector, "snapshot_date": _AT.date().isoformat()},
            "price": {"close": 820.0, "resolved_date": _AT.date().isoformat()},
            "price_path_relative": {
                "benchmark_ticker": "1321",
                "comparisons": {"R20": {**endpoints, "relative_return": -0.26}},
            },
            "relative": {
                "benchmark_ticker": "1321",
                "relative_20d": -0.26,
                "sector": {
                    **endpoints,
                    "sector_33": sector,
                    "peer_count": 1,
                    "peer_median_return_20d": 0.45,
                    "relative_return_20d": -0.63,
                },
            },
        },
    )


def test_public_next_page_detail_withholds_only_unclassified_peer(tmp_path, monkeypatch, capsys):
    runs, app = tmp_path / "runs", tmp_path / "app"
    stocks = tuple(str(t) for t in range(1001, 1027))
    unclassified, classified = _market("1025", "その他"), _market("1026", "その他製品")
    saved = ModelGeneration(
        schema_version=2,
        generation_id="fixed-peers",
        as_of=_AT.date(),
        evaluation_time=_AT,
        inputs=generation().inputs,
        subject_plan={
            t: GenerationSubject(scope="stock", model_ids=("market_context",)) for t in stocks
        },
        models=(unclassified, classified),
    )
    publish_generation(saved, path=runs)
    identity = discovery_publication_id(saved.generation_id)
    rows = {
        model.subject: project_discovery_row(model.subject, models=(model,), metadata=saved)
        for model in saved.models
    }
    selection = evaluate_discovery_rows(saved, tickers=stocks, rows=rows)
    with monkeypatch.context() as patch:
        clock = Mock(wraps=datetime)
        clock.now.return_value = _AT
        patch.setattr(selection_store, "datetime", clock)
        assert selection_store.ScreeningSelectionStore(runs).publish(identity, selection)
    original = runs.read_bytes()

    def cli(args):
        assert main(args, now=_AT) == 0
        return json.loads(capsys.readouterr().out)

    menu = cli(
        [
            "selection-context",
            "--scope",
            "discover_new",
            "--request",
            "当時の価格の独立比較",
            "--runs-path",
            str(runs),
            "--db",
            str(app),
        ]
    )
    pinned = menu["repeat_command_args"][1:]
    first = cli([*pinned, "--discovery-question", "universe"])
    second = cli([*pinned, *first["discovery"]["next_args"]])
    assert [r["ticker"] for r in first["discovery"]["rows"]] == list(stocks[:24])
    unclassified_row, classified_row = second["discovery"]["rows"]
    assert (unclassified_row["ticker"], classified_row["ticker"]) == ("1025", "1026")
    views = cli([*pinned, *unclassified_row["detail_args"], *classified_row["detail_args"]])
    assert views["screening_selection_id"] == first["screening_selection_id"] == identity
    securities = views["model_inputs"]["securities"]
    raw, known = (securities[t]["models"]["market_context"] for t in ("1025", "1026"))
    assert raw["master"] == unclassified.payload["master"]
    assert raw["price"] == unclassified.payload["price"]
    assert raw["price_path_relative"] == unclassified.payload["price_path_relative"]
    assert raw["relative"] == {
        **unclassified.payload["relative"],
        "sector": None,
        "sector_unavailable_reason": "unclassified_sector",
    }
    assert raw["generation_model_ref"] == {
        "generation_id": saved.generation_id,
        "subject": "1025",
        "model_id": "market_context",
    }
    assert known == classified.payload
    exact = read_research_model_inputs(saved.generation_id, targets=("1025",), path=runs)
    assert exact.result("market_context", "1025").payload == unclassified.payload
    assert runs.read_bytes() == original
    assert not app.exists()
