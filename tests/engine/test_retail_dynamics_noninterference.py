"""External observations must not change the existing Screening/FV calculation."""

from datetime import UTC, datetime
from unittest.mock import patch

from tests.helpers.screening_sqlite import (
    CALIBRATION_FIXTURE_ASOF,
    build_calibration_fixture_sqlite,
)

from baibai_engine.market.retail_dynamics.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
)
from baibai_engine.market.retail_dynamics.reader import read_observations
from baibai_engine.market.retail_dynamics.store import save_observations
from baibai_engine.screening.calibration.panel import build_panel
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.security_analysis_builder import build_security_analysis


def test_external_observations_leave_existing_screening_and_fv_identical(tmp_path):
    database = tmp_path / "market.sqlite"
    build_calibration_fixture_sqlite(database)
    rules = load_screening_rules()
    received = datetime(2026, 6, 30, 0, 0, tzinfo=UTC)
    analyses = []

    def capture_analysis(**inputs):
        analysis = build_security_analysis(**inputs)
        analyses.append(analysis)
        return analysis

    with patch(
        "baibai_engine.screening.calibration.panel.build_security_analysis",
        side_effect=capture_analysis,
    ):
        before = build_panel(CALIBRATION_FIXTURE_ASOF, sqlite_path=database, rules=rules)
        previous_analyses = tuple(analyses)
        assert previous_analyses
        assert any(
            analysis.metrics["fv_sector_median_yen"] is not None for analysis in previous_analyses
        )
        analyses.clear()
        valuation = ModelValuationObservation(
            ticker="9001",
            fetched_at_utc=received,
            distributor="monex",
            source_url="https://example.invalid/model/9001",
            parser_version="synthetic-noninterference-1",
            consensus_target=9_999_999,
            per_company_forecast=8_888_888,
            per_consensus=7_777_777,
            pbr=6_666_666,
        )
        rating = AnalystRatingObservation(
            ticker="9001",
            fetched_at_utc=received,
            distributor="rakuten",
            source_url="https://example.invalid/rating/9001",
            parser_version="synthetic-noninterference-1",
            rating=1,
        )
        assert save_observations(database, [valuation, rating]) == 2
        history = read_observations(database, "9001", received)
        assert history.model_valuation.latest == valuation
        assert history.analyst_rating.latest == rating
        after = build_panel(CALIBRATION_FIXTURE_ASOF, sqlite_path=database, rules=rules)

    assert after == before
    assert tuple(analyses) == previous_analyses
