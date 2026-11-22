"""同日・同期間の別会計cohortを、PDFと保存後の比較読取で混同しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from itertools import permutations

import pytest
from tests.engine.machine_analysis.models.company_growth.test_pdf_annual_comparison import (
    discovery_paths,
)
from tests.engine.machine_analysis.observations.financials.test_pdf_selection import (
    document,
    models,
)
from tests.helpers.pdf_statements import statement

from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)
from baibai_engine.machine_analysis.models.company_growth.reading import CompanyGrowthReader
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    _change,
    _growth_policy_inputs,
    _recovery_available,
)
from baibai_engine.machine_analysis.screening.policies.financial_growth import _interval_check
from baibai_engine.primitives.json import canonical_json


def current_quarters(kind):
    quarter = document()
    short_year = replace(
        quarter,
        fiscal_period="FY",
        fiscal_year_end=quarter.period_end,
        disclosure_number="short-fy",
        document_type=f"FYFinancialStatements_{kind}",
        sales=123_000_000_000,
        operating_profit=7_000_000_000,
        profit=2_000_000_000,
    )
    return quarter, short_year


def prior_quarter():
    return replace(
        document(),
        disclosure_number="prior-q1",
        disclosed_at=date(2025, 8, 4),
        period_start=date(2025, 4, 1),
        period_end=date(2025, 6, 30),
        fiscal_year_end=date(2026, 3, 31),
        sales=69_111_000_000,
        operating_profit=1_000_000_000,
        profit=600_000_000,
    )


@pytest.mark.parametrize(
    ("kind", "sales", "reason"),
    [
        ("NonConsolidated_JP", 123_000_000_000, "statement_basis_incomparable"),
        ("Consolidated_IFRS", 123_000_000_000, None),
        ("Consolidated_IFRS", None, "operand_unavailable"),
        ("Consolidated_Foreign", 123_000_000_000, "accounting_standard_incomparable"),
    ],
)
def test_selected_pdf_only_changes_its_native_cohort_and_its_projected_comparison(
    kind, sales, reason
):
    quarter, short_year = current_quarters(kind)
    short_year = replace(short_year, sales=sales)
    documents = (document(True), prior_quarter(), quarter, short_year)
    selected = statement()
    pdfs = (statement(True), selected)
    _, original, _ = models(documents=documents, pdfs=())
    _, generated, _ = models(documents=documents, pdfs=pdfs)
    for order in permutations(documents):
        assert models(documents=order, pdfs=pdfs)[1] == generated

    before = canonical_json(generated.payload)
    reader = CompanyGrowthReader(generated.payload)
    comparison_reader = CompanyGrowthReader(original.payload)
    sibling, anchor = reader.model.periods[-2:]
    assert anchor is reader.latest_period
    assert sibling.fiscal_year_end != anchor.fiscal_year_end
    assert (sibling.period_start, sibling.period_end) == (anchor.period_start, anchor.period_end)
    assert sibling == comparison_reader.model.periods[-2]
    assert sibling.cohort != anchor.cohort
    short_reported = reader.observation(sibling, "reported")
    assert short_reported.metrics["sales"].value == sales
    if sales is None:
        assert short_reported.metrics["sales"].documents == ()
    else:
        assert short_reported.metrics["sales"].documents[0].disclosure_number == "short-fy"
    (native_change,) = reader.year_over_year(short_reported, "sales")
    assert native_change.target_cohort == sibling.cohort
    assert native_change.source_sha256 is None
    assert native_change.to_value == sales
    assert native_change.reason == reason
    if reason is not None:
        assert native_change.difference is native_change.rate is None

    current = reader.observation(anchor, "reported")
    (pdf_change,) = reader.year_over_year(current, "sales")
    assert pdf_change.source_sha256 == selected.source_sha256
    assert pdf_change.target_cohort == anchor.cohort
    assert pdf_change.native_prior_cohorts == ()
    assert pdf_change.to_value == current.metrics["sales"].value == 106_641_000_000
    (prior,) = reader.comparison_prior(current, pdf_change)
    assert prior.metrics["sales"].documents[0].source_sha256 == selected.source_sha256
    assert reader.comparison_prior(short_reported, pdf_change) == ()
    assert reader.year_over_year(current.model_copy(), "sales") == ()

    projection = project_company_growth(generated.payload, ticker=quarter.ticker, periods=1)
    (row,) = projection["reported"]["rows"]
    assert (row["fiscal_period"], row["values"][0]) == ("1Q", 106_641_000_000)
    table = projection["reported_yoy"]
    (sales,) = [entry for entry in table["rows"] if entry[2] == "sales"]
    assert sales[table["columns"].index("target_cohort")] == row["cohort"]
    assert sales[table["columns"].index("native_prior_cohorts")] == []
    yoy = projection["latest_observations"]["quarter"]["yoy"]
    assert [item["target_cohort"] for item in yoy] == [row["cohort"]] * len(yoy)
    assert canonical_json(generated.payload) == before


def test_unreceived_correction_blocks_only_its_cohort_not_other_native_ttm():
    quarter, short_year = current_quarters("Consolidated_IFRS")
    correction = replace(
        quarter, disclosure_number="q1-correction-pdf-not-received", disclosed_at=date(2026, 9, 1)
    )
    documents = (document(True), prior_quarter(), quarter, short_year, correction)
    _, native, _ = models(documents=documents, pdfs=())
    _, blocked, _ = models(documents=documents)
    assert blocked.payload["pdf_selection"]["current"] is None
    assert blocked.payload["pdf_selection"]["blocks_legacy_ttm"] is True
    reader = CompanyGrowthReader(blocked.payload)
    sibling, anchor = reader.model.periods[-2:]
    assert sibling == CompanyGrowthReader(native.payload).model.periods[-2]
    assert reader.observation(sibling, "annual_ttm").metrics["sales"].value == 369_396_123_000
    assert reader.observation(anchor, "annual_ttm").metrics["sales"].value is None
    assert (
        reader.observation(anchor, "annual_ttm").metrics["sales"].reason
        == "latest_disclosure_pdf_missing_or_ambiguous"
    )


@pytest.mark.parametrize("witness", ["short_fy", "unbound", "earlier_other_standard"])
def test_same_range_pdf_needs_the_current_native_cohort_to_block_ttm(witness):
    quarter, short_year = current_quarters("Consolidated_IFRS")
    short_year = replace(short_year, disclosure_number="20260804509999")
    documents = [document(True), prior_quarter(), short_year, quarter]
    source_id = short_year.disclosure_number
    pdf_day, standard = short_year.disclosed_at, "IFRS"
    if witness == "unbound":
        source_id = "20260804509998"
    elif witness == "earlier_other_standard":
        source_id, pdf_day, standard = "20260804509997", date(2026, 8, 6), "JP"
        documents.append(
            replace(
                quarter,
                disclosure_number=source_id,
                disclosed_at=pdf_day,
                document_type="1QFinancialStatements_Consolidated_JP",
            )
        )
    cousin = statement().model_copy(
        update={
            "disclosure_number": source_id,
            "published_on": pdf_day,
            "accounting_standard": standard,
            "source_sha256": "f" * 64,
            "source_url": f"https://www2.jpx.co.jp/disc/92680/1401{source_id}.pdf",
        }
    )
    annual_pdf = statement(True)
    baseline = models(documents=documents, pdfs=(annual_pdf,))
    with_cousin = models(documents=documents[::-1], pdfs=(cousin, annual_pdf))
    for financial, growth, _ in (baseline, with_cousin):
        assert financial.payload["earnings_price"]["profit_ttm_yen"] == 2_793_246_000
        assert financial.payload["financial"]["sales_ttm"] == 353_037_246_000
        reader = CompanyGrowthReader(growth.payload)
        latest = reader.observation(reader.latest_period, "annual_ttm")
        assert latest.metrics["sales"].value == 353_037_246_000
        assert growth.payload["pdf_selection"]["current"] is None
        assert growth.payload["pdf_selection"]["blocks_legacy_ttm"] is False
    assert with_cousin[0].payload["earnings_price"] == baseline[0].payload["earnings_price"]
    assert with_cousin[1].payload["periods"] == baseline[1].payload["periods"]
    assert with_cousin[1].payload["changes"] == baseline[1].payload["changes"]
    assert any(
        item["source_sha256"] == cousin.source_sha256
        for item in with_cousin[1].payload["pdf_supplement"]["statements"]
    )


@pytest.mark.parametrize("same_cohort", [False, True])
def test_unchanged_annual_pdf_requires_the_same_fiscal_cohort(same_cohort):
    earlier = document(True)
    latest = replace(
        earlier,
        disclosure_number="reconfirmed-native-fy",
        disclosed_at=date(2026, 6, 15),
        fiscal_year_end=earlier.fiscal_year_end if same_cohort else date(2026, 6, 30),
    )
    pdf = statement(True)
    baseline, base_growth, _ = models(documents=(earlier, latest), pdfs=())
    financial, growth, _ = models(documents=(latest, earlier), pdfs=(pdf,))
    selected = growth.payload["pdf_selection"]
    reader = CompanyGrowthReader(growth.payload)
    annual = reader.observation(reader.latest_period, "annual_ttm")
    if same_cohort:
        assert selected["current"]["disclosure_number"] == earlier.disclosure_number
        assert selected["annual_latest_disclosure_number"] == latest.disclosure_number
        assert selected["annual_revision_basis"] == "latest_reconfirmed_unchanged_amounts"
        assert selected["comparative_verified"] is False
        assert financial.payload["earnings_price"]["profit_ttm_yen"] == 2_469_000_000
        assert financial.payload["financial"]["sales_ttm"] == 315_507_000_000
        assert annual.metrics["sales"].documents[0].source_sha256 == pdf.source_sha256
        assert annual.metrics["sales"].value == 315_507_000_000
    else:
        assert selected["current"] is None
        assert not selected["blocks_legacy_ttm"]
        earnings = financial.payload["earnings_price"]
        assert earnings == baseline.payload["earnings_price"]
        assert earnings["profit_ttm_yen"] == 2_469_123_000
        assert earnings["profit_components"][0]["disclosure_number"] == latest.disclosure_number
        assert financial.payload["financial"]["sales_ttm"] == 315_507_123_000
        assert annual.metrics["sales"].documents[0].disclosure_number == latest.disclosure_number
        assert annual.metrics["sales"].value == 315_507_123_000
        assert growth.payload["periods"] == base_growth.payload["periods"]
    assert growth.payload["pdf_supplement"]["statements"][0]["source_sha256"] == pdf.source_sha256


def test_no_pdf_incompatible_sibling_cannot_remove_supported_screening_coordinates():
    quarter, short_year = current_quarters("NonConsolidated_JP")
    documents = (document(True), prior_quarter(), quarter)
    baseline = models(documents=documents, pdfs=())
    generated = models(documents=(*documents, short_year), pdfs=())
    active = {"business_expansion", "operating_recovery"}
    baseline_paths = [item for item in discovery_paths(baseline) if item["path_id"] in active]
    actual_paths = [item for item in discovery_paths(generated) if item["path_id"] in active]
    assert {item["path_id"] for item in actual_paths} == active
    assert actual_paths == baseline_paths


def test_exact_native_prior_set_survives_compaction_and_missing_refs_are_not_guessed():
    previous = prior_quarter()
    twin = replace(
        previous,
        fiscal_year_end=date(2026, 6, 30),
        disclosure_number="prior-q1-other-year",
    )
    _, growth, _ = models(documents=(document(True), previous, twin, document()), pdfs=())
    for payload in (growth.payload, _growth_policy_inputs(growth.payload)):
        reader = CompanyGrowthReader(payload)
        current = reader.observation(reader.latest_period, "quarter")
        (pair,) = reader.year_over_year(current, "sales")
        assert len(pair.native_prior_cohorts) == 2
        assert pair.reason == "ambiguous_growth_comparison"
        assert pair.difference is pair.rate is None
        source_observations = reader.comparison_prior(current, pair)
        assert {p.metrics["sales"].documents[0].disclosure_number for p in source_observations} == {
            "prior-q1",
            "prior-q1-other-year",
        }

    missing_prior = deepcopy(growth.payload)
    missing_prior["periods"] = [
        state
        for state in missing_prior["periods"]
        if (state["fiscal_year_end"], state["fiscal_period"]) != ("2026-06-30", "1Q")
    ]
    reader = CompanyGrowthReader(_growth_policy_inputs(missing_prior))
    current = reader.observation(reader.latest_period, "quarter")
    (pair,) = reader.year_over_year(current, "sales")
    assert reader.comparison_prior(current, pair) == ()
    assert reader.comparison_prior(current, pair.model_copy()) == ()

    for missing_field in ("target_cohort", "native_prior_cohorts"):
        unbound = deepcopy(growth.payload)
        for item in unbound["changes"]:
            item.pop(missing_field)
        for payload in (unbound, _growth_policy_inputs(unbound)):
            reader = CompanyGrowthReader(payload)
            current = reader.observation(reader.latest_period, "quarter")
            assert reader.year_over_year(current, "sales") == ()


def test_restatement_of_one_prior_cohort_stays_comparable_but_distinct_equal_prior_does_not():
    previous = prior_quarter()
    repeated = replace(
        previous,
        disclosed_at=date(2025, 8, 5),
        disclosure_number="equal-amount-correction",
    )
    distinct = replace(repeated, fiscal_year_end=date(2026, 6, 30))
    short = replace(
        distinct,
        fiscal_year_end=previous.period_end,
        fiscal_period="FY",
        document_type="FYFinancialStatements_Consolidated_IFRS",
    )
    retained = (document(True), previous, document())
    authentic = models(documents=(*retained, repeated), pdfs=())
    ambiguous = models(documents=(*retained, distinct), pdfs=())
    same_role = models(documents=(*retained, short), pdfs=())
    required = {"business_expansion", "operating_recovery"}
    assert required <= {item["path_id"] for item in discovery_paths(authentic)}
    assert required <= {item["path_id"] for item in discovery_paths(same_role)}
    assert not required & {item["path_id"] for item in discovery_paths(ambiguous)}
    for output, count in ((authentic, 1), (same_role, 1), (ambiguous, 2)):
        reader = CompanyGrowthReader(_growth_policy_inputs(output[1].payload))
        current = reader.observation(reader.latest_period, "quarter")
        (pair,) = reader.year_over_year(current, "operating_margin")
        assert len(pair.native_prior_cohorts) == count
        assert (pair.reason, pair.difference is None) == (
            ("ambiguous_growth_comparison", True) if count == 2 else (None, False)
        )


def test_window_can_clip_a_real_native_prior_without_erasing_historical_comparison():
    documents = (document(True), prior_quarter(), document())
    original = models(documents=documents, pdfs=(), growth_start=date(2025, 1, 1))
    assert any(item["path_id"] == "business_expansion" for item in discovery_paths(original))
    financial, growth, technical = models(
        documents=documents, pdfs=(), growth_start=date(2026, 1, 1)
    )
    clipped = growth.payload
    assert clipped["periods"] == original[1].payload["periods"][1:]
    assert clipped["changes"] == [
        item for item in original[1].payload["changes"] if item["to_end"] >= "2026-01-01"
    ]
    reader = CompanyGrowthReader(_growth_policy_inputs(clipped))
    current = reader.observation(reader.latest_period, "quarter")
    (saved,) = reader.year_over_year(current, "sales")
    assert saved.rate > 0
    assert reader.comparison_prior(current, saved) == ()
    screened = _change(reader, current, "sales")
    assert screened["reason"] == "saved_comparison_operands_unavailable"
    assert screened["difference"] is screened["rate"] is None
    assert screened["native_prior_cohorts"] == reader.saved_change(saved)["native_prior_cohorts"]
    view = project_company_growth(clipped, ticker=growth.subject, periods=1)
    reported = view["reported_yoy"]
    (sales,) = [
        dict(zip(reported["columns"], row, strict=True))
        for row in reported["rows"]
        if row[2] == "sales"
    ]
    assert sales["rate"] == saved.rate
    assert sales["native_prior_cohorts"] == screened["native_prior_cohorts"]
    assert sales["prior_evidence_status"] == "outside_saved_window"
    quarter_yoy = view["latest_observations"]["quarter"]["yoy"]
    assert (
        next(item for item in quarter_yoy if item["metric"] == "sales")["prior_evidence_status"]
        == "outside_saved_window"
    )
    assert not {item["path_id"] for item in discovery_paths((financial, growth, technical))} & {
        "business_expansion",
        "operating_recovery",
    }


@pytest.mark.parametrize("invalid_prior", ["missing_period", "changed_amount"])
def test_l3_labels_missing_or_invalid_prior_expected_inside_saved_window(invalid_prior):
    _, growth, _ = models(
        documents=(document(True), prior_quarter(), document()),
        pdfs=(),
        growth_start=date(2025, 1, 1),
    )
    payload = deepcopy(growth.payload)
    if invalid_prior == "missing_period":
        payload["periods"].pop(0)
    else:
        for item in payload["periods"][0]["observations"]:
            if item["measurement"] in {"quarter", "reported"}:
                item["metrics"]["sales"]["value"] += 1
    before = canonical_json(payload)
    result = project_company_growth(payload, ticker=growth.subject, periods=1)
    quarter = result["latest_observations"]["quarter"]["yoy"]
    (sales,) = [item for item in quarter if item["metric"] == "sales"]
    assert sales["rate"] > 0
    assert sales["prior_evidence_status"] == "missing_or_invalid_saved_prior"
    table = result["reported_yoy"]
    (reported,) = [row for row in table["rows"] if row[2] == "sales"]
    assert (
        reported[table["columns"].index("prior_evidence_status")]
        == "missing_or_invalid_saved_prior"
    )
    assert canonical_json(payload) == before


def test_recovery_does_not_pair_other_same_date_native_or_pdf_provenance():
    documents = (document(True), prior_quarter(), document())

    def comparisons(generated):
        reader = CompanyGrowthReader(_growth_policy_inputs(generated[1].payload))
        current = reader.observation(reader.latest_period, "quarter")
        return {
            metric: reader.saved_change(reader.year_over_year(current, metric)[0])
            for metric in ("operating_profit", "operating_margin")
        }

    native = comparisons(models(documents=documents, pdfs=()))
    pdf = comparisons(models(documents=documents))
    assert _recovery_available(native, cohort_bound=True)
    assert _recovery_available(pdf, cohort_bound=True)
    assert not _recovery_available(
        {**pdf, "operating_profit": native["operating_profit"]}, cohort_bound=True
    )
    assert not _recovery_available(
        {**pdf, "operating_profit": {**pdf["operating_profit"], "source_sha256": "d" * 64}},
        cohort_bound=True,
    )
    other_cohort = deepcopy(native["operating_profit"]["native_prior_cohorts"])
    other_cohort[0][2] = "2026-06-30"
    mismatched = {**native["operating_profit"], "native_prior_cohorts": other_cohort}
    assert not _recovery_available(
        {**native, "operating_profit": mismatched},
        cohort_bound=True,
    )
    assert (
        _interval_check(native["operating_profit"], native["operating_margin"], cohort_bound=True)
        == "met"
    )
    assert _interval_check(mismatched, native["operating_margin"], cohort_bound=True) == "missing"
    assert _interval_check(mismatched, native["operating_margin"], cohort_bound=False) == "met"
