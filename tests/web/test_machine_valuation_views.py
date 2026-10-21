"""候補外の保存済み機械推計も、説明を失わずWebへ届く。"""

import json

import pytest
from fastapi.testclient import TestClient
from tests.conftest import _seed_screening_run

from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_engine.valuation.estimates import EXPECTED_RETURN_MODEL_VERSION
from baibai_engine.valuation.query import CARRY_MISSING_ASSUMPTION
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models


@pytest.mark.parametrize("component", [None, 0.0])
def test_machine_estimate_metadata_reaches_list_detail_history_and_export(
    app_method_root, tmp_path, component
):
    run = _seed_screening_run("2026-07-10")
    analysis = next(item for item in run["stock_analyses"] if item["ticker"] == "0001")
    metadata = {
        "er_model_version": EXPECTED_RETURN_MODEL_VERSION,
        "er_unit": "ratio_per_year",
        "er_anchor_metrics": "PBR+PER",
        "er_assumptions": "synthetic: fixed realization and cap",
    }
    analysis["metrics"].update(metadata)
    components = {
        "dividend_yield": component,
        "dividend_basis": "unavailable" if component is None else "actual_reported",
        "net_share_change_yoy": component,
    }
    analysis["metrics"].update(components)
    store = app_method_root / "stores/screening/runs.sqlite"
    ScreeningRunStore(store).publish_run(run, rules=load_screening_rules())
    before = store.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        latest = client.get("/api/screening/latest").json()
        history = client.get("/api/screening/history/2026-07-10").json()
        detail = client.get("/api/securities/0001").json()["stock_analysis"]
    assert latest["candidate_discovery"]["candidate_count"] == 0
    for rows in (latest["stock_analyses"], history["rows"], [detail]):
        row = next(item for item in rows if item["ticker"] == "0001")
        assert {key: row[key] for key in metadata} == metadata
        assert {key: row[key] for key in components} == components
        assert row["carry_missing_assumption"] == CARRY_MISSING_ASSUMPTION
        assert row["er_annual"] == 0.1
    out = tmp_path / "export"
    export_read_models(app_method_root, out)
    saved = json.loads((out / "views/security--0001.json").read_text())
    assert saved["stock_analysis"] == detail
    assert store.read_bytes() == before
