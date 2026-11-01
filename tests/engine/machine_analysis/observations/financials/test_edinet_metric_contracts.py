"""現行EDINET財務metric抽出の回帰契約。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path

from tests.helpers.edinet_csv_zip import _edinet_csv_zip
from tests.helpers.edinet_documents import store_edinet_documents

from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import (
    select_document_candidates,
)
from baibai_engine.machine_analysis.observations.financials.edinet_decode import (
    normalize_metric_record,
    parse_sec_code,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    EDINET_METRIC_REVISION,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.write import (
    store_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality

ROOT = Path(__file__).resolve().parents[5]


class EDINETMetricTests(unittest.TestCase):
    def test_parse_sec_code_supports_alpha_numeric_code(self) -> None:
        self.assertEqual(parse_sec_code("130A0"), "130A")

    def test_parse_sec_code_rejects_non_zero_suffix(self) -> None:
        with self.assertRaises(EDINETProviderError):
            parse_sec_code("130A1")

    def test_parse_sec_code_sanitizes_control_characters_in_error(self) -> None:
        with self.assertRaises(EDINETProviderError) as ctx:
            parse_sec_code("7203\n0")

        self.assertNotIn("\n", str(ctx.exception))
        self.assertIn("'7203?0'", str(ctx.exception))

    def test_normalize_metric_record_parses_ttm_quality(self) -> None:
        record = normalize_metric_record(
            {
                "ticker": "7203",
                "sales_ttm": "100",
                "ocf_ttm": "10",
                "debt": "20",
                "cash": "5",
                "ebitda_ttm": "30",
                "ttm_quality_ev_ebitda": "exact",
                "ttm_quality_p_s": "approximated",
                "ttm_quality_pcfr": "unavailable",
                "source_submit_datetime": "2026-04-01 12:00",
                "source_period_start": "2025-04-01",
                "source_period_end": "2026-03-31",
            }
        )
        self.assertEqual(record.ttm_quality_ev_ebitda, TTMQuality.EXACT)
        self.assertEqual(record.ttm_quality_p_s, TTMQuality.APPROXIMATED)
        self.assertEqual(record.ttm_quality_pcfr, TTMQuality.UNAVAILABLE)
        self.assertEqual(record.source_submit_datetime, "2026-04-01 12:00")
        self.assertEqual(record.source_period_start, date(2025, 4, 1))
        self.assertEqual(record.source_period_end, date(2026, 3, 31))

    def test_parse_csv_zip_metric_record_extracts_net_cash_and_fcf(self) -> None:
        rows = [
            ("jppfs_cor:NetSales", "CurrentYearDuration", "1000"),
            (
                "jppfs_cor:NetCashProvidedByUsedInOperatingActivities",
                "CurrentYearDuration",
                "150",
            ),
            ("jppfs_cor:OperatingIncome", "CurrentYearDuration", "90"),
            ("jppfs_cor:DepreciationAndAmortizationOpeCF", "CurrentYearDuration", "30"),
            (
                "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF",
                "CurrentYearDuration",
                "-40",
            ),
            ("jppfs_cor:PurchaseOfIntangibleAssetsInvCF", "CurrentYearDuration", "0"),
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "300"),
            ("jppfs_cor:InvestmentSecurities", "CurrentYearInstant", "250"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "20"),
            ("jppfs_cor:LongTermLoansPayable", "CurrentYearInstant", "50"),
            ("jppfs_cor:NetAssets", "CurrentYearInstant", "800"),
            ("jppfs_cor:Assets", "CurrentYearInstant", "1400"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
            submit_datetime="2026-04-01 12:00",
            period_start=date(2025, 4, 1),
            period_end=date(2026, 3, 31),
        )

        self.assertEqual(record.sales_ttm, 1000.0)
        self.assertEqual(record.ocf_ttm, 150.0)
        self.assertEqual(record.debt, 70.0)
        self.assertEqual(record.cash, 300.0)
        self.assertEqual(record.net_cash, 230.0)
        self.assertEqual(record.investment_securities, 250.0)
        self.assertEqual(record.capex_ttm, 40.0)
        self.assertEqual(record.fcf_ttm, 110.0)
        self.assertEqual(record.ebitda_ttm, 120.0)
        self.assertEqual(record.ttm_quality_fcf, TTMQuality.EXACT)
        self.assertEqual(record.source_submit_datetime, "2026-04-01 12:00")
        self.assertEqual(record.source_period_start, date(2025, 4, 1))
        self.assertEqual(record.source_period_end, date(2026, 3, 31))

    def test_investment_securities_uses_exact_tag_and_consolidated_basis(self) -> None:
        rows = [
            ("jppfs_cor:InvestmentSecurities", "CurrentYearInstant", "250"),
            ("jppfs_cor:InvestmentSecurities", "CurrentYearInstant_NonConsolidatedMember", "900"),
            (
                "jppfs_cor:SharesOfSubsidiariesAndAssociates",
                "CurrentYearInstant",
                "700",
            ),
            (
                "jpcrp_cor:OperationalInvestmentSecurities",
                "CurrentYearInstant",
                "800",
            ),
            (
                "jppfs_cor:GainOnSaleOfInvestmentSecurities",
                "CurrentYearDuration",
                "1000",
            ),
        ]

        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertEqual(record.investment_securities, 250.0)
        self.assertNotIn("metric_unavailable:investment_securities", record.failure_reasons)

    def test_investment_securities_excludes_similar_tags_and_accepts_reported_zero(self) -> None:
        excluded = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(
                [
                    (
                        "jpcrp_cor:OperationalInvestmentSecurities",
                        "CurrentYearInstant",
                        "800",
                    ),
                    (
                        "jppfs_cor:ProceedsFromSaleOfInvestmentSecurities",
                        "CurrentYearDuration",
                        "900",
                    ),
                ]
            ),
        )
        zero = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100ZERO",
            doc_type_code="120",
            content=_edinet_csv_zip(
                [("jppfs_cor:InvestmentSecurities", "CurrentYearInstant", "-")]
            ),
        )

        self.assertIsNone(excluded.investment_securities)
        self.assertIn("metric_unavailable:investment_securities", excluded.failure_reasons)
        self.assertEqual(zero.investment_securities, 0.0)

    def test_investment_securities_rejects_negative_value(self) -> None:
        for value in ("-1", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_csv_zip_metric_record(
                    ticker="7203",
                    doc_id="S100INVALID",
                    doc_type_code="120",
                    content=_edinet_csv_zip(
                        [
                            (
                                "jppfs_cor:InvestmentSecurities",
                                "CurrentYearInstant",
                                value,
                            )
                        ]
                    ),
                )

    def test_parse_csv_zip_metric_record_extracts_loan_payable_and_lease_debt(self) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "1000"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "120"),
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant", "30"),
            ("jppfs_cor:LeaseAssetsPPE", "CurrentYearInstant", "999"),
            ("jppfs_cor:LongTermLoansReceivable", "CurrentYearInstant", "888"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertEqual(record.debt, 150.0)
        self.assertEqual(record.net_cash, 850.0)
        self.assertNotIn("metric_unavailable:debt", record.failure_reasons)

    def test_parse_csv_zip_metric_record_keeps_missing_debt_unavailable(
        self,
    ) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "1000"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertIsNone(record.debt)
        self.assertIsNone(record.net_cash)
        self.assertEqual(record.ttm_quality_net_cash, TTMQuality.UNAVAILABLE)
        self.assertIn("metric_unavailable:debt", record.failure_reasons)

    def test_parse_csv_zip_metric_record_allows_reported_zero_debt(self) -> None:
        rows = [
            ("jppfs_cor:CashAndDeposits", "CurrentYearInstant", "1000"),
            ("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "-"),
            ("jppfs_cor:LeaseObligationsCL", "CurrentYearInstant", "-"),
        ]
        record = parse_csv_zip_metric_record(
            ticker="7203",
            doc_id="S100TEST",
            doc_type_code="120",
            content=_edinet_csv_zip(rows),
        )

        self.assertEqual(record.debt, 0.0)
        self.assertEqual(record.net_cash, 1000.0)
        self.assertEqual(record.ttm_quality_net_cash, TTMQuality.EXACT)
        self.assertNotIn("metric_unavailable:debt", record.failure_reasons)

    def test_edinet_reader_reads_cached_metrics_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "data" / "screening" / "market.sqlite"
            document = {
                "seqNumber": 1,
                "docID": "S100SAVED",
                "secCode": "72030",
                "docTypeCode": "120",
                "csvFlag": "1",
                "xbrlFlag": "1",
                "legalStatus": "1",
                "disclosureStatus": "0",
                "withdrawalStatus": "0",
                "docInfoEditStatus": "0",
                "periodStart": "2025-04-01",
                "periodEnd": "2026-03-31",
                "submitDateTime": "2026-05-01 12:00",
            }
            store_edinet_documents(sqlite_path, date(2026, 5, 1), [document])
            candidate = select_document_candidates([document]).candidates["7203"]
            store_edinet_metrics(
                sqlite_path,
                date(2026, 5, 1),
                [
                    {
                        "ticker": "7203",
                        "cash": 100.0,
                        "debt": 10.0,
                        "source_doc_id": candidate.doc_id,
                        "document_type": candidate.doc_type_code,
                        "source_submit_datetime": candidate.submit_datetime,
                        "source_period_start": candidate.period_start,
                        "source_period_end": candidate.period_end,
                        "extractor_revision": EDINET_METRIC_REVISION,
                        "source_document_revision": candidate.source_document_revision,
                    }
                ],
            )

            reader = EDINETReader(sqlite_path)
            records = reader.load_metric_records(date(2026, 5, 1))

        self.assertEqual(records["7203"].cash, 100.0)
        self.assertEqual(records["7203"].debt, 10.0)


def test_capex_original_edinet_rows():
    # EDINET API v2 documents/{doc_id}?type=5、2026-09-17取得の原本CSV行を抜粋。
    # S100YU8C InterimDurationは2026-01-01..06-30 (会社半期報告書id=500)。
    cases = [
        ("5946", "S100YU8C", "160", 4_857_000_000, 1_136_000_000, 3_721_000_000, "approximated"),
        ("7122", "S100YI50", "120", 15_630_000_000, 1_831_000_000, 13_799_000_000, "approximated"),
        ("8127", "S100XXXU", "160", 871_844_000, 100_902_000, 770_942_000, "approximated"),
    ]
    for ticker, doc, kind, ocf, capex, fcf, quality in cases:
        content = BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr(
                "XBRL_TO_CSV/statement.csv",
                (ROOT / "tests/fixtures/edinet" / f"{doc}.tsv").read_text().encode("utf-16"),
            )
        record = parse_csv_zip_metric_record(
            ticker=ticker,
            doc_id=doc,
            doc_type_code=kind,
            content=content.getvalue(),
            period_end=date(2026, 12, 31) if ticker == "5946" else None,
        )
        assert (record.ocf_ttm, record.capex_ttm, record.fcf_ttm) == (ocf, capex, fcf)
        assert record.ttm_quality_fcf == quality
        assert record.capex_source == "purchase_of_fixed_assets"
        assert "metric_unavailable:capex" not in record.failure_reasons


def test_annual_cash_flow_details_from_original_edinet_rows():
    cases = (
        (
            "2415",
            "S100YIDK",
            "120",
            (-54_148_000, 31_636_000, -12_897_000, 21_080_000, None, 241_674_000),
            (741_370_000, 263_164_000),
            (3_282_582_000, 1_004_534_000, 2_278_048_000),
        ),
        (
            "4231",
            "S100YK4M",
            "120",
            (-654_271_000, -459_700_000, -409_250_000, None, None, None),
            (None, None),
            (2_416_413_000, 3_577_928_000, -1_161_515_000),
        ),
        (
            "1814",
            "S100YK21",
            "120",
            (1_239_000_000, None, 4_078_000_000, None, None, 1_021_000_000),
            (157_000_000, 95_000_000),
            (9_299_000_000, 252_000_000, 9_047_000_000),
        ),
        (
            "7991",
            "S100YPE4",
            "130",
            (975_027_000, 119_225_000, -1_351_721_000, None, None, None),
            (3_415_260_000, 43_566_000),
            (-715_467_000, 3_458_826_000, -4_174_293_000),
        ),
        (
            "7122",
            "S100YI50",
            "120",
            (223_000_000, -4_537_000_000, 1_802_000_000, 14_489_000_000, None, None),
            (1_679_000_000, 152_000_000),
            (15_630_000_000, 1_831_000_000, 13_799_000_000),
        ),
    )
    for ticker, doc, kind, effects, capex_parts, totals in cases:
        content = BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr(
                "XBRL_TO_CSV/statement.csv",
                (ROOT / "tests" / "fixtures" / "edinet" / f"{doc}.tsv")
                .read_text()
                .encode("utf-16"),
            )
        record = parse_csv_zip_metric_record(
            ticker=ticker,
            doc_id=doc,
            doc_type_code=kind,
            content=content.getvalue(),
        )
        assert (
            record.ocf_receivables_cash_effect,
            record.ocf_inventories_cash_effect,
            record.ocf_payables_cash_effect,
            record.ocf_contract_liabilities_cash_effect,
            record.ocf_advances_received_cash_effect,
            record.ocf_other_payables_cash_effect,
        ) == effects
        assert (record.capex_ppe_reported, record.capex_intangible_reported) == capex_parts
        assert (record.ocf_ttm, record.capex_ttm, record.fcf_ttm) == totals


def test_cash_flow_details_require_annual_jpy_unique_current_context():
    csv_text = "\n".join(
        (
            "要素ID\tコンテキストID\t連結・個別\tユニットID\t値",
            "jppfs_cor:NetCashProvidedByUsedInOperatingActivities\tCurrentYearDuration\t連結\tJPY\t100",
            "jppfs_cor:DecreaseIncreaseInNotesAndAccountsReceivableTradeOpeCF\tCurrentYearDuration\t連結\tJPY\t-",
            "jppfs_cor:DecreaseIncreaseInInventoriesOpeCF\tCurrentYearDuration\t連結\tJPY\t10",
            "jppfs_cor:DecreaseIncreaseInInventoriesOpeCF\tCurrentYearDuration\t連結\tJPY\t11",
            "jppfs_cor:IncreaseDecreaseInNotesAndAccountsPayableTradeOpeCF\tCurrentYearDuration\t連結\tUSD\t12",
            "jppfs_cor:IncreaseDecreaseInContractLiabilitiesOpeCF\tPrior1YearDuration\t連結\tJPY\t13",
        )
    )
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("XBRL_TO_CSV/statement.csv", csv_text.encode("utf-16"))
    annual = parse_csv_zip_metric_record(
        ticker="9999", doc_id="S100TEST", doc_type_code="120", content=content.getvalue()
    )
    assert annual.ocf_receivables_cash_effect == 0
    assert annual.ocf_inventories_cash_effect is None
    assert annual.ocf_payables_cash_effect is None
    assert annual.ocf_contract_liabilities_cash_effect is None

    interim = parse_csv_zip_metric_record(
        ticker="9999", doc_id="S100TEST", doc_type_code="160", content=content.getvalue()
    )
    assert interim.ocf_receivables_cash_effect is None
    assert interim.capex_ppe_reported is None


def test_capex_total_components_and_period_are_not_mixed(subtests):
    total = "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF"
    tangible = "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF"
    intangible = "jppfs_cor:PurchaseOfIntangibleAssetsInvCF"
    current = "InterimDuration"
    prior = "Prior1InterimDuration"
    rows = [("jppfs_cor:NetCashProvidedByUsedInOperatingActivities", current, "4857")]
    cases = [
        ("total", [(total, current, "-1136")], 1136),
        ("components", [(tangible, current, "-1000"), (intangible, current, "-136")], 1136),
        (
            "total-and-components",
            [
                (total, current, "-1136"),
                (tangible, current, "-1000"),
                (intangible, current, "-136"),
            ],
            1136,
        ),
        (
            "blank-total-does-not-fall-back",
            [(total, current, ""), (tangible, current, "-1000"), (intangible, current, "-136")],
            None,
        ),
        (
            "conflicting-total-does-not-fall-back",
            [
                (total, current, "-1136"),
                (total, current, "-2000"),
                (tangible, current, "-1000"),
                (intangible, current, "-136"),
            ],
            None,
        ),
        (
            "explicit-zero-total-precedes-components",
            [(total, current, "-"), (tangible, current, "-1000"), (intangible, current, "-136")],
            0,
        ),
        ("missing-intangible", [(tangible, current, "-1000")], None),
        ("missing-tangible", [(intangible, current, "-136")], None),
        ("prior-component", [(tangible, current, "-1000"), (intangible, prior, "-136")], None),
        (
            "prior-total",
            [(total, prior, "-769"), (tangible, current, "-1000"), (intangible, current, "-136")],
            1136,
        ),
        ("nonconsolidated-total", [(total, "InterimDuration_NonConsolidatedMember", "-999")], None),
        ("missing", [], None),
        ("explicit-zero", [(total, current, "0")], 0),
    ]
    for name, extra, expected in cases:
        with subtests.test(name=name):
            record = parse_csv_zip_metric_record(
                ticker="9999",
                doc_id="S100TEST",
                doc_type_code="160",
                content=_edinet_csv_zip(
                    rows
                    + extra
                    + [("jppfs_cor:PurchaseOfInvestmentSecuritiesInvCF", current, "-9999")]
                ),
            )
            assert record.capex_ttm == expected
            assert record.fcf_ttm == (None if expected is None else 4857 - expected)
            assert ("metric_unavailable:capex" in record.failure_reasons) == (expected is None)
            assert "metric_unavailable:cash" in record.failure_reasons


def test_missing_edinet_quality_is_unknown():
    record = normalize_metric_record(
        {"ticker": "5946", "ebitda_ttm": 1328000000, "fcf_ttm": 3721000000}
    )
    assert record.ttm_quality_ev_ebitda is None
    assert record.ttm_quality_fcf is None
