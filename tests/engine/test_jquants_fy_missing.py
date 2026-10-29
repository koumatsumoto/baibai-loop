"""FY provider reads must not turn missing latest earnings into older anchors."""

from datetime import date

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    load_screening_rules,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.profit import (
    build_normalized_profit_signals,
)
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_fin_summaries
from baibai_jobs.maintenance.jquants_inputs import JQuantsReader


def test_fy_reader_retains_latest_year_without_eps(tmp_path):
    start, end = date(2021, 1, 1), date(2026, 6, 1)
    records = [
        {
            "Code": "13010",
            "DiscDate": f"{year}-05-15",
            "DiscNo": f"statement-{year}",
            "CurPerType": "FY",
            "CurFYEn": f"{year}-03-31",
            "EPS": 10.0 if year < 2026 else None,
        }
        for year in range(2021, 2027)
    ]
    database = tmp_path / "market.sqlite"
    store_jquants_fin_summaries(database, records, requested_start=start, requested_end=end)
    rows = JQuantsReader(database).get_fy_summary_range(start, end)
    assert len(rows) == 6
    assert rows[-1].fiscal_year_end == date(2026, 3, 31)
    assert rows[-1].cumulative_eps is None
    signals = build_normalized_profit_signals(
        rows, [], end, close=100.0, ttm_rules=load_screening_rules().ttm
    )
    assert signals.normalized_per_3fy is None
    assert signals.normalized_per_5fy is None
