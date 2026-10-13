"""候補外の保存済み機械推計も、説明を失わずWebへ届く。"""

import json

from fastapi.testclient import TestClient
from tests.conftest import _seed_screening_run

from baibai_engine.screening.run_store import ScreeningRunStore
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models


def test_machine_estimate_metadata_reaches_list_detail_history_and_export(
    app_method_root, tmp_path
):
    run = _seed_screening_run("2026-07-10")
    analysis = next(item for item in run["security_analyses"] if item["ticker"] == "0001")
    metadata = {
        "er_model_version": "synthetic-model",
        "er_unit": "ratio_per_year",
        "er_anchor_metrics": "PBR+PER",
        "er_assumptions": "synthetic: fixed realization and cap",
    }
    analysis["metrics"].update(metadata)
    store = app_method_root / "stores/screening/runs.sqlite"
    ScreeningRunStore(store).publish_run(run)
    before = store.read_bytes()
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        latest = client.get("/api/screening/latest").json()
        history = client.get("/api/screening/history/2026-07-10").json()
        detail = client.get("/api/securities/0001").json()["security_analysis"]
    assert latest["review_sets"] == []
    for rows in (latest["security_analyses"], history["rows"], [detail]):
        row = next(item for item in rows if item["ticker"] == "0001")
        assert {key: row[key] for key in metadata} == metadata
        assert row["er_annual"] == 0.1
    out = tmp_path / "export"
    export_read_models(app_method_root, out)
    saved = json.loads((out / "views/security--0001.json").read_text())
    assert saved["security_analysis"] == detail
    assert store.read_bytes() == before
