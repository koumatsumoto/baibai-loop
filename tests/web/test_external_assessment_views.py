"""保存済み外部値だけで候補外の銘柄detailとserving exportへ到達できる。"""

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from tests.engine.test_external_assessment_core import valuation

from baibai_engine.external_assessment.store import save_observations
from baibai_engine.read_api import MARKET_DB_PATH
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models
from baibai_web.sources.db_sources import DbExternalAssessmentSource


def test_external_only_ticker_is_available_in_api_and_materialized_export(
    app_method_root, tmp_path, monkeypatch
):
    observation = valuation(
        datetime(2026, 10, 10, 7, tzinfo=UTC),
        ticker="9999",
        consensus_target=1500,
        share_basis=None,
    )
    save_observations(app_method_root / MARKET_DB_PATH, [observation])
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/securities/9999")
    assert response.status_code == 200
    detail = response.json()
    assert detail["security_analysis"] is None
    assert (
        detail["external_assessment"]["model_valuations"][0]["inputs"]["latest"]["consensus_target"]
        == 1500
    )
    assert (
        detail["external_assessment"]["model_valuations"][0]["inputs"]["latest"]["share_basis"]
        is None
    )
    out = tmp_path / "export"
    cutoffs = []
    original_tickers = DbExternalAssessmentSource.observed_tickers
    original_assessment = DbExternalAssessmentSource.assessment

    def observed_tickers(self, *, cutoff):
        cutoffs.append(cutoff)
        return original_tickers(self, cutoff=cutoff)

    def assessment(self, ticker, *, cutoff):
        assert cutoffs
        assert cutoff == cutoffs[0]
        return original_assessment(self, ticker, cutoff=cutoff)

    monkeypatch.setattr(DbExternalAssessmentSource, "observed_tickers", observed_tickers)
    monkeypatch.setattr(DbExternalAssessmentSource, "assessment", assessment)
    export_read_models(app_method_root, out)
    saved = json.loads((out / "views/security--9999.json").read_text())
    assert (
        saved["external_assessment"]["model_valuations"]
        == detail["external_assessment"]["model_valuations"]
    )
    assert (
        saved["external_assessment"]["origin_groups"]
        == detail["external_assessment"]["origin_groups"]
    )
