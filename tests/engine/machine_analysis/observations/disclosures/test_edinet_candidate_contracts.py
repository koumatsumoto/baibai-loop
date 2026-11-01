"""現行EDINET文書選択の回帰契約。"""

from __future__ import annotations

import unittest
from datetime import date

from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    parse_doc_id,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import (
    select_document_candidates,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError


class EDINETCandidateTests(unittest.TestCase):
    def test_select_document_candidates_requires_csv_and_prefers_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100A",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "submitDateTime": "2026-05-01 10:00",
                },
                {
                    "docID": "S100B",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "submitDateTime": "2026-05-01 11:00",
                },
                {
                    "docID": "S100C",
                    "secCode": "67580",
                    "docTypeCode": "120",
                    "csvFlag": "0",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
                {
                    "docID": "S100D",
                    "secCode": "99840",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "0",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
            ]
        ).candidates
        self.assertEqual(selected["7203"].doc_id, "S100B")
        self.assertNotIn("6758", selected)
        self.assertNotIn("9984", selected)

    def test_select_document_candidates_rejects_invalid_sec_code(self) -> None:
        with self.assertRaisesRegex(EDINETProviderError, "invalid EDINET secCode"):
            select_document_candidates(
                [
                    {
                        "docID": "S100A",
                        "secCode": "../../72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "disclosureStatus": "0",
                        "withdrawalStatus": "0",
                    }
                ]
            )

    def test_select_document_candidates_skips_missing_sec_code(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100A",
                    "secCode": None,
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
                {
                    "docID": "S100B",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                },
            ]
        ).candidates

        self.assertEqual(tuple(selected), ("7203",))

    def test_parse_doc_id_rejects_path_traversal(self) -> None:
        with self.assertRaisesRegex(EDINETProviderError, "invalid EDINET docID"):
            parse_doc_id("../../etc/passwd")

    def test_parse_doc_id_sanitizes_control_characters_in_error(self) -> None:
        with self.assertRaises(EDINETProviderError) as ctx:
            parse_doc_id("S100\nBAD")

        self.assertNotIn("\n", str(ctx.exception))
        self.assertIn("'S100?BAD'", str(ctx.exception))

    def test_select_document_candidates_prefers_new_period_over_old_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100OLD",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2024-04-01",
                    "periodEnd": "2025-03-31",
                    "submitDateTime": "2025-07-01 10:00",
                },
                {
                    "docID": "S100NEW",
                    "secCode": "72030",
                    "docTypeCode": "160",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2025-09-30",
                    "submitDateTime": "2025-11-01 10:00",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100NEW")

    def test_select_document_candidates_folds_disclosure_and_release_events(self) -> None:
        origin = {
            "doc_date": "2026-04-01",
            "seqNumber": 1,
            "docID": "S100TEST",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "2",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "submitDateTime": "2026-04-01 10:00",
        }
        hidden = {
            **origin,
            "doc_date": "2026-04-02",
            "opeDateTime": "2026-04-02 12:00",
            "disclosureStatus": "1",
        }
        released = {
            **origin,
            "doc_date": "2026-04-03",
            "opeDateTime": "2026-04-03 12:00",
            "disclosureStatus": "3",
        }

        self.assertEqual(select_document_candidates([origin, hidden]).candidates, {})
        selected = select_document_candidates([released, hidden, origin]).candidates
        self.assertEqual(selected["7203"].doc_id, "S100TEST")

    def test_select_document_candidates_tombstones_withdrawn_old_parent_and_recent_child(
        self,
    ) -> None:
        parent = {
            "doc_date": "2024-04-01",
            "seqNumber": 1,
            "docID": "S100PARENT",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        child = {
            **parent,
            "doc_date": "2026-04-01",
            "seqNumber": 2,
            "docID": "S100CHILD",
            "docTypeCode": "130",
            "parentDocID": "S100PARENT",
        }
        withdrawal = {
            "doc_date": "2026-04-02",
            "seqNumber": 1,
            "docID": "S100WITHDRAW",
            "parentDocID": "S100PARENT",
            "withdrawalStatus": "1",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "legalStatus": "0",
        }

        selection = select_document_candidates(
            [child, withdrawal, parent], origin_start=date(2026, 4, 1)
        )
        self.assertEqual(selection.candidates, {})
        self.assertEqual(selection.quarantined_events, ())

    def test_select_document_candidates_reports_an_event_without_origin(self) -> None:
        selection = select_document_candidates(
            [
                {
                    "doc_date": "2026-04-02",
                    "seqNumber": 1,
                    "docID": "S100MISSING",
                    "secCode": "72030",
                    "docTypeCode": "120; injected",
                    "docInfoEditStatus": "1",
                    "withdrawalStatus": "0",
                    "disclosureStatus": "0",
                    "opeDateTime": "2026-04-02 12:00",
                }
            ]
        )

        self.assertEqual(selection.candidates, {})
        self.assertEqual(
            tuple(event.doc_id for event in selection.quarantined_events),
            ("S100MISSING",),
        )
        self.assertEqual(selection.quarantined_events[0].ticker, "7203")
        self.assertIsNone(selection.quarantined_events[0].document_type)

    def test_select_document_candidates_reports_a_withdrawal_without_parent(self) -> None:
        selection = select_document_candidates(
            [
                {
                    "doc_date": "2026-04-02",
                    "seqNumber": 1,
                    "docID": "S100WITHDRAW",
                    "parentDocID": "S100GONE",
                    "withdrawalStatus": "1",
                    "docInfoEditStatus": "0",
                    "disclosureStatus": "0",
                    "legalStatus": "0",
                }
            ]
        )

        self.assertEqual(selection.candidates, {})
        self.assertEqual(
            tuple(event.doc_id for event in selection.quarantined_events),
            ("S100WITHDRAW",),
        )

    def test_select_document_candidates_resolves_an_event_before_filtering_old_origins(
        self,
    ) -> None:
        origin = {
            "doc_date": "2024-01-01",
            "seqNumber": 1,
            "docID": "S100OLD",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        event = {
            **origin,
            "doc_date": "2026-04-02",
            "docInfoEditStatus": "1",
            "opeDateTime": "2026-04-02 12:00",
        }
        recent = {
            **origin,
            "doc_date": "2026-04-03",
            "docID": "S100NEW",
            "secCode": "99840",
        }

        selection = select_document_candidates(
            [origin, event, recent],
            origin_start=date(2025, 1, 1),
        )

        self.assertEqual(selection.quarantined_events, ())
        self.assertEqual(sorted(selection.candidates), ["9984"])

    def test_select_document_candidates_rejects_parent_cycle_without_withdrawal(self) -> None:
        base = {
            "doc_date": "2026-04-01",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        with self.assertRaisesRegex(EDINETProviderError, "parent relation cycle"):
            select_document_candidates(
                [
                    {**base, "seqNumber": 1, "docID": "S100A", "parentDocID": "S100B"},
                    {**base, "seqNumber": 2, "docID": "S100B", "parentDocID": "S100A"},
                ]
            )

    def test_select_document_candidates_rejects_event_after_withdrawal(self) -> None:
        origin = {
            "doc_date": "2026-04-01",
            "seqNumber": 1,
            "docID": "S100ORIGIN",
            "secCode": "72030",
            "docTypeCode": "120",
            "csvFlag": "1",
            "xbrlFlag": "1",
            "legalStatus": "1",
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
        }
        withdrawal = {
            "doc_date": "2026-04-02",
            "seqNumber": 1,
            "docID": "S100WITHDRAW",
            "parentDocID": "S100ORIGIN",
            "withdrawalStatus": "1",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "legalStatus": "0",
            "opeDateTime": "2026-04-02 10:00",
        }
        later_edit = {
            **origin,
            "doc_date": "2026-04-03",
            "docInfoEditStatus": "1",
            "opeDateTime": "2026-04-03 10:00",
        }

        with self.assertRaisesRegex(EDINETProviderError, "event follows withdrawal"):
            select_document_candidates([later_edit, origin, withdrawal])

    def test_select_document_candidates_rejects_unknown_status(self) -> None:
        with self.assertRaisesRegex(EDINETProviderError, "unknown EDINET legal status"):
            select_document_candidates(
                [
                    {
                        "docID": "S100TEST",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "9",
                    }
                ]
            )

    def test_select_document_candidates_rejects_unknown_withdrawal_status(self) -> None:
        """The merge keeps the later reading of this column, so its domain has to hold."""

        with self.assertRaisesRegex(EDINETProviderError, "unknown EDINET withdrawal status"):
            select_document_candidates(
                [
                    {
                        "docID": "S100TEST",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "xbrlFlag": "1",
                        "legalStatus": "1",
                        "withdrawalStatus": "9",
                    }
                ]
            )

    def test_select_document_candidates_prefers_same_period_annual_correction_from_description(
        self,
    ) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2024-10-01",
                    "periodEnd": "2025-09-30",
                    "submitDateTime": "2025-12-24 16:11",
                },
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正有価証券報告書－第28期(2024/10/01－2025/09/30)",
                    "submitDateTime": "2026-04-15 16:01",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100CORR")
        self.assertEqual(selected["7203"].period_start, date(2024, 10, 1))
        self.assertEqual(selected["7203"].period_end, date(2025, 9, 30))

    def test_select_document_candidates_keeps_newer_period_over_old_correction_description(
        self,
    ) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100OLD",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正有価証券報告書－第90期(2024/04/01－2025/03/31)",
                    "submitDateTime": "2026-01-23 14:35",
                },
                {
                    "docID": "S100NEW",
                    "secCode": "72030",
                    "docTypeCode": "160",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2025-11-01 10:00",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100NEW")

    def test_select_document_candidates_accepts_semiannual_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "160",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2025-11-14 15:34",
                },
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "170",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正半期報告書－第5期(2025/04/01-2026/03/31)",
                    "submitDateTime": "2026-01-30 15:33",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100CORR")
        self.assertEqual(selected["7203"].doc_type_code, "170")

    def test_select_document_candidates_accepts_quarterly_correction(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "140",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2021-11-01",
                    "periodEnd": "2022-01-31",
                    "submitDateTime": "2022-03-15 15:00",
                },
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "150",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "docDescription": "訂正四半期報告書－第72期第3四半期(2021/11/01～2022/01/31)",
                    "submitDateTime": "2025-02-14 15:29",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100CORR")
        self.assertEqual(selected["7203"].doc_type_code, "150")

    def test_select_document_candidates_prefers_latest_submit_within_same_period(self) -> None:
        selected = select_document_candidates(
            [
                {
                    "docID": "S100CORR",
                    "secCode": "72030",
                    "docTypeCode": "130",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2026-06-01 10:00",
                },
                {
                    "docID": "S100NORMAL",
                    "secCode": "72030",
                    "docTypeCode": "120",
                    "csvFlag": "1",
                    "xbrlFlag": "1",
                    "legalStatus": "1",
                    "disclosureStatus": "0",
                    "withdrawalStatus": "0",
                    "periodStart": "2025-04-01",
                    "periodEnd": "2026-03-31",
                    "submitDateTime": "2026-06-02 10:00",
                },
            ]
        ).candidates

        self.assertEqual(selected["7203"].doc_id, "S100NORMAL")
