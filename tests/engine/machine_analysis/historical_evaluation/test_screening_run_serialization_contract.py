from tests.helpers.screening_run import screening_run_payload, seed_saved_run, stock_analysis

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.run_store import (
    ScreeningRunReader,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.screening_run_serializer import (
    published_run_payload,
)


def test_saved_metric_serialization_preserves_types_without_another_rounding_pass(tmp_path):
    metrics = {"ratio": 1.234567, "count": 3, "known": True, "missing": None, "basis": "raw"}
    path = tmp_path / "runs.sqlite"
    seed_saved_run(
        path,
        screening_run_payload(stock_analyses=[stock_analysis("7203", metrics=metrics)]),
        run_revision_id="retained",
    )
    before = path.read_bytes()
    run = ScreeningRunReader(path).get_run("retained")
    assert run is not None
    assert published_run_payload(run)["stock_analyses"][0]["metrics"] == metrics
    assert path.read_bytes() == before
