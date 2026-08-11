from __future__ import annotations

import csv
import io
import unittest
import zipfile
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.screening.capital_control import JPXDelistingRow, store_jpx_delistings
from baibai_engine.screening.sqlite_cache import open_connection
from baibai_engine.screening.tender_offer import (
    TenderOfferError,
    build_control_event_exit_values,
    parse_ordinary_share_offer_price,
    pays_in_cash_only,
    read_document_blocks,
    read_tender_offer_exit_values,
    read_tender_offer_outcome,
    store_tender_offer_exit_values,
)

_PRICE = "jptoo-ton_cor:PriceOfPurchaseEtcTextBlock"
_FUNDING = "jptoo-ton_cor:FundEtcForPurchaseEtcTextBlock"
_CASH_ONLY = (
    "買付代金(円)(a)14,175,561,260金銭以外の対価の種類―金銭以外の対価の総額―"
    "買付手数料(b)85,000,000その他(c)10,800,000合計14,271,361,260"
)
_OUTCOME = "jptoo-tor_cor:SuccessOrFailureOfTenderOfferTextBlock"

_SUCCESS = (
    "本公開買付けにおいては、応募株券等の数の合計が買付予定数の下限(4,165,800株)に満たない"
    "場合は、応募株券等の全部の買付け等を行わない旨の条件を付しておりましたが、応募株券等の"
    "数の合計(7,290,251株)が買付予定数の下限以上となりましたので、応募株券等の全部の買付け等"
    "を行います。"
)
_FAILURE = (
    "本公開買付けにおいては、応募株券等の数の合計が買付予定数の下限(849,600株)に満たない"
    "場合は、応募株券等の全部の買付け等を行わない旨の条件を付しておりましたが、応募株券等の"
    "数の合計(721,295株)が買付予定数の下限に満たなかったため、応募株券等の全部の買付け等を"
    "行いません。"
)


def _archive(blocks: dict[str, str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t")
    writer.writerow(("要素ID", "項目名", "値"))
    for element, value in blocks.items():
        writer.writerow((element, "項目", value))
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/jptoo-001.csv", buffer.getvalue().encode("utf-16"))
    return zip_buffer.getvalue()


class OfferPriceTest(unittest.TestCase):
    """Every real spelling of the price row the delisting cohort actually contained."""

    def test_reads_the_price_when_the_class_label_is_only_the_share_certificate(self) -> None:
        blocks = {
            _PRICE: (
                "（２）【買付け等の価格】株券１株につき金2,400円新株予約権証券―"
                "新株予約権付社債券―算定の基礎　公開買付者は、本公開買付価格の公正性を…"
            )
        }
        self.assertEqual(parse_ordinary_share_offer_price(blocks), 2400.0)

    def test_reads_the_price_across_a_full_width_space_before_the_share_unit(self) -> None:
        blocks = {_PRICE: "株券普通株式　１株につき、金2,950円新株予約権証券―算定の基礎…"}
        self.assertEqual(parse_ordinary_share_offer_price(blocks), 2950.0)

    def test_reads_the_price_when_the_yen_marker_is_dropped(self) -> None:
        blocks = {_PRICE: "株券普通株式１株につき、9,850円新株予約権証券―算定の基礎…"}
        self.assertEqual(parse_ordinary_share_offer_price(blocks), 9850.0)

    def test_a_warrant_priced_per_unit_is_not_read_as_the_share_price(self) -> None:
        blocks = {
            _PRICE: (
                "株券普通株式　１株につき金1,950円新株予約権証券第３回新株予約権　"
                "１個につき金4,389円新株予約権付社債券－算定の基礎…"
            )
        }
        self.assertEqual(parse_ordinary_share_offer_price(blocks), 1950.0)

    def test_a_reference_price_in_the_valuation_narrative_is_not_a_candidate(self) -> None:
        blocks = {
            _PRICE: (
                "株券普通株式１株につき金1,000円新株予約権証券―算定の基礎"
                "第三者算定機関は１株につき金1,800円から金2,400円のレンジを示しました。"
            )
        }
        self.assertEqual(parse_ordinary_share_offer_price(blocks), 1000.0)

    def test_two_share_prices_in_the_table_are_refused(self) -> None:
        blocks = {
            _PRICE: ("株券普通株式１株につき金1,000円Ａ種優先株式１株につき金2,000円算定の基礎…")
        }
        self.assertIsNone(parse_ordinary_share_offer_price(blocks))

    def test_a_sub_yen_price_is_refused_rather_than_truncated(self) -> None:
        blocks = {_PRICE: "株券普通株式１株につき金1,000円50銭新株予約権証券―算定の基礎…"}
        self.assertIsNone(parse_ordinary_share_offer_price(blocks))

    def test_a_missing_price_block_is_absent_rather_than_zero(self) -> None:
        self.assertIsNone(parse_ordinary_share_offer_price({}))


class OfferOutcomeTest(unittest.TestCase):
    def test_a_completed_offer_reads_as_bought(self) -> None:
        self.assertIs(read_tender_offer_outcome({_OUTCOME: _SUCCESS}), True)

    def test_an_offer_below_its_floor_reads_as_not_bought(self) -> None:
        self.assertIs(read_tender_offer_outcome({_OUTCOME: _FAILURE}), False)

    def test_the_shorter_wording_without_the_etc_particle_still_reads(self) -> None:
        text = _SUCCESS.replace("買付け等を行います", "買付けを行います")
        self.assertIs(read_tender_offer_outcome({_OUTCOME: text}), True)

    def test_a_block_stating_both_outcomes_is_refused(self) -> None:
        self.assertIsNone(read_tender_offer_outcome({_OUTCOME: _SUCCESS + _FAILURE}))

    def test_a_missing_outcome_block_is_unknown(self) -> None:
        self.assertIsNone(read_tender_offer_outcome({}))


class _Provider:
    """Serves prepared archives and records what the build actually downloaded."""

    def __init__(self, archives: dict[str, bytes]) -> None:
        self._archives = archives
        self.requested: list[str] = []

    def download_csv_zip(self, doc_id: str) -> bytes:
        self.requested.append(doc_id)
        return self._archives[doc_id]


class ControlEventExitBuildTest(unittest.TestCase):
    ASOF = date(2026, 8, 10)
    DELISTED_ON = date(2026, 5, 1)

    def _store(self, tmp: str, documents: list[tuple[object, ...]], *, reason: str) -> Path:
        sqlite_path = Path(tmp) / "market.sqlite"
        connection = open_connection(sqlite_path)
        connection.executemany(
            "INSERT INTO edinet_documents("
            "doc_date, sequence_number, doc_id, sec_code, doc_type_code, submit_datetime, "
            "legal_status, disclosure_status, withdrawal_status, edinet_code, "
            "subject_edinet_code) VALUES (?, ?, ?, ?, ?, ?, '1', '0', '0', ?, ?)",
            [
                # The target identifies itself through its own annual report.
                ("2026-01-05", 1, "SELF", "20000", "120", "2026-01-05T09:00", "TARGET", None),
                *documents,
            ],
        )
        connection.commit()
        connection.close()
        store_jpx_delistings(
            sqlite_path,
            [
                JPXDelistingRow(
                    delisted_on=self.DELISTED_ON,
                    ticker="2000",
                    name="テスト",
                    market="プライム",
                    reason=reason,
                )
            ],
        )
        return sqlite_path

    def _registration_and_result(self) -> list[tuple[object, ...]]:
        return [
            ("2026-02-01", 1, "REG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
            ("2026-03-20", 1, "RES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
        ]

    def test_a_completed_offer_becomes_an_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(summary.tender_offer_delisting_count, 1)
            self.assertEqual(summary.resolved_count, 1)
            self.assertEqual(values[0].ticker, "2000")
            self.assertEqual(values[0].offer_price_yen, 1060.0)
            self.assertEqual(values[0].delisted_on, self.DELISTED_ON)

    def test_a_delisting_jpx_does_not_attribute_to_a_tender_offer_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="株式等売渡請求による取得"
            )
            provider = _Provider({})

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(summary.tender_offer_delisting_count, 0)
            self.assertEqual(values, ())
            self.assertEqual(provider.requested, [])

    def test_a_withdrawn_offer_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    ("2026-03-01", 1, "WDR", None, "260", "2026-03-01T09:00", "BIDDER", "TARGET"),
                ],
                reason="ＭＢＯ（公開買付け、株式併合）",
            )
            provider = _Provider({})

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"withdrawn": 1})

    def test_an_offer_that_missed_its_floor_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "RES": _archive({_OUTCOME: _FAILURE}),
                }
            )

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})
            # The price is never read once the offer is known not to have bought.
            self.assertEqual(provider.requested, ["RES"])

    def test_two_offerors_over_one_target_produce_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    ("2026-02-10", 1, "REG2", None, "240", "2026-02-10T09:00", "RIVAL", "TARGET"),
                    ("2026-03-25", 1, "RES2", None, "270", "2026-03-25T09:00", "RIVAL", "TARGET"),
                ],
                reason="他社による買収（公開買付け、株式併合）",
            )
            provider = _Provider({})

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"multiple_offerors": 1})

    def test_a_two_tier_offer_at_two_prices_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "REG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    ("2026-02-05", 1, "REG_B", None, "240", "2026-02-05T09:00", "BIDDER", "TARGET"),
                    ("2026-03-20", 1, "RES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="他社による買収（公開買付け、株式併合）",
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,650円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "REG_B": _archive(
                        {_PRICE: "株券普通株式１株につき金1,240円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})

    def test_a_second_tier_that_cannot_be_read_still_blocks_the_case(self) -> None:
        """A tier that is unreadable is not a tier that did not happen."""
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "REG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    ("2026-02-05", 1, "REG_B", None, "240", "2026-02-05T09:00", "BIDDER", "TARGET"),
                    ("2026-03-20", 1, "RES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="他社による買収（公開買付け、株式併合）",
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {
                            _PRICE: "株券普通株式１株につき金1,650円算定の基礎…",
                            _FUNDING: _CASH_ONLY,
                        }
                    ),
                    # The second tier states a price the table cannot resolve.
                    "REG_B": _archive({_PRICE: "株券―新株予約権証券―", _FUNDING: _CASH_ONLY}),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})

    def test_a_correction_supersedes_the_price_it_revises(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "REG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    ("2026-02-20", 1, "COR", None, "250", "2026-02-20T09:00", "BIDDER", "TARGET"),
                    ("2026-03-20", 1, "RES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="ＭＢＯ（公開買付け、株式併合）",
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "COR": _archive(
                        {_PRICE: "株券普通株式１株につき金1,300円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, _ = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values[0].offer_price_yen, 1300.0)
            self.assertEqual(values[0].offer_doc_id, "COR")

    def test_a_correction_that_leaves_the_price_alone_falls_back_to_the_registration(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "REG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    ("2026-02-20", 1, "COR", None, "250", "2026-02-20T09:00", "BIDDER", "TARGET"),
                    ("2026-03-20", 1, "RES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="ＭＢＯ（公開買付け、株式併合）",
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "COR": _archive({"jptoo-ton_cor:PeriodOfPurchaseEtcTextBlock": "期間の訂正"}),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, _ = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values[0].offer_price_yen, 1060.0)
            self.assertEqual(values[0].offer_doc_id, "REG")

    def test_an_offer_paying_partly_in_stock_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                self._registration_and_result(),
                reason="他社による買収（公開買付け、株式併合）",
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {
                            _PRICE: "株券普通株式１株につき金1,060円算定の基礎…",
                            _FUNDING: _CASH_ONLY.replace(
                                "金銭以外の対価の種類―", "金銭以外の対価の種類公開買付者株式"
                            ),
                        }
                    ),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})

    def test_a_registration_without_a_funding_table_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            provider = _Provider(
                {
                    "REG": _archive({_PRICE: "株券普通株式１株につき金1,060円算定の基礎…"}),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, _ = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())

    def test_a_target_whose_ticker_cannot_be_resolved_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            connection = open_connection(sqlite_path)
            connection.executemany(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, sec_code, doc_type_code, submit_datetime, "
                "legal_status, disclosure_status, withdrawal_status, edinet_code, "
                "subject_edinet_code) VALUES (?, ?, ?, ?, ?, ?, '1', '0', '0', ?, ?)",
                self._registration_and_result(),
            )
            connection.commit()
            connection.close()
            store_jpx_delistings(
                sqlite_path,
                [
                    JPXDelistingRow(
                        delisted_on=self.DELISTED_ON,
                        ticker="2000",
                        name="テスト",
                        market="プライム",
                        reason="ＭＢＯ（公開買付け、株式併合）",
                    )
                ],
            )
            provider = _Provider({})

            values, summary = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"no_filing_in_document_window": 1})

    def test_an_empty_derivation_over_an_empty_table_is_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            open_connection(sqlite_path).close()

            store_tender_offer_exit_values(sqlite_path, ())

            self.assertEqual(read_tender_offer_exit_values(sqlite_path), ())

    def test_an_empty_derivation_does_not_clear_values_already_established(self) -> None:
        """The published copy cannot restore this table, so an empty run must not erase it."""
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            provider = _Provider(
                {
                    "REG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "RES": _archive({_OUTCOME: _SUCCESS}),
                }
            )
            values, _ = build_control_event_exit_values(
                sqlite_path, provider=provider, asof=self.ASOF
            )
            store_tender_offer_exit_values(sqlite_path, values)

            with self.assertRaises(TenderOfferError):
                store_tender_offer_exit_values(sqlite_path, ())

            self.assertEqual(len(read_tender_offer_exit_values(sqlite_path)), 1)


class DocumentBlocksTest(unittest.TestCase):
    def test_every_element_of_every_csv_member_is_flattened(self) -> None:
        blocks = read_document_blocks(_archive({_PRICE: "価格", _OUTCOME: "成否"}))
        self.assertEqual(blocks[_PRICE], "価格")
        self.assertEqual(blocks[_OUTCOME], "成否")


if __name__ == "__main__":
    unittest.main()


class CashConsiderationTest(unittest.TestCase):
    def test_a_dash_in_the_non_cash_row_reads_as_cash_only(self) -> None:
        self.assertIs(pays_in_cash_only({_FUNDING: _CASH_ONLY}), True)

    def test_a_named_non_cash_consideration_reads_as_not_cash_only(self) -> None:
        text = _CASH_ONLY.replace("金銭以外の対価の種類―", "金銭以外の対価の種類公開買付者株式")
        self.assertIs(pays_in_cash_only({_FUNDING: text}), False)

    def test_a_funding_table_that_does_not_answer_is_unknown(self) -> None:
        self.assertIsNone(pays_in_cash_only({_FUNDING: "買付代金(円)(a)1,000"}))
        self.assertIsNone(pays_in_cash_only({}))
