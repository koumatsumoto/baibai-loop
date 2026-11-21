"""企業成長の計算と、保存した結果からのResearchの再開。"""

import socket
from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256
from unittest.mock import patch

import pytest
import yaml
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)
from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelection,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace import resume_selected_workspace
from baibai_engine.machine_analysis.models.company_growth import calculations
from baibai_engine.machine_analysis.models.company_growth.calculations import (
    build_changes,
    build_period_states,
)
from baibai_engine.machine_analysis.models.company_growth.generation import generate_company_growth
from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth, InputRelease
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    prior_year_period_matches,
)

NOW = datetime.fromisoformat("2026-10-24T18:00:00+09:00")


def document(year, quarter, sales, op=None, profit=None, **updates):
    end = (date(year, 6, 30), date(year, 9, 30), date(year, 12, 31), date(year + 1, 3, 31))[
        quarter - 1
    ]
    record = {
        "Code": "12340",
        "DiscDate": (end + timedelta(days=45)).isoformat(),
        "DiscTime": "15:00:00",
        "DiscNo": f"synthetic-{year}-{quarter}",
        "DocType": "FinancialStatements_Consolidated_JP",
        "CurPerType": ("1Q", "2Q", "3Q", "FY")[quarter - 1],
        "CurPerSt": date(year, 4, 1).isoformat(),
        "CurPerEn": end.isoformat(),
        "CurFYEn": date(year + 1, 3, 31).isoformat(),
        "Sales": sales,
        "OP": op,
        "NP": profit,
    }
    return {**record, **updates}


def states(records, sector="情報・通信業"):
    rows = [normalize_financial_summary(r) for r in records]
    assert all(row is not None for row in rows)
    return build_period_states(rows, sector=sector)


def observation(state, measurement):
    return next(o for o in state.observations if o.measurement == measurement)


def amount(state, measurement, metric):
    return observation(state, measurement).metrics[metric]


def change(result, end, metric, measurement="reported"):
    return next(
        c for c in result if c.to_end == end and c.metric == metric and c.measurement == measurement
    )


def company_growth(records, *, ticker, as_of, start=None, end=None):
    documents = [normalize_financial_summary(record) for record in records]
    assert all(item is not None and item.ticker == ticker for item in documents)
    return generate_company_growth(
        documents,
        ticker=ticker,
        sector=None,
        as_of=as_of,
        input_release=InputRelease(release_id="synthetic-release-1", manifest_sha256="a" * 64),
        start=start,
        end=end,
    )


def test_seasonality_and_one_time_gain_remain_one_quarter_observation():
    records = []
    for year in (2022, 2023, 2024):
        cumulative = 0
        for quarter in range(1, 5):
            cumulative += 200 if (year, quarter) == (2023, 1) else 100
            records.append(document(year, quarter, cumulative * 10, cumulative, cumulative))
    result = states(records)
    quarters = [amount(s, "quarter", "parent_profit").value for s in result]
    assert quarters[4:8] == [200, 100, 100, 100]
    ttm = [amount(s, "annual_ttm", "parent_profit").value for s in result]
    assert ttm[4:8] == [500] * 4
    assert ttm[8] == 400
    assert "sustained_growth" not in result[4].model_dump_json()


def test_eleven_month_year_remains_reported_but_not_annualized():
    result = states([document(2025, 4, 1100, 110, 55, CurPerEn="2026-02-28", CurFYEn="2026-02-28")])
    assert amount(result[0], "reported", "sales").value == 1100
    assert amount(result[0], "annual_ttm", "sales").value is None


def test_fiscal_year_change_does_not_combine_eleven_months_as_ttm():
    result = states(
        [
            document(2025, 2, 600, 60, 30),
            document(2025, 4, 1100, 110, 55, CurPerEn="2026-02-28", CurFYEn="2026-02-28"),
            document(
                2026,
                2,
                700,
                70,
                35,
                CurPerSt="2026-03-01",
                CurPerEn="2026-08-31",
                CurFYEn="2027-02-28",
            ),
        ]
    )
    assert amount(result[-1], "reported", "sales").value == 700
    annual = amount(result[-1], "annual_ttm", "sales")
    assert annual.value is None
    assert annual.reason == "accounting_periods_incomparable"


def test_cumulative_difference_and_reported_eps_never_share_arithmetic():
    result = states(
        [
            document(2024, 1, 100, 10, 100, EPS=1, AvgSh=100),
            document(2024, 2, 230, 25, 200, EPS=200 / 150, AvgSh=150),
        ]
    )
    assert amount(result[1], "quarter", "sales").value == 130
    assert amount(result[1], "quarter", "parent_profit").value == 100
    assert amount(result[1], "reported", "reported_basic_eps").value == pytest.approx(200 / 150)
    assert "reported_basic_eps" not in observation(result[1], "quarter").metrics
    assert "reported_basic_eps" not in observation(result[1], "annual_ttm").metrics
    assert observation(result[1], "quarter").period_start == date(2024, 7, 1)


@pytest.mark.parametrize(("corrected", "expected"), [(100, 130), (90, None)])
def test_later_q1_correction_only_blocks_a_known_changed_cumulative_pair(corrected, expected):
    records = [document(2024, 1, 100), document(2024, 2, 230)]
    records.append(document(2024, 1, corrected, DiscDate="2025-02-01", DiscNo="correction"))
    with patch.object(calculations, "_select", wraps=calculations._select) as select:
        result = states(records)
    sales_cohorts = [
        [row.disclosure_number for row in call.args[0]]
        for call in select.call_args_list
        if call.args[1] == "sales"
    ]
    assert sales_cohorts.count([records[0]["DiscNo"], "correction"]) == 1
    assert [records[0]["DiscNo"]] in sales_cohorts  # What was known at the H1 disclosure.
    value = amount(result[1], "quarter", "sales")
    assert value.value == expected
    if expected is None:
        assert value.reason == "inconsistent_cumulative_revision"
    assert amount(result[0], "reported", "sales").value == corrected
    # A new H1 on the corrected basis makes the pair usable again.
    records.append(document(2024, 2, 230, DiscDate="2025-02-02", DiscNo="h1-corrected"))
    assert amount(states(records)[1], "quarter", "sales").value == 230 - corrected


def test_partial_correction_and_same_revision_conflict_are_metric_local():
    base = document(2024, 1, 100, 10, 8)
    correction = {
        **base,
        "Sales": None,
        "OP": 15,
        "NP": None,
        "DiscDate": "2024-08-20",
        "DiscNo": "op-fix",
    }
    result = states([base, correction])
    assert amount(result[0], "reported", "sales").value == 100
    assert amount(result[0], "reported", "operating_profit").value == 15
    assert amount(result[0], "reported", "sales").documents[0].disclosure_number == base["DiscNo"]
    conflicting = {**correction, "OP": 16, "DiscNo": "op-conflict"}
    result = states([base, correction, conflicting])
    assert amount(result[0], "reported", "operating_profit").value is None
    assert amount(result[0], "reported", "sales").value == 100


@pytest.mark.parametrize(
    ("before", "after", "rate", "difference"),
    [
        (10, -10, -2, -20),
        (-100, -50, None, 50),
        (0, 10, None, 10),
        (1, 2, 1, 1),
        (100, 200, 1, 100),
    ],
)
def test_growth_rates_keep_amounts_sign_and_nonpositive_base(before, after, rate, difference):
    result = build_changes(states([document(2023, 1, before), document(2024, 1, after)]))
    value = change(result, date(2024, 6, 30), "sales")
    assert (value.from_value, value.to_value) == (before, after)
    assert value.difference == difference
    assert value.rate == rate
    assert value.sign_change is not None
    if before <= 0:
        assert value.reason == "nonpositive_base"


def test_margin_points_do_not_become_a_growth_score():
    result = build_changes(states([document(2023, 1, 100, 20), document(2024, 1, 200, 30)]))
    value = change(result, date(2024, 6, 30), "operating_margin")
    assert value.margin_points == pytest.approx(-5)
    assert value.rate is None
    assert change(result, date(2024, 6, 30), "operating_profit").rate == 0.5


def test_unverified_share_bases_keep_native_amounts_not_yoy_comparisons():
    periods = states(
        [
            document(2023, 4, 100, 20, 10, EPS=788.29, AvgSh=80, ShOutFY=100, TrShFY=0),
            document(2024, 4, 120, 25, 12, EPS=188.71, ShOutFY=500, TrShFY=100),
        ]
    )
    native = {
        "reported_basic_eps": ("reported", (788.29, 188.71)),
        "average_shares": ("reported", (80, None)),
        "issued_shares": ("balance", (100, 500)),
        "treasury_shares": ("balance", (0, 100)),
    }
    for metric, (measurement, expected) in native.items():
        assert tuple(amount(p, measurement, metric).value for p in periods) == expected
        assert observation(periods[-1], measurement).share_basis == "source_reported_unverified"
    assert amount(periods[0], "reported", "reported_basic_eps").documents[0].disclosure_number == (
        "synthetic-2023-4"
    )
    assert amount(periods[-1], "reported", "average_shares").reason == "not_observed"
    changes = build_changes(periods)
    assert not {item.metric for item in changes} & native.keys()
    assert change(changes, date(2025, 3, 31), "parent_profit").difference == 2


def test_cf_bs_and_ordinary_profit_are_not_filled_or_relabelled():
    result = states(
        [
            document(2024, 1, 100, None, -30, OdP=20, CFO=50, TA=1000, Eq=400),
            document(2024, 2, 230, 25, -60),
        ]
    )
    assert amount(result[0], "reported", "operating_profit").value is None
    assert amount(result[0], "reported", "parent_profit").value == -30
    assert amount(result[1], "reported", "operating_cash_flow").value is None
    assert amount(result[1], "balance", "total_assets").value is None
    assert "operating_cash_flow" not in observation(result[0], "quarter").metrics
    assert amount(result[0], "balance", "net_assets").value == 400
    assert "common_equity" not in observation(result[0], "balance").metrics


@pytest.mark.parametrize(
    "sector", ["銀行業", "保険業", "証券・商品先物取引業", "その他金融業", None]
)
def test_financial_or_unknown_sector_does_not_get_nonfinancial_margin(sector):
    result = states([document(2024, 1, 100, 20)], sector=sector)
    value = amount(result[0], "reported", "operating_margin")
    assert value.value is None
    assert value.reason in {"sector_unverified", "not_applicable_to_financial_sector"}
    assert amount(result[0], "reported", "operating_profit").value == 20


def test_basis_and_irregular_year_do_not_gain_convenient_ttm():
    result = states(
        [
            document(2023, 1, 100),
            document(2023, 4, 400),
            document(2024, 1, 120, DocType="FinancialStatements_NonConsolidated_JP"),
        ]
    )
    assert amount(result[2], "annual_ttm", "sales").value is None
    assert amount(result[2], "annual_ttm", "sales").reason == "statement_basis_incomparable"
    assert change(build_changes(result), date(2024, 6, 30), "sales").difference is None
    short = document(2024, 4, 400, CurPerSt="2024-07-01")
    assert amount(states([short])[0], "annual_ttm", "sales").value is None
    first = normalize_financial_summary(document(2023, 1, 100))
    second = normalize_financial_summary(document(2024, 1, 120, CurPerEn="2024-07-05"))
    assert prior_year_period_matches(
        first, second, length_tolerance_ratio=0.1, end_tolerance_days=45
    )
    shifted = normalize_financial_summary(
        document(2024, 1, 120, CurPerSt="2024-07-01", CurPerEn="2024-09-30")
    )
    assert not prior_year_period_matches(
        first, shifted, length_tolerance_ratio=0.1, end_tolerance_days=45
    )


@pytest.mark.parametrize("standard", ["JP", "IFRS"])
def test_unchanged_accounting_standard_preserves_growth(standard):
    records = [
        document(2024, 1, 100, 10, 8, DocType=f"1QFinancialStatements_Consolidated_{standard}"),
        document(2025, 1, 120, 20, 12, DocType=f"1QFinancialStatements_Consolidated_{standard}"),
    ]
    result = states(records)
    assert amount(result[1], "reported", "operating_profit").value == 20
    assert change(build_changes(result), date(2025, 6, 30), "operating_profit").difference == 10


def test_accounting_transition_blocks_only_comparisons_and_derived_amounts():
    records = [
        document(2024, 1, 100, 12, 8, DocType="1QFinancialStatements_Consolidated_JP"),
        document(2024, 4, 400, 50, 35, DocType="FYFinancialStatements_Consolidated_JP"),
        document(2025, 1, 160, 35, 20, DocType="1QFinancialStatements_Consolidated_IFRS"),
    ]
    result = states(records)
    current = result[-1]
    assert amount(current, "reported", "operating_profit").value == 35
    assert observation(current, "reported").accounting_standard == "IFRS"
    assert amount(current, "annual_ttm", "parent_profit").reason == (
        "accounting_standard_incomparable"
    )
    comparison = change(build_changes(result), date(2025, 6, 30), "operating_profit")
    assert comparison.from_value == 12
    assert comparison.to_value == 35
    assert comparison.difference is None
    assert comparison.reason == "accounting_standard_incomparable"


def test_newer_accounting_revision_does_not_revive_older_matching_basis():
    prior = document(2024, 1, 100, 10, 8)
    revised = document(
        2024,
        1,
        110,
        12,
        9,
        DiscDate="2024-08-20",
        DiscNo="ifrs-correction",
        DocType="1QFinancialStatements_Consolidated_IFRS",
    )
    current = document(2025, 1, 130, 20, 15)
    result = states([prior, revised, current])
    comparison = change(build_changes(result), date(2025, 6, 30), "operating_profit")
    assert comparison.from_value == 12
    assert comparison.difference is None
    assert comparison.reason == "accounting_standard_incomparable"


def test_unknown_accounting_standard_keeps_reported_amount():
    result = states(
        [
            document(2024, 1, 100, 10, 8, DocType="1QFinancialStatements_Consolidated_Foreign"),
            document(2025, 1, 120, 20, 10, DocType="1QFinancialStatements_Consolidated_Foreign"),
        ]
    )
    assert amount(result[-1], "reported", "sales").value == 120
    assert change(build_changes(result), date(2025, 6, 30), "sales").reason == (
        "accounting_standard_incomparable"
    )


def test_query_retains_pre_window_operands_and_later_corrections_at_both_edges():
    records = [document(y, q, q * 100) for y in (2022, 2023, 2024) for q in range(1, 5)]
    records.append(document(2022, 4, 450, DiscDate="2026-10-01", DiscNo="late-old-fy"))
    narrow = company_growth(
        records, ticker="1234", as_of=NOW.date(), start=date(2023, 4, 1), end=date(2023, 12, 31)
    )
    broad = company_growth(
        records, ticker="1234", as_of=NOW.date(), start=date(2022, 4, 1), end=date(2025, 3, 31)
    )
    assert narrow.periods == tuple(
        s for s in broad.periods if date(2023, 4, 1) <= s.period_end <= date(2023, 12, 31)
    )
    assert narrow.changes == tuple(
        c for c in broad.changes if date(2023, 4, 1) <= c.to_end <= date(2023, 12, 31)
    )
    ttm = amount(narrow.periods[0], "annual_ttm", "sales")
    assert ttm.value == 450
    assert any(d.disclosure_number == "late-old-fy" for d in ttm.documents)
    assert narrow.input_release.release_id == "synthetic-release-1"
    assert narrow.input_release.receipt_cutoff is None
    assert narrow.coverage.status == "unverified"
    assert narrow.coverage.observed_start == date(2022, 6, 30)


def test_observations_do_not_prove_continuous_acquisition_coverage():
    records = [document(2022, 1, 100), document(2024, 1, 200)]
    result = company_growth(records, ticker="1234", as_of=NOW.date())
    assert result.status == "available"
    assert result.coverage.status == "unverified"
    assert result.coverage.gaps_between_observations == ((date(2022, 6, 30), date(2024, 6, 30)),)
    value = change(result.changes, date(2024, 6, 30), "sales")
    assert value.from_value is None
    assert value.reason == "comparable_prior_period_not_observed"


def test_generation_excludes_invalid_documents_and_rejects_invalid_display_period():
    records = [document(2024, 1, 100), document(2024, 2, 230, CurPerEn="2027-03-31")]
    result = company_growth(records, ticker="1234", as_of=NOW.date())
    assert len(result.periods) == 1
    assert result.coverage.excluded_document_count == 1
    with pytest.raises(ValueError, match="start <= end"):
        company_growth(records, ticker="1234", as_of=NOW.date(), start=date(2027, 1, 1))


def test_fixed_growth_result_reaches_l3_and_does_not_change_valuation_gaps(monkeypatch):
    records = [document(2023, 1, 100, 20, 10), document(2024, 1, 200, 30, 25)]
    selected = selection(now=NOW, tickers=("1234",))
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("network used"))
    growth = company_growth(
        records, ticker="1234", as_of=NOW.date(), start=date(2024, 4, 1), end=date(2024, 6, 30)
    )
    context = dict(selected.inputs.securities_context["1234"])
    context["company_growth"] = growth.model_dump(mode="json")
    inputs = selected.inputs.model_copy(update={"securities_context": {"1234": context}})
    saved = ResearchSelection.model_validate_json(
        selected.model_copy(update={"inputs": inputs}).model_dump_json()
    )
    expected = deepcopy(inputs.securities_context["1234"]["company_growth"])
    assert inputs.analysis == selected.inputs.analysis
    assert inputs.input_gaps == selected.inputs.input_gaps
    assert expected["changes"][0]["from_value"] == 100
    assert expected["changes"][0]["to_value"] == 200
    assert expected["periods"][0]["period_end"] == "2024-06-30"
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.generation.financial_models.generate_company_growth",
        lambda *_a, **_kw: pytest.fail("growth recomputed"),
    )
    projected = project_research_inputs(saved)["securities"]["1234"]["company_growth"]
    assert projected["input_release"] == expected["input_release"]
    assert (
        projected["comparison_basis"]
        == expected["comparison_basis"]
        == "source_reported_economic_scope_unverified"
    )
    assert projected["reported"]["rows"][0]["values"][:3] == [200, 30, 25]
    assert projected["reported_yoy"]["rows"][0][5:8] == [100, 100, 1]
    assert saved.inputs.securities_context["1234"]["company_growth"] == expected
    assert selection_sha256(saved) != selection_sha256(selected)
    legacy = saved.model_dump(mode="json")
    del legacy["inputs"]["securities_context"]["1234"]["company_growth"]
    assert (
        "company_growth"
        not in project_research_inputs(parse_selection(legacy))["securities"]["1234"]
    )
    legacy["inputs"]["securities_context"]["1234"]["growth_score"] = 100
    with pytest.raises(ValueError, match="security context"):
        parse_selection(legacy)


def test_workspace_resume_restores_saved_growth_without_store_or_current_method(
    tmp_path, monkeypatch
):
    records = [document(2023, 1, 100), document(2024, 1, 200)]
    selected = selection(now=NOW, tickers=("1234",))
    context = dict(selected.inputs.securities_context["1234"])
    context["company_growth"] = company_growth(
        records, ticker="1234", as_of=NOW.date(), start=date(2024, 4, 1), end=date(2024, 6, 30)
    ).model_dump(mode="json")
    selected = selected.model_copy(
        update={
            "inputs": selected.inputs.model_copy(update={"securities_context": {"1234": context}})
        }
    )
    db, workspace = tmp_path / "application.sqlite", tmp_path / "workspace"
    operation = historical_operation(selected)
    seed_historical_operation(db, operation)
    arguments = dict(db_path=db, workspace=workspace, now=NOW)
    prepared = resume_selected_workspace(operation_id=operation.operation_id, **arguments)
    saved = selection_binding(OperationService(db).get(prepared.operation_id).payload)
    expected = project_research_inputs(saved)
    output = workspace / "domain-inputs.yaml"
    output.write_text("damaged projection", encoding="utf-8")
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.generation.financial_models.generate_company_growth",
        lambda *_a, **_kw: pytest.fail("current method used"),
    )
    resumed = resume_selected_workspace(operation_id=prepared.operation_id, force=True, **arguments)
    assert resumed.status == "resumed"
    assert yaml.safe_load(output.read_text(encoding="utf-8")) == expected
    assert (
        expected["securities"]["1234"]["company_growth"]["input_release"]["release_id"]
        == "synthetic-release-1"
    )


def test_same_current_state_keeps_distinct_paths_for_l3():
    improving = states(
        [
            document(2022, 4, 1000, 50, 50),
            document(2023, 4, 1000, 75, 75),
            document(2024, 4, 1000, 100, 100),
        ]
    )
    declining = states(
        [
            document(2022, 4, 1000, 200, 200),
            document(2023, 4, 1000, 150, 150),
            document(2024, 4, 1000, 100, 100),
        ]
    )
    assert amount(improving[-1], "reported", "parent_profit").value == 100
    assert amount(declining[-1], "reported", "parent_profit").value == 100
    up = change(build_changes(improving), date(2025, 3, 31), "parent_profit")
    down = change(build_changes(declining), date(2025, 3, 31), "parent_profit")
    assert (up.from_value, up.to_value, up.difference) == (75, 100, 25)
    assert (down.from_value, down.to_value, down.difference) == (150, 100, -50)
    assert up.margin_points is None
    assert "forecast" not in up.model_dump()


def test_legacy_selection_hash_keeps_its_original_serialized_inputs():
    from baibai_engine.primitives.json import canonical_json

    old = selection(now=NOW, tickers=("1234",)).model_dump(mode="json")
    expected = sha256(canonical_json(old).encode()).hexdigest()
    restored = parse_selection(old)
    assert restored.model_dump(mode="json") == old
    assert selection_sha256(restored) == expected
    assert "company_growth" not in restored.inputs.securities_context["1234"]
    assert "company_growth" not in project_research_inputs(restored)["securities"]["1234"]


@pytest.mark.parametrize("start", [None, date(2021, 6, 30)])
def test_growth_display_window_leaves_fixed_inputs_and_requested_range_unchanged(
    tmp_path, monkeypatch, capsys, start
):
    from baibai_engine.cli.investment_judgment import main
    from baibai_engine.investment_judgment.workflow.workspace import compute_status

    records = [
        document(year, quarter, quarter * 100, quarter * 20, quarter * 10)
        for year in range(2020, 2025)
        for quarter in range(1, 5)
    ]
    selected = selection(now=NOW, tickers=("1234",))
    context = dict(selected.inputs.securities_context["1234"])
    context["company_growth"] = company_growth(
        records, ticker="1234", as_of=NOW.date(), start=start, end=date(2025, 3, 31)
    ).model_dump(mode="json")
    selected = selected.model_copy(
        update={
            "inputs": selected.inputs.model_copy(update={"securities_context": {"1234": context}})
        }
    )
    operation = historical_operation(selected)
    arguments = dict(
        db_path=tmp_path / "application.sqlite", workspace=tmp_path / "workspace", now=NOW
    )
    seed_historical_operation(arguments["db_path"], operation)
    prepared = resume_selected_workspace(operation_id=operation.operation_id, **arguments)
    service = OperationService(arguments["db_path"])
    saved = selection_binding(service.get(prepared.operation_id).payload)
    original = saved.model_dump(mode="json")
    digest = selection_sha256(saved)
    growth = saved.inputs.securities_context["1234"]["company_growth"]
    assert len(growth["periods"]) == (20 if start is None else 16)
    output = arguments["workspace"] / "domain-inputs.yaml"

    def displayed():
        return yaml.safe_load(output.read_text())["securities"]["1234"]["company_growth"]

    compact = displayed()
    assert compact["saved_scope"]["requested_start"] == (start.isoformat() if start else None)
    assert compact["saved_scope"]["requested_end"] == "2025-03-31"
    assert compact["display_scope"]["period_count"] == 12
    assert len(compact["reported"]["rows"]) == 12
    assert compact["reported"]["rows"][0]["values"][:3] == [100, 20, 10]
    assert compact["reported"]["rows"][0]["missing"]["reported_basic_eps"] == "not_observed"
    assert compact["documents"]["d1"]["disclosure_number"] == "synthetic-2022-1"
    # These must be the already saved calculations, including prior operands outside the display.
    columns = compact["reported_yoy"]["columns"]
    first = dict(zip(columns, compact["reported_yoy"]["rows"][0], strict=True))
    assert first["from_end"] == "2021-06-30"
    assert first["from_value"] == 100
    assert first["rate"] == 0
    assert "periods" not in compact
    assert compact["full_result_ref"] == "/inputs/securities_context/1234/company_growth"

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.generation.financial_models.generate_company_growth",
        lambda **_kw: pytest.fail("inputs recollected"),
    )
    authored = arguments["workspace"] / "assessment-draft.yaml"
    authored.write_text("authored draft retained")
    resumed = resume_selected_workspace(
        operation_id=operation.operation_id, **arguments, company_growth_periods=16, force=True
    )
    assert resumed.selection_sha256 == digest
    assert displayed()["display_scope"]["period_count"] == 16
    assert (
        main(
            [
                "prepare",
                "--operation-id",
                prepared.operation_id,
                "--db",
                str(arguments["db_path"]),
                "--workspace",
                str(arguments["workspace"]),
                "--company-growth-periods",
                "all",
                "--force",
            ],
            now=NOW,
        )
        == 0
    )
    capsys.readouterr()
    assert displayed()["display_scope"]["period_count"] == len(growth["periods"])
    assert displayed()["display_scope"]["latest_period_limit"] is None
    assert compute_status(arguments["workspace"], db_path=arguments["db_path"], now=NOW)
    assert authored.read_text() == "authored draft retained"
    restored = selection_binding(service.get(prepared.operation_id).payload)
    assert restored.model_dump(mode="json") == original
    assert selection_sha256(restored) == digest


def test_metric_availability_cannot_change_the_shared_ttm_measurement_interval():
    records = [
        document(2023, 1, 100),
        document(2023, 1, None, 10, CurPerEn="2023-07-05", DiscNo="different-interval"),
        document(2023, 4, 400, 50),
        document(2024, 1, 200, 25),
    ]
    current = states(records)[-1]
    ttm = observation(current, "annual_ttm")
    assert ttm.period_start == date(2023, 7, 1)
    assert ttm.period_end == date(2024, 6, 30)
    assert ttm.metrics["sales"].value == 500
    assert ttm.metrics["operating_profit"].value is None
    assert ttm.metrics["operating_profit"].reason == "operand_unavailable"
    # The differently measured original OP remains observed, without relabelling it.
    assert amount(states(records)[1], "reported", "operating_profit").value == 10


@pytest.mark.parametrize(
    ("correction_time", "h1_time", "expected", "reason"),
    [
        ("16:00:00", "15:00:00", None, "inconsistent_cumulative_revision"),
        ("14:00:00", "15:00:00", 140, None),
        (None, "15:00:00", None, "cumulative_revision_order_unresolved"),
        ("16:00:00", None, None, "cumulative_revision_order_unresolved"),
        (None, None, None, "cumulative_revision_order_unresolved"),
    ],
)
def test_same_day_cumulative_correction_respects_known_and_unknown_times(
    correction_time, h1_time, expected, reason
):
    result = states(
        [
            document(2024, 1, 100),
            document(2024, 2, 230, DiscDate="2024-11-14", DiscTime=h1_time),
            document(
                2024,
                1,
                90,
                DiscDate="2024-11-14",
                DiscTime=correction_time,
                DiscNo="same-day-q1-correction",
            ),
        ]
    )
    value = amount(result[-1], "quarter", "sales")
    assert value.value == expected
    assert value.reason == reason


def test_unknown_times_without_an_observed_change_do_not_invent_a_revision_conflict():
    result = states(
        [
            document(2024, 1, 100, DiscDate="2024-11-14", DiscTime=None),
            document(2024, 2, 230, DiscDate="2024-11-14", DiscTime=None),
        ]
    )
    assert amount(result[-1], "quarter", "sales").value == 130


def test_compact_growth_keeps_quarter_and_annual_conflict_and_reported_cash_flow():
    periods = states(
        [
            document(2023, 1, 100, 30, 40),
            document(2023, 4, 400, 80, 100, CFO=110),
            document(2024, 1, 110, 15, 10),
            document(2024, 4, 440, 90, 120, CFO=105),
            document(2025, 1, 115, 20, 8, TA=300, Eq=150),
        ]
    )
    growth = CompanyGrowth(
        ticker="1234",
        status="available",
        requested_start=None,
        requested_end=date(2025, 6, 30),
        periods=periods,
        changes=build_changes(periods),
    )
    saved = growth.model_dump(mode="json")
    original = deepcopy(saved)
    projected = project_company_growth(saved, ticker="1234")
    latest = projected["latest_observations"]

    def values(measurement):
        view = latest[measurement]
        return dict(zip(view["columns"], view["row"]["values"], strict=True))

    assert values("quarter")["parent_profit"] == 8
    assert values("annual_ttm")["parent_profit"] == 118
    quarter_change = next(c for c in latest["quarter"]["yoy"] if c["metric"] == "parent_profit")
    annual_change = next(c for c in latest["annual_ttm"]["yoy"] if c["metric"] == "parent_profit")
    assert quarter_change["rate"] == pytest.approx(-0.2)
    assert annual_change["from_value"] == 70
    assert annual_change["to_value"] == 118
    assert annual_change["rate"] == pytest.approx(48 / 70)
    assert latest["annual_ttm"]["row"]["period_start"] == "2024-07-01"
    assert values("balance")["net_assets"] == 150
    assert latest["balance"]["row"]["period_start"] == "2025-06-30"
    cf = projected["reported_cash_flow"]
    assert cf["measurement"] == "reported_FY_or_year_to_date_not_single_quarter"
    assert [row["values"][0] for row in cf["rows"]] == [None, 110, None, 105, None]
    assert cf["rows"][-1]["missing"]["operating_cash_flow"] == "not_observed"
    for view in latest.values():
        for ref in view["row"]["sources"]:
            assert ref in projected["documents"]
    assert saved == original
    # The saved TTM/YoY survive even when their old operands are outside the payload.
    latest_only = {**saved, "periods": saved["periods"][-1:]}
    narrow = project_company_growth(latest_only, ticker="1234")["latest_observations"]
    for measurement, view in latest.items():
        assert narrow[measurement]["row"]["values"] == view["row"]["values"]
        assert narrow[measurement]["yoy"] == view["yoy"]


def test_compact_growth_does_not_replace_latest_missing_ttm_with_old_fy():
    periods = states(
        [document(2024, 4, 400, 80, 100), document(2025, 2, 220, 30, 20)],
        sector="銀行業",
    )
    growth = CompanyGrowth(
        ticker="1234",
        status="available",
        requested_start=None,
        requested_end=date(2025, 9, 30),
        periods=periods,
        changes=build_changes(periods),
    )
    projected = project_company_growth(growth.model_dump(mode="json"), ticker="1234")
    latest = projected["latest_observations"]["annual_ttm"]
    assert latest["row"]["period_end"] == "2025-09-30"
    assert latest["row"]["values"] == [None, None, None, None]
    assert latest["row"]["missing"]["parent_profit"] == "matching_period_not_observed"
    assert latest["row"]["missing"]["operating_margin"] == "not_applicable_to_financial_sector"


@pytest.mark.parametrize("derived", ["quarter", "annual_ttm"])
@pytest.mark.parametrize("other_sales", [100, 110, None])
def test_derived_period_ties_require_metric_agreement(derived, other_sales):
    from itertools import permutations

    prior = document(2024, 1, 100, 10)
    other = document(2024, 1, other_sales, 10, CurPerType="2Q", DiscNo="other-identity")
    records = [prior, other]
    if derived == "quarter":
        records.append(document(2024, 2, 230, 25))
        expected_sales, expected_op = 130, 15
    else:
        records.extend([document(2024, 4, 500, 50), document(2025, 1, 120, 12)])
        expected_sales, expected_op = 520, 52
    for ordered in permutations(records):
        result = states(ordered)
        selected = amount(result[-1], derived, "sales")
        assert selected.value == (expected_sales if other_sales == 100 else None)
        assert selected.reason == (None if other_sales == 100 else "operand_unavailable")
        assert amount(result[-1], derived, "operating_profit").value == expected_op
        assert observation(result[-1], derived).period_status == "available"
        # Both native reported identities remain observable, even when they disagree.
        assert len([s for s in result if s.period_end == date(2024, 6, 30)]) == 2


def test_ttm_annual_operand_ties_do_not_choose_a_convenient_identity():
    records = [
        document(2024, 1, 100, 10),
        document(2024, 4, 500, 50),
        document(2024, 4, 600, 50, CurPerType="3Q", DiscNo="other-annual-identity"),
        document(2025, 1, 120, 12),
    ]
    result = states(records)
    assert amount(result[-1], "annual_ttm", "sales").value is None
    assert amount(result[-1], "annual_ttm", "operating_profit").value == 52


def test_ttm_tied_prior_ends_do_not_resolve_conflicting_actual_starts():
    records = [
        document(2024, 1, 100),
        document(2024, 1, 100, CurPerSt="2024-04-02", DiscNo="other-start"),
        document(2024, 4, 500),
        document(2025, 1, 120),
    ]
    result = states(records)
    annual = observation(result[-1], "annual_ttm")
    assert annual.period_status == "accounting_periods_incomparable"
    assert annual.metrics["sales"].value is None
    assert annual.metrics["sales"].reason == "accounting_periods_incomparable"


@pytest.mark.parametrize("shift", [-46, -45, 45, 46])
def test_ttm_skips_unrelated_end_dates_before_the_full_comparison(shift):
    end = date(2024, 12, 31) + timedelta(days=shift)
    # A shifted start keeps this period within the duration tolerance. At ±45
    # days it remains a candidate, but cannot be combined with the prior FY.
    start = date(2024, 4, 1) + timedelta(days=-30 if shift < 0 else 30)
    prior = document(
        2024,
        3,
        300,
        CurPerSt=start.isoformat(),
        CurPerEn=end.isoformat(),
        DiscDate=(end + timedelta(days=30)).isoformat(),
    )
    current = document(2025, 3, 350)
    with patch.object(
        calculations, "prior_year_period_matches", wraps=prior_year_period_matches
    ) as compare:
        periods = states([document(2020, 3, 200), prior, document(2024, 4, 400), current])
    checked = {
        left.period_end
        for (left, right), _ in compare.call_args_list
        if right.period_end == date(2025, 12, 31)
    }
    inside = abs(shift) == 45
    assert checked == ({end} if inside else set())
    reason = "accounting_periods_incomparable" if inside else "matching_period_not_observed"
    ttm = observation(periods[-1], "annual_ttm")
    assert ttm.period_status == reason
    assert ttm.metrics["sales"].reason == reason


def test_equal_distance_ttm_priors_are_not_reduced_to_one_end_date():
    earlier = document(2024, 1, 100, CurPerEn="2024-06-25", DiscNo="earlier")
    later = document(2024, 1, 100, CurPerEn="2024-07-05", DiscNo="later")
    tail = [document(2024, 4, 400), document(2025, 1, 130)]
    for single in (earlier, later):
        result = amount(states([single, *tail])[-1], "annual_ttm", "sales")
        assert result.value == 430
        assert result.reason is None
    for order in ((earlier, later), (later, earlier)):
        result = amount(states([*order, *tail])[-1], "annual_ttm", "sales")
        assert result.value is None
        assert result.reason == "accounting_periods_incomparable"


@pytest.mark.parametrize(
    ("measurement", "quarter"), [("quarter", 2), ("quarter", 4), ("annual_ttm", 1)]
)
def test_all_tied_identities_keep_their_cumulative_revision_history(measurement, quarter):
    from itertools import product

    prior_quarter = quarter - 1 if measurement == "quarter" else quarter
    a = [
        document(2024, prior_quarter, 100, 10, DiscNo="a-old"),
        document(2024, prior_quarter, 100, 10, DiscNo="a-unchanged", DiscDate="2025-07-01"),
    ]
    b = [
        document(2024, prior_quarter, 90, 10, CurPerType="2Q", DiscNo="b-old"),
        document(
            2024,
            prior_quarter,
            100,
            10,
            CurPerType="2Q",
            DiscNo="b-correction",
            DiscDate="2025-06-01",
        ),
    ]
    counterpart = document(2024, quarter if measurement == "quarter" else 4, 230, 25)
    other = [counterpart]
    if measurement == "annual_ttm":
        other.append(document(2025, quarter, 120, 12))
    # Both orders of the tied identities and both receipt orders within each.
    for a_rows, b_rows in product((a, a[::-1]), (b, b[::-1])):
        for first, last in ((a_rows, b_rows), (b_rows, a_rows)):
            result = states([*first, *other, *last])
            value = amount(result[-1], measurement, "sales")
            assert value.value is None
            assert value.reason == "inconsistent_cumulative_revision"
            assert amount(result[-1], measurement, "operating_profit").value == (
                27 if measurement == "annual_ttm" else 15
            )
    records = [*a, *b, *other]
    # A later disclosure of the containing cumulative resolves the known change.
    records.append({**counterpart, "DiscDate": "2025-07-02", "DiscNo": "counterpart-corrected"})
    value = amount(states(records)[-1], measurement, "sales")
    assert value.value == (250 if measurement == "annual_ttm" else 130)
    assert value.reason is None
    assert {source.disclosure_number for source in value.documents} >= {
        "a-unchanged",
        "b-correction",
    }


def test_ttm_checks_changed_prior_against_every_tied_annual_identity():
    records = [
        document(2024, 1, 90, 10),
        document(2024, 4, 500, 50, DiscNo="annual-a"),
        document(2024, 4, 500, 50, CurPerType="3Q", DiscNo="annual-b", DiscDate="2025-07-01"),
        document(2024, 1, 100, 10, DiscNo="prior-corrected", DiscDate="2025-06-01"),
        document(2025, 1, 120, 12),
    ]
    value = amount(states(records)[-1], "annual_ttm", "sales")
    assert value.value is None
    assert value.reason == "inconsistent_cumulative_revision"
    records.append(document(2024, 4, 500, 50, DiscNo="annual-a-corrected", DiscDate="2025-07-02"))
    value = amount(states(records)[-1], "annual_ttm", "sales")
    assert value.value == 520
    assert value.reason is None
    assert {source.disclosure_number for source in value.documents} >= {
        "annual-a-corrected",
        "annual-b",
    }
