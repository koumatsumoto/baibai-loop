from __future__ import annotations

import csv
import io
import os
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.models import EdinetAcquisitionError, EdinetRateLimitError
from baibai_engine.machine_analysis.observations.disclosures.delistings import DelistingRecord, store_jpx_delistings
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    TenderOfferExitValue,
    build_tender_offer_exit_values,
    parse_ordinary_share_offer_price,
    pays_in_cash_only,
    read_document_blocks,
    read_tender_offer_exit_values,
    read_tender_offer_filings,
    read_tender_offer_outcome,
    store_tender_offer_exit_values,
)
from baibai_jobs.maintenance.market import cli as app
from baibai_jobs.maintenance.market import delisting_cli
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

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


def _archive_with_unreadable_csv() -> bytes:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/jptoo-001.csv", b"\xff")
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

    def test_reads_the_price_without_the_optional_kin_prefix(self) -> None:
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


class _Reader:
    """Serves prepared archives and records what the build actually read."""

    def __init__(self, archives: dict[str, bytes]) -> None:
        self._archives = archives
        self.requested: list[str] = []

    def read_csv_zip(self, doc_id: str) -> bytes:
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
            "subject_edinet_code, parent_doc_id) "
            "VALUES (?, ?, ?, ?, ?, ?, '1', '0', '0', ?, ?, ?)",
            [
                # The target identifies itself through its own annual report.
                (
                    "2026-01-05",
                    1,
                    "SSELF",
                    "20000",
                    "120",
                    "2026-01-05T09:00",
                    "TARGET",
                    None,
                    None,
                ),
                *(row if len(row) == 9 else (*row, None) for row in documents),
            ],
        )
        connection.commit()
        connection.close()
        store_jpx_delistings(
            sqlite_path,
            [
                DelistingRecord(
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
            ("2026-02-01", 1, "SREG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
            ("2026-03-20", 1, "SRES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
        ]

    def _retain_archives(self, sqlite_path: Path, archives: dict[str, bytes]) -> None:
        connection = open_connection(sqlite_path)
        try:
            for doc_id, content in archives.items():
                retain_source_archive(connection, doc_id, 5, content, stored_on=self.ASOF)
        finally:
            connection.close()

    def _old_exit(self) -> TenderOfferExitValue:
        return TenderOfferExitValue(
            ticker="2000",
            delisted_on=self.DELISTED_ON,
            offer_price_yen=999.0,
            offer_doc_id="SOLDREG",
            result_doc_id="SOLDRES",
            filed_on=date(2026, 1, 1),
        )

    def test_later_non_disclosure_operation_removes_the_old_result(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(tmp, self._registration_and_result(), reason="公開買付け")
            connection = open_connection(sqlite_path)
            try:
                connection.execute(
                    "INSERT INTO edinet_documents("
                    "doc_date, sequence_number, doc_id, disclosure_status, operation_datetime) "
                    "VALUES ('2026-04-01', 1, 'SRES', '1', '2026-04-01T10:00')"
                )
                connection.commit()
                before = read_tender_offer_filings(connection, through=date(2026, 3, 31))
                after = read_tender_offer_filings(connection, through=self.ASOF)
            finally:
                connection.close()
            self.assertEqual([doc.doc_id for doc in before], ["SREG", "SRES"])
            self.assertEqual([doc.doc_id for doc in after], ["SREG"])
            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=_Reader({}), asof=self.ASOF
            )
            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"incomplete_case": 1})

    def test_a_completed_offer_becomes_an_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(summary.candidate_delisting_count, 1)
            self.assertEqual(summary.resolved_count, 1)
            self.assertEqual(values[0].ticker, "2000")
            self.assertEqual(values[0].offer_price_yen, 1060.0)
            self.assertEqual(values[0].delisted_on, self.DELISTED_ON)

    def test_read_count_survives_an_unreadable_csv(self) -> None:
        for bad_document, expected_count in (("SRES", 1), ("SREG", 2)):
            with self.subTest(bad_document=bad_document), TemporaryDirectory() as tmp:
                sqlite_path = self._store(tmp, self._registration_and_result(), reason="公開買付け")
                archives = {
                    "SREG": _archive({_PRICE: "株券１株につき金1,060円", _FUNDING: _CASH_ONLY}),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
                archives[bad_document] = _archive_with_unreadable_csv()
                reader = _Reader(archives)
                values, summary = build_tender_offer_exit_values(
                    sqlite_path, reader=reader, asof=self.ASOF
                )
                self.assertEqual(values, ())
                self.assertEqual(summary.rejection_reason_counts, {"document_unreadable": 1})
                self.assertEqual(summary.read_document_count, expected_count)
                self.assertEqual(summary.read_document_count, len(reader.requested))

    def test_a_non_candidate_delisting_mechanism_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(tmp, self._registration_and_result(), reason="吸収合併")
            reader = _Reader({})

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(summary.candidate_delisting_count, 0)
            self.assertEqual(values, ())
            self.assertEqual(reader.requested, [])

    def test_completed_and_withdrawn_old_rounds_do_not_contaminate_a_later_offer(self) -> None:
        for offeror in ("BIDDER", "OLD_BIDDER"):
            for terminal in ("270", "260"):
                with self.subTest(offeror=offeror, terminal=terminal), TemporaryDirectory() as tmp:
                    sqlite_path = self._store(
                        tmp,
                        [
                            (
                                "2025-09-01",
                                1,
                                "SOLDREG",
                                None,
                                "240",
                                "2025-09-01T09:00",
                                offeror,
                                "TARGET",
                                None,
                            ),
                            (
                                "2025-10-01",
                                1,
                                "SOLDEND",
                                None,
                                terminal,
                                "2025-10-01T09:00",
                                offeror,
                                "TARGET",
                                "SOLDREG",
                            ),
                            *self._registration_and_result(),
                            # Late revisions belong to the old registration/result.
                            (
                                "2026-04-01",
                                1,
                                "SOLDCOR",
                                None,
                                "250",
                                "2026-04-01T09:00",
                                offeror,
                                "TARGET",
                                "SOLDREG",
                            ),
                            (
                                "2026-04-02",
                                1,
                                "SOLDRESCOR",
                                None,
                                "280",
                                "2026-04-02T09:00",
                                offeror,
                                "TARGET",
                                "SOLDEND",
                            ),
                        ],
                        reason="公開買付け、株式併合",
                    )
                    reader = _Reader(
                        {
                            "SREG": _archive(
                                {_PRICE: "株券１株につき金1,060円", _FUNDING: _CASH_ONLY}
                            ),
                            "SRES": _archive({_OUTCOME: _SUCCESS}),
                        }
                    )
                    values, summary = build_tender_offer_exit_values(
                        sqlite_path, reader=reader, asof=self.ASOF
                    )
                    self.assertEqual(summary.resolved_count, 1)
                    self.assertEqual(values[0].offer_price_yen, 1060)
                    self.assertEqual(values[0].offer_doc_id, "SREG")
                    self.assertEqual(values[0].result_doc_id, "SRES")
                    self.assertEqual(reader.requested, ["SRES", "SREG"])

    def test_a_new_incomplete_round_does_not_fall_back_to_an_old_completed_round(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    (
                        "2026-04-01",
                        1,
                        "SNEW",
                        None,
                        "240",
                        "2026-04-01T09:00",
                        "BIDDER",
                        "TARGET",
                        None,
                    ),
                ],
                reason="公開買付け",
            )
            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=_Reader({}), asof=self.ASOF
            )
            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"incomplete_case": 1})

    def test_an_unfinished_overlapping_bid_does_not_disappear(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    (
                        "2026-02-10",
                        1,
                        "SRIVALREG",
                        None,
                        "240",
                        "2026-02-10T09:00",
                        "RIVAL",
                        "TARGET",
                        None,
                    ),
                ],
                reason="公開買付け",
            )
            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=_Reader({}), asof=self.ASOF
            )
            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"incomplete_case": 1})

    def test_same_day_close_and_new_registration_do_not_prove_separate_rounds(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    (
                        "2026-03-20",
                        2,
                        "SRIVALREG",
                        None,
                        "240",
                        "2026-03-20T10:00",
                        "RIVAL",
                        "TARGET",
                    ),
                    (
                        "2026-04-01",
                        1,
                        "SRIVALRES",
                        None,
                        "270",
                        "2026-04-01T09:00",
                        "RIVAL",
                        "TARGET",
                    ),
                ],
                reason="公開買付け",
            )
            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=_Reader({}), asof=self.ASOF
            )
            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"multiple_offerors": 1})

    def test_parent_id_keeps_a_late_first_tier_correction_in_its_own_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    ("2026-02-05", 1, "SREG2", None, "240", "2026-02-05T09:00", "BIDDER", "TARGET"),
                    (
                        "2026-02-10",
                        1,
                        "SCOR",
                        None,
                        "250",
                        "2026-02-10T09:00",
                        "BIDDER",
                        "TARGET",
                        "SREG",
                    ),
                ],
                reason="公開買付け",
            )
            reader = _Reader(
                {
                    "SREG": _archive({_PRICE: "株券１株につき金2,000円", _FUNDING: _CASH_ONLY}),
                    "SREG2": _archive({_PRICE: "株券１株につき金1,000円", _FUNDING: _CASH_ONLY}),
                    "SCOR": _archive({_PRICE: "株券１株につき金1,000円", _FUNDING: _CASH_ONLY}),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )
            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )
            self.assertEqual(summary.resolved_count, 1)
            self.assertEqual(values[0].offer_price_yen, 1000)
            self.assertNotIn("SREG", reader.requested)

    def test_an_unknown_or_different_offeror_parent_is_not_assigned_to_the_latest_offer(
        self,
    ) -> None:
        for parent, offeror in ((None, "BIDDER"), ("SMISSING", "BIDDER"), ("SREG", "RIVAL")):
            with self.subTest(parent=parent), TemporaryDirectory() as tmp:
                sqlite_path = self._store(
                    tmp,
                    [
                        *self._registration_and_result(),
                        (
                            "2026-04-01",
                            1,
                            "SCOR",
                            None,
                            "250",
                            "2026-04-01T09:00",
                            offeror,
                            "TARGET",
                            parent,
                        ),
                    ],
                    reason="公開買付け",
                )
                values, summary = build_tender_offer_exit_values(
                    sqlite_path, reader=_Reader({}), asof=self.ASOF
                )
                self.assertEqual(values, ())
                self.assertEqual(summary.rejection_reason_counts, {"incomplete_case": 1})

    def test_squeeze_out_mechanisms_require_the_same_cash_offer_evidence(self) -> None:
        for reason in ("株式の併合", "株式併合", "株式等売渡請求による取得"):
            with self.subTest(reason=reason), TemporaryDirectory() as tmp:
                sqlite_path = self._store(tmp, self._registration_and_result(), reason=reason)
                reader = _Reader(
                    {
                        "SREG": _archive(
                            {_PRICE: "株券普通株式１株につき金1,060円", _FUNDING: _CASH_ONLY}
                        ),
                        "SRES": _archive({_OUTCOME: _SUCCESS}),
                    }
                )
                values, summary = build_tender_offer_exit_values(
                    sqlite_path, reader=reader, asof=self.ASOF
                )
                self.assertEqual(summary.candidate_delisting_count, 1)
                self.assertEqual(summary.resolved_count, 1)
                self.assertEqual(values[0].offer_price_yen, 1060.0)
                self.assertEqual(values[0].delisted_on, self.DELISTED_ON)

    def test_squeeze_out_reason_alone_does_not_establish_a_cash_exit(self) -> None:
        for reason in ("株式の併合", "株式等売渡請求による取得"):
            with self.subTest(reason=reason), TemporaryDirectory() as tmp:
                sqlite_path = self._store(tmp, [], reason=reason)
                reader = _Reader({})
                values, summary = build_tender_offer_exit_values(
                    sqlite_path, reader=reader, asof=self.ASOF
                )
                self.assertEqual(summary.candidate_delisting_count, 1)
                self.assertEqual(summary.resolved_count, 0)
                self.assertEqual(values, ())
                self.assertEqual(
                    summary.rejection_reason_counts, {"no_filing_in_document_window": 1}
                )
                self.assertEqual(reader.requested, [])

    def test_squeeze_out_non_cash_consideration_remains_unresolved(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="株式等売渡請求による取得"
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {
                            _PRICE: "株券普通株式１株につき金1,060円",
                            _FUNDING: "金銭以外の対価の種類株式",
                        }
                    ),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )
            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )
            self.assertEqual(summary.candidate_delisting_count, 1)
            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})

    def test_a_withdrawn_offer_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    ("2026-03-01", 1, "SWDR", None, "260", "2026-03-01T09:00", "BIDDER", "TARGET"),
                ],
                reason="ＭＢＯ（公開買付け、株式併合）",
            )
            reader = _Reader({})

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"withdrawn": 1})

    def test_an_offer_that_missed_its_floor_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SRES": _archive({_OUTCOME: _FAILURE}),
                }
            )

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})
            # The price is never read once the offer is known not to have bought.
            self.assertEqual(reader.requested, ["SRES"])

    def test_two_offerors_over_one_target_produce_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    ("2026-02-10", 1, "SREG2", None, "240", "2026-02-10T09:00", "RIVAL", "TARGET"),
                    ("2026-03-25", 1, "SRES2", None, "270", "2026-03-25T09:00", "RIVAL", "TARGET"),
                ],
                reason="他社による買収（公開買付け、株式併合）",
            )
            reader = _Reader({})

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"multiple_offerors": 1})

    def test_a_two_tier_offer_at_two_prices_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "SREG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    ("2026-02-05", 1, "SREGB", None, "240", "2026-02-05T09:00", "BIDDER", "TARGET"),
                    ("2026-03-20", 1, "SRES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="他社による買収（公開買付け、株式併合）",
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,650円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SREGB": _archive(
                        {_PRICE: "株券普通株式１株につき金1,240円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})

    def test_a_second_tier_that_cannot_be_read_still_blocks_the_case(self) -> None:
        """A tier that is unreadable is not a tier that did not happen."""
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "SREG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    ("2026-02-05", 1, "SREGB", None, "240", "2026-02-05T09:00", "BIDDER", "TARGET"),
                    ("2026-03-20", 1, "SRES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="他社による買収（公開買付け、株式併合）",
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {
                            _PRICE: "株券普通株式１株につき金1,650円算定の基礎…",
                            _FUNDING: _CASH_ONLY,
                        }
                    ),
                    # The second tier states a price the table cannot resolve.
                    "SREGB": _archive({_PRICE: "株券―新株予約権証券―", _FUNDING: _CASH_ONLY}),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})

    def test_unreadable_correction_does_not_restore_an_old_value(self) -> None:
        cases = (
            ("250", "SREG", {_PRICE: "株券１株につき金1,000円、１株につき金2,000円"}),
            ("250", "SREG", {_PRICE: ""}),
            ("250", "SREG", {_FUNDING: "買付代金のみ。対価の種類不明"}),
            ("280", "SRES", {_OUTCOME: _SUCCESS + _FAILURE}),
            ("280", "SRES", {_OUTCOME: ""}),
            (
                "250",
                "SREG",
                {
                    _PRICE: "株券１株につき金1,000円",
                    "other:PriceOfPurchaseEtcTextBlock": "株券１株につき金2,000円",
                },
            ),
            (
                "250",
                "SREG",
                {
                    _FUNDING: _CASH_ONLY,
                    "other:FundEtcForPurchaseEtcTextBlock": "金銭以外の対価の種類株式",
                },
            ),
            (
                "280",
                "SRES",
                {_OUTCOME: _SUCCESS, "other:SuccessOrFailureOfTenderOfferTextBlock": _FAILURE},
            ),
        )
        for doc_type, parent, blocks in cases:
            with self.subTest(blocks=blocks), TemporaryDirectory() as tmp:
                sqlite_path = self._store(
                    tmp,
                    [
                        *self._registration_and_result(),
                        (
                            "2026-04-01",
                            1,
                            "SCOR",
                            None,
                            doc_type,
                            "2026-04-01T09:00",
                            "BIDDER",
                            "TARGET",
                            parent,
                        ),
                    ],
                    reason="公開買付け",
                )
                reader = _Reader(
                    {
                        "SREG": _archive({_PRICE: "株券１株につき金1,060円", _FUNDING: _CASH_ONLY}),
                        "SRES": _archive({_OUTCOME: _SUCCESS}),
                        "SCOR": _archive(blocks),
                    }
                )
                values, summary = build_tender_offer_exit_values(
                    sqlite_path, reader=reader, asof=self.ASOF
                )
                self.assertEqual(values, ())
                self.assertEqual(
                    summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1}
                )

    def test_a_correction_supersedes_the_price_it_revises(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "SREG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    (
                        "2026-02-20",
                        1,
                        "SCOR",
                        None,
                        "250",
                        "2026-02-20T09:00",
                        "BIDDER",
                        "TARGET",
                        "SREG",
                    ),
                    ("2026-03-20", 1, "SRES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="ＭＢＯ（公開買付け、株式併合）",
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SCOR": _archive(
                        {_PRICE: "株券普通株式１株につき金1,300円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, _ = build_tender_offer_exit_values(sqlite_path, reader=reader, asof=self.ASOF)

            self.assertEqual(values[0].offer_price_yen, 1300.0)
            self.assertEqual(values[0].offer_doc_id, "SCOR")

    def test_a_correction_that_leaves_the_price_alone_falls_back_to_the_registration(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    ("2026-02-01", 1, "SREG", None, "240", "2026-02-01T09:00", "BIDDER", "TARGET"),
                    (
                        "2026-02-20",
                        1,
                        "SCOR",
                        None,
                        "250",
                        "2026-02-20T09:00",
                        "BIDDER",
                        "TARGET",
                        "SREG",
                    ),
                    ("2026-03-20", 1, "SRES", None, "270", "2026-03-20T09:00", "BIDDER", "TARGET"),
                ],
                reason="ＭＢＯ（公開買付け、株式併合）",
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SCOR": _archive({"jptoo-ton_cor:PeriodOfPurchaseEtcTextBlock": "期間の訂正"}),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, _ = build_tender_offer_exit_values(sqlite_path, reader=reader, asof=self.ASOF)

            self.assertEqual(values[0].offer_price_yen, 1060.0)
            self.assertEqual(values[0].offer_doc_id, "SREG")

    def test_an_offer_paying_partly_in_stock_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                self._registration_and_result(),
                reason="他社による買収（公開買付け、株式併合）",
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {
                            _PRICE: "株券普通株式１株につき金1,060円算定の基礎…",
                            _FUNDING: _CASH_ONLY.replace(
                                "金銭以外の対価の種類―", "金銭以外の対価の種類公開買付者株式"
                            ),
                        }
                    ),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"price_or_outcome_unreadable": 1})

    def test_a_registration_without_a_funding_table_produces_no_exit_value(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            reader = _Reader(
                {
                    "SREG": _archive({_PRICE: "株券普通株式１株につき金1,060円算定の基礎…"}),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )

            values, _ = build_tender_offer_exit_values(sqlite_path, reader=reader, asof=self.ASOF)

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
                    DelistingRecord(
                        delisted_on=self.DELISTED_ON,
                        ticker="2000",
                        name="テスト",
                        market="プライム",
                        reason="ＭＢＯ（公開買付け、株式併合）",
                    )
                ],
            )
            reader = _Reader({})

            values, summary = build_tender_offer_exit_values(
                sqlite_path, reader=reader, asof=self.ASOF
            )

            self.assertEqual(values, ())
            self.assertEqual(summary.rejection_reason_counts, {"no_filing_in_document_window": 1})

    def test_an_empty_derivation_over_an_empty_table_is_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            open_connection(sqlite_path).close()

            store_tender_offer_exit_values(sqlite_path, ())

            self.assertEqual(read_tender_offer_exit_values(sqlite_path), ())

    def test_an_empty_derivation_retracts_values_no_longer_established(self) -> None:
        """Derived proxies must not outlive the filing evidence that supports them."""
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp, self._registration_and_result(), reason="ＭＢＯ（公開買付け、株式併合）"
            )
            reader = _Reader(
                {
                    "SREG": _archive(
                        {_PRICE: "株券普通株式１株につき金1,060円算定の基礎…", _FUNDING: _CASH_ONLY}
                    ),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                }
            )
            values, _ = build_tender_offer_exit_values(sqlite_path, reader=reader, asof=self.ASOF)
            store_tender_offer_exit_values(sqlite_path, values)

            self.assertEqual(store_tender_offer_exit_values(sqlite_path, ()), 0)
            self.assertEqual(read_tender_offer_exit_values(sqlite_path), ())

    def test_offline_cli_reads_retained_archives_without_credentials_or_http(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(tmp, self._registration_and_result(), reason="公開買付け")
            self._retain_archives(
                sqlite_path,
                {
                    "SREG": _archive({_PRICE: "株券１株につき金1,060円", _FUNDING: _CASH_ONLY}),
                    "SRES": _archive({_OUTCOME: _SUCCESS}),
                },
            )
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"EDINET_API_KEY": "", "JQUANTS_API_KEY": ""}),
                patch.object(app, "load_project_env", return_value=None),
                patch("requests.Session", side_effect=AssertionError("HTTP session constructed")),
                patch.object(
                    app, "EDINETRefresh", side_effect=AssertionError("acquisition constructed")
                ),
                patch.object(
                    app, "ProviderBundle", side_effect=AssertionError("bundle constructed")
                ),
                redirect_stdout(output),
            ):
                result = app.main(
                    [
                        "build-tender-offer-exits",
                        "--asof",
                        self.ASOF.isoformat(),
                        "--sqlite-path",
                        str(sqlite_path),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertIn("resolved=1, reads=2", output.getvalue())
            self.assertEqual(read_tender_offer_exit_values(sqlite_path)[0].offer_price_yen, 1060.0)

    def test_offline_missing_or_non_zip_archive_preserves_old_exit(self) -> None:
        for kind in ("missing", "non_zip"):
            with self.subTest(kind=kind), TemporaryDirectory() as tmp:
                sqlite_path = self._store(tmp, self._registration_and_result(), reason="公開買付け")
                old = self._old_exit()
                store_tender_offer_exit_values(sqlite_path, (old,))
                if kind == "non_zip":
                    self._retain_archives(sqlite_path, {"SRES": b"not a ZIP archive"})
                stderr = io.StringIO()
                with (
                    patch.object(delisting_cli, "store_tender_offer_exit_values") as save,
                    redirect_stderr(stderr),
                ):
                    result = delisting_cli.build_tender_offer_exits_command(
                        sqlite_path=sqlite_path,
                        asof_date=self.ASOF,
                        reader=EDINETReader(sqlite_path),
                        stdout=io.StringIO(),
                    )
                self.assertEqual(result, 1)
                self.assertIn("EDINETProviderError:", stderr.getvalue())
                if kind == "non_zip":
                    self.assertIn(
                        "stored EDINET tender-offer archive is not a ZIP", stderr.getvalue()
                    )
                save.assert_not_called()
                self.assertEqual(read_tender_offer_exit_values(sqlite_path), (old,))

    def test_successful_empty_offline_build_retracts_old_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(tmp, self._registration_and_result(), reason="吸収合併")
            store_tender_offer_exit_values(sqlite_path, (self._old_exit(),))
            output = io.StringIO()
            result = delisting_cli.build_tender_offer_exits_command(
                sqlite_path=sqlite_path,
                asof_date=self.ASOF,
                reader=EDINETReader(sqlite_path),
                stdout=output,
            )
            self.assertEqual(result, 0)
            self.assertIn("resolved=0, reads=0", output.getvalue())
            self.assertEqual(read_tender_offer_exit_values(sqlite_path), ())

    def test_refresh_acquires_only_documents_requested_by_newest_correction(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = self._store(
                tmp,
                [
                    *self._registration_and_result(),
                    (
                        "2026-02-10",
                        1,
                        "SCOR",
                        None,
                        "250",
                        "2026-02-10T09:00",
                        "BIDDER",
                        "TARGET",
                        "SREG",
                    ),
                ],
                reason="公開買付け",
            )
            archives = {
                "SRES": _archive({_OUTCOME: _SUCCESS}),
                "SCOR": _archive({_PRICE: "株券１株につき金1,300円", _FUNDING: _CASH_ONLY}),
            }
            acquisition = Mock()

            def acquire(doc_id: str) -> bytes:
                content = archives[doc_id]
                self._retain_archives(sqlite_path, {doc_id: content})
                return content

            acquisition.download_csv_zip.side_effect = acquire
            output = io.StringIO()
            result = delisting_cli.refresh_tender_offer_exits_command(
                sqlite_path=sqlite_path,
                asof_date=self.ASOF,
                reader=EDINETReader(sqlite_path),
                acquisition=acquisition,
                stdout=output,
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                [call.args[0] for call in acquisition.download_csv_zip.call_args_list],
                ["SRES", "SCOR"],
            )
            self.assertIn("resolved=1, reads=2", output.getvalue())
            self.assertEqual(read_tender_offer_exit_values(sqlite_path)[0].offer_price_yen, 1300.0)

    def test_cli_source_failure_does_not_replace_existing_exit_values(self) -> None:
        for error_type in (EdinetAcquisitionError, EdinetRateLimitError):
            with self.subTest(error_type=error_type.__name__), TemporaryDirectory() as tmp:
                sqlite_path = self._store(tmp, self._registration_and_result(), reason="公開買付け")
                old = TenderOfferExitValue(
                    ticker="2000",
                    delisted_on=self.DELISTED_ON,
                    offer_price_yen=1060.0,
                    offer_doc_id="SREG",
                    result_doc_id="SRES",
                    filed_on=date(2026, 3, 20),
                )
                store_tender_offer_exit_values(sqlite_path, (old,))
                acquisition = Mock()
                acquisition.download_csv_zip.side_effect = error_type("synthetic EDINET failure")
                stderr = io.StringIO()
                with (
                    patch.object(delisting_cli, "store_tender_offer_exit_values") as save,
                    redirect_stderr(stderr),
                ):
                    result = delisting_cli.refresh_tender_offer_exits_command(
                        sqlite_path=sqlite_path,
                        asof_date=self.ASOF,
                        reader=EDINETReader(sqlite_path),
                        acquisition=acquisition,
                        stdout=io.StringIO(),
                    )
                self.assertEqual(result, 1)
                self.assertTrue(stderr.getvalue().startswith(f"{error_type.__name__}: "))
                acquisition.download_csv_zip.assert_called_once_with("SRES")
                save.assert_not_called()
                self.assertEqual(read_tender_offer_exit_values(sqlite_path), (old,))


class DocumentBlocksTest(unittest.TestCase):
    def test_duplicate_blocks_do_not_select_a_value_by_archive_order(self) -> None:
        cases = (
            (
                _PRICE,
                "株券１株につき金1,000円",
                "株券１株につき金2,000円",
                parse_ordinary_share_offer_price,
                1000,
            ),
            (_OUTCOME, _SUCCESS, _FAILURE, read_tender_offer_outcome, True),
            (_FUNDING, _CASH_ONLY, "金銭以外の対価の種類株式", pays_in_cash_only, True),
        )
        for element, first, second, parse, agreed in cases:
            for values in ((first, second, first), (second, first, second), (first, first)):
                with self.subTest(element=element, values=values):
                    buffer = io.BytesIO()
                    with zipfile.ZipFile(buffer, "w") as archive:
                        for index, value in enumerate(values):
                            text = io.StringIO()
                            writer = csv.writer(text, delimiter="\t")
                            writer.writerow(("要素ID", "項目名", "値"))
                            writer.writerow((element, "項目", value))
                            archive.writestr(
                                f"member-{index}.csv", text.getvalue().encode("utf-16")
                            )
                    blocks = read_document_blocks(buffer.getvalue())
                    self.assertIn(element, blocks)
                    self.assertEqual(parse(blocks), agreed if len(set(values)) == 1 else None)

    def test_matching_suffixes_must_agree_regardless_of_mapping_order(self) -> None:
        first = {
            _PRICE: "株券１株につき金1,000円",
            "other:PriceOfPurchaseEtcTextBlock": "株券１株につき金2,000円",
        }
        self.assertIsNone(parse_ordinary_share_offer_price(first))
        self.assertIsNone(parse_ordinary_share_offer_price(dict(reversed(list(first.items())))))
        self.assertEqual(
            parse_ordinary_share_offer_price(dict.fromkeys(first, "株券１株につき金1,000円")), 1000
        )

    def test_every_element_of_every_csv_member_is_flattened(self) -> None:
        blocks = read_document_blocks(_archive({_PRICE: "価格", _OUTCOME: "成否"}))
        self.assertEqual(blocks[_PRICE], "価格")
        self.assertEqual(blocks[_OUTCOME], "成否")


class CashConsiderationTest(unittest.TestCase):
    def test_a_dash_in_the_non_cash_row_reads_as_cash_only(self) -> None:
        self.assertIs(pays_in_cash_only({_FUNDING: _CASH_ONLY}), True)

    def test_a_named_non_cash_consideration_reads_as_not_cash_only(self) -> None:
        text = _CASH_ONLY.replace("金銭以外の対価の種類―", "金銭以外の対価の種類公開買付者株式")
        self.assertIs(pays_in_cash_only({_FUNDING: text}), False)

    def test_a_funding_table_that_does_not_answer_is_unknown(self) -> None:
        self.assertIsNone(pays_in_cash_only({_FUNDING: "買付代金(円)(a)1,000"}))
        self.assertIsNone(pays_in_cash_only({}))


if __name__ == "__main__":
    unittest.main()
