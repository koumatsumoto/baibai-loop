"""Calibration must reproduce the FY-only production split-reference policy."""

from contextlib import closing
from datetime import date

import pytest
from tests.helpers.screening_sqlite import (
    CALIBRATION_FIXTURE_ASOF,
    add_source_coverage,
    build_calibration_fixture_sqlite,
    insert_financial_record,
)

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    load_screening_rules,
)
from baibai_engine.machine_analysis.historical_evaluation.panel import build_panel
from baibai_engine.machine_analysis.models.company_financials.metrics.profit import (
    build_normalized_profit_signals,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.jquants_inputs import JQuantsReader


@pytest.mark.parametrize("issuance", [False, True])
def test_panel_normalized_per_uses_same_fy_references_as_production(tmp_path, issuance):
    database = tmp_path / "market.sqlite"
    build_calibration_fixture_sqlite(database)
    asof = CALIBRATION_FIXTURE_ASOF
    start = date(2023, 5, 15)
    with closing(open_connection(database)) as connection, connection:
        connection.execute("DELETE FROM jquants_fin_summaries WHERE ticker='9001'")
        for year in (2023, 2024, 2025, 2026):
            insert_financial_record(
                connection,
                {
                    "Code": "90010",
                    "DiscDate": f"{year}-05-15",
                    "DiscNo": f"annual-{year}",
                    "DocType": "FYFinancialStatements_Consolidated_JP",
                    "CurPerType": "FY",
                    "CurFYSt": f"{year - 1}-04-01",
                    "CurFYEn": f"{year}-03-31",
                    "CurPerSt": f"{year - 1}-04-01",
                    "CurPerEn": f"{year}-03-31",
                    "EPS": 10.0,
                    "ShOutFY": (3e8 if issuance else 2e8) if year == 2026 else 1e8,
                    "TrShFY": 0.0,
                    "NP": 1e9,
                },
            )
        insert_financial_record(
            connection,
            {
                "Code": "90010",
                "DiscDate": "2026-02-15",
                "DiscNo": "quarter-before-split",
                "DocType": "3QFinancialStatements_Consolidated_JP",
                "CurPerType": "3Q",
                "CurFYSt": "2025-04-01",
                "CurFYEn": "2026-03-31",
                "CurPerSt": "2025-04-01",
                "CurPerEn": "2025-12-31",
                "ShOutFY": 1.5e8 if issuance else 1e8,
                "TrShFY": 0.0,
            },
        )
        connection.execute(
            "UPDATE jquants_daily_bars SET adjustment_factor=0.5, ex_right_type='1' "
            "WHERE ticker='9001' AND traded_at='2026-04-10'"
        )
        for source, count in (("jquants_fin_summaries", 6), ("jquants_daily_bars", 400)):
            add_source_coverage(
                connection,
                source=source,
                coverage_key="synthetic-full-history",
                record_count=count,
                min_date=start.isoformat(),
                max_date=asof.isoformat(),
            )

    rules = load_screening_rules()
    reader = JQuantsReader(database)
    events = reader.get_adjustment_factor_bars_range(start, asof)
    production = build_normalized_profit_signals(
        [row for row in reader.get_fy_summary_range(start, asof) if row.ticker == "9001"],
        [event for event in events if event.ticker == "9001"],
        asof,
        close=100.0,
        ttm_rules=rules.ttm,
    )
    mixed = build_normalized_profit_signals(
        [row for row in reader.get_fin_summary_range(start, asof) if row.ticker == "9001"],
        [event for event in events if event.ticker == "9001"],
        asof,
        close=100.0,
        ttm_rules=rules.ttm,
    )
    panel = build_panel(asof, sqlite_path=database, rules=rules)
    target = next(row for row in panel.rows if row.ticker == "9001")
    assert mixed == production

    if issuance:
        # The FY-only reference cannot distinguish issuance from the intervening
        # split. Quarterly input must not silently give calibration an extra anchor.
        assert production.normalized_per_3fy is None
        assert target.normalized_per_3fy is None
        assert "normalized-earnings-power" not in target.valuation_approaches
    else:
        assert production.normalized_per_3fy == pytest.approx(15.0)
        assert target.normalized_per_3fy == pytest.approx(15.0)
        assert "normalized-earnings-power" in target.valuation_approaches
