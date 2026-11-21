"""Explicit IFRS half-year display statements; amounts and receipts are synthetic."""

from datetime import UTC, date, datetime

import pytest
from tests.helpers.pdf_statements import base

from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    build_pdf_supplement,
    extract_pages,
)


def interim_pages():
    return [
        (
            "2027年3月期 第2四半期(中間期)決算短信〔IFRS会計基準〕(連結)\n"
            "2026年11月11日\n上場会社名\nコード番号 9268\n(百万円未満切捨て)\n"
            "2027年3月期中間期 200,000 25.0 18,000 50.0 20,000 33.3 14,000 40.0 13,500 50.0 15,000 50.0\n"
            "2026年3月期中間期 160,000 - 12,000 - 15,000 - 10,000 - 9,000 - 10,000 -\n"
            "基本的1株当たり"
        ),
        (
            "前中間連結会計期間及び前連結会計年度の財務数値についても、"
            "IFRS会計基準に組替えて表示しています。"
        ),
        (
            "(要約中間連結損益計算書)\n(単位:百万円)\n"
            "前中間連結会計期間\n2025年4月1日\n2025年9月30日\n"
            "当中間連結会計期間\n2026年4月1日\n2026年9月30日\n"
            "売上収益 160,000 200,000\n営業利益 12,000 18,000\n親会社の所有者 9,000 13,500"
        ),
    ]


def interim_base():
    return base().model_copy(
        update={
            "received_at": datetime(2026, 11, 14, tzinfo=UTC),
            "disclosure_number": "20261111500000",
            "source_url": "https://www2.jpx.co.jp/disc/92680/140120261111500000.pdf",
            "resource_name": "pdf:20261111500000",
        }
    )


@pytest.mark.parametrize("standard", ["IFRS", "IFRS会計基準"])
@pytest.mark.parametrize("sales", ["売上高", "売上収益"])
def test_explicit_interim_statement_preserves_period_unit_and_pair(standard, sales):
    pages = interim_pages()
    pages[0] = pages[0].replace("IFRS会計基準", standard)
    pages[2] = pages[2].replace("売上収益", sales)
    item = extract_pages(interim_base(), pages)
    assert item.reason is None
    assert item.current_start == date(2026, 4, 1)
    assert item.current_end == date(2026, 9, 30)
    assert item.comparative_start == date(2025, 4, 1)
    assert item.comparative_end == date(2025, 9, 30)
    assert item.accounting_standard == "IFRS"
    assert item.statement_basis == "consolidated"
    assert item.unit == "JPY_million"
    assert item.rounding == "truncate_below_unit"
    assert item.pairs[0].row == sales
    assert [(p.metric, p.current, p.comparative) for p in item.pairs] == [
        ("sales", 200_000, 160_000),
        ("operating_profit", 18_000, 12_000),
        ("parent_profit", 13_500, 9_000),
    ]
    assert item.comparative_basis_page == 2
    assert item.source_sha256 == interim_base().source_sha256


@pytest.mark.parametrize(
    "basis",
    [
        "前中間連結会計期間の数値についても、IFRSベースに組み替えて比較分析",
        (
            "前中間連結会計期間及び前連結会計年度の財務数値についても"
            "IFRS会計基準に組替えて比較分析を行っています。"
        ),
    ],
)
def test_interim_comparative_basis_must_be_explicit(basis):
    pages = interim_pages()
    pages[1] = basis
    item = extract_pages(interim_base(), pages)
    assert item.reason is None
    assert item.comparative_basis_text in basis


@pytest.mark.parametrize("period", ["前連結会計年度", "前第1四半期連結累計期間"])
def test_interim_does_not_borrow_restatement_basis_from_another_period(period):
    pages = interim_pages()
    pages[1] = f"{period}の数値についても、IFRSベースに組み替えて比較分析"
    item = extract_pages(interim_base(), pages)
    assert item.reason == "pdf_comparative_basis_not_explicit"
    assert not item.pairs


@pytest.mark.parametrize(
    ("prior", "current"),
    [
        ("前連結会計年度", "当中間連結会計期間"),
        ("前第1四半期連結累計期間", "当中間連結会計期間"),
        ("前中間連結会計期間", "当連結会計年度"),
        ("前中間連結会計期間", "当第1四半期連結累計期間"),
        ("前連結会計年度", "当連結会計年度"),
        ("前第1四半期連結累計期間", "当第1四半期連結累計期間"),
        ("当中間連結会計期間", "前中間連結会計期間"),
        ("前中間連結会計期間及び前連結会計年度", "当中間連結会計期間"),
    ],
)
def test_interim_requires_unambiguous_prior_and_current_interim_column_roles(prior, current):
    pages = interim_pages()
    pages[2] = pages[2].replace("前中間連結会計期間", "PRIOR_ROLE")
    pages[2] = pages[2].replace("当中間連結会計期間", current).replace("PRIOR_ROLE", prior)
    item = extract_pages(interim_base(), pages)
    assert item.reason == "pdf_column_roles_unsupported"
    assert not item.pairs


@pytest.mark.parametrize(
    ("page", "old", "new", "reason"),
    [
        (0, "IFRS会計基準", "USGAAP", "pdf_accounting_basis_unsupported"),
        (1, "前中間連結会計期間", "当中間連結会計期間", "pdf_comparative_basis_not_explicit"),
        (1, "組替えて", "そのまま", "pdf_comparative_basis_not_explicit"),
        (2, "当中間連結会計期間", "比較対象不明", "pdf_column_roles_unsupported"),
        (0, "2027年3月期中間期", "2027年3月期第1四半期", "pdf_headline_period_conflict"),
        (2, "2025年9月30日", "2025年6月30日", "pdf_comparative_period_mismatch"),
        (2, "18,000", "18,001", "pdf_headline_statement_conflict:operating_profit"),
    ],
)
def test_interim_unknown_basis_columns_or_period_do_not_create_operands(page, old, new, reason):
    pages = interim_pages()
    pages[page] = pages[page].replace(old, new)
    item = extract_pages(interim_base(), pages)
    assert item.reason == reason
    assert not item.pairs


def test_duplicate_statement_or_sales_label_remains_ambiguous():
    pages = interim_pages()
    item = extract_pages(interim_base(), [*pages, pages[2]])
    assert item.reason == "pdf_statement_missing_or_ambiguous"
    pages[2] += "\n売上高 160,000 200,000"
    item = extract_pages(interim_base(), pages)
    assert item.reason == "pdf_metric_missing_or_ambiguous:sales"


def test_one_interim_statement_does_not_create_quarter_or_ttm():
    item = extract_pages(interim_base(), interim_pages())
    result = build_pdf_supplement([item], as_of=date(2026, 11, 14))
    assert result is not None
    assert not result.ttm
    assert {p.metric: p.amount for p in result.comparisons} == {
        "sales": 40_000,
        "operating_profit": 6_000,
        "parent_profit": 4_500,
    }
    assert all(p.period_start == date(2026, 4, 1) for p in result.comparisons)
    assert all(p.period_end == date(2026, 9, 30) for p in result.comparisons)


def test_legacy_statement_keeps_its_recorded_extractor_revision():
    old = interim_base().model_dump(mode="json")
    old["extractor_revision"] = "pdf-restated-display-statements-v3"
    old["reason"] = "pdf_accounting_basis_unsupported"
    assert PdfStatement.model_validate(old).model_dump(mode="json") == old


def test_models_recover_only_reported_half_year_not_quarter_or_mixed_basis_ttm():
    documents = []
    for year, period, standard, sales, operating, profit in [
        (2025, "1Q", "JP", 70_000, 3_000, 2_000),
        (2025, "2Q", "JP", 150_000, 9_000, 7_000),
        (2025, "FY", "JP", 310_000, 20_000, 15_000),
        (2026, "1Q", "IFRS", 80_000, 8_000, 6_000),
        (2026, "2Q", "IFRS", 200_000, 18_000, 13_500),
    ]:
        end = {
            "1Q": date(year, 6, 30),
            "2Q": date(year, 9, 30),
            "FY": date(year + 1, 3, 31),
        }[period]
        published = {
            "1Q": date(year, 8, 7),
            "2Q": date(year, 11, 11),
            "FY": date(year + 1, 5, 15),
        }[period]
        documents.append(
            JQuantsFinancialSummary(
                ticker="9268",
                disclosed_at=published,
                disclosed_time="15:30:00",
                disclosure_number="20261111500000"
                if (year, period) == (2026, "2Q")
                else f"synthetic-{year}-{period}",
                document_type=f"{period}FinancialStatements_Consolidated_{standard}",
                period_start=date(year, 4, 1),
                period_end=end,
                fiscal_year_end=date(year + 1, 3, 31),
                fiscal_period=period,
                sales=sales * 1_000_000,
                operating_profit=operating * 1_000_000,
                profit=profit * 1_000_000,
            )
        )

    def generate(statements):
        return generate_financial_and_price_models(
            ticker="9268",
            documents=documents,
            pdf_statements=statements,
            adjusted_bars=[],
            adjustment_events=[],
            trading_dates=[],
            expected_price_date=None,
            sector="卸売業",
            as_of=date(2026, 11, 14),
            input_release=InputRelease(release_id="synthetic", manifest_sha256="a" * 64),
            ttm_rules=DEFAULT_TTM_RULES,
        )[:2]

    before_financial, before_growth = generate([])
    statement = extract_pages(interim_base(), interim_pages())
    financial, growth = generate([statement])

    def change(model, measurement):
        return next(
            row
            for row in model.payload["changes"]
            if row["to_end"] == "2026-09-30"
            and row["measurement"] == measurement
            and row["metric"] == "operating_profit"
        )

    assert change(before_growth, "reported")["reason"] == "accounting_standard_incomparable"
    reported = change(growth, "reported")
    assert reported["from_value"] == 12_000_000_000
    assert reported["difference"] == 6_000_000_000
    assert reported["reason"] is None
    assert reported["source_sha256"] == statement.source_sha256
    assert reported["precision"] == "derived_from_truncated_display_amounts"
    assert change(growth, "quarter") == change(before_growth, "quarter")
    assert change(growth, "quarter")["difference"] is None
    assert change(growth, "quarter")["reason"] == "accounting_standard_incomparable"
    assert financial.payload["earnings_price"]["profit_ttm_yen"] is None
    assert before_financial.payload["earnings_price"]["profit_ttm_yen"] is None
    assert not growth.payload["pdf_supplement"]["ttm"]
    old_period = next(row for row in growth.payload["periods"] if row["period_end"] == "2025-09-30")
    old_reported = next(
        row for row in old_period["observations"] if row["measurement"] == "reported"
    )
    assert old_reported["accounting_standard"] == "JP"
    assert old_reported["metrics"]["operating_profit"]["value"] == 9_000_000_000
    assert "observations-v18-pdf-jp-interim" in financial.calculation_revision
    assert growth.calculation_revision == "company_growth_retrospective_v25-pdf-jp-interim"
