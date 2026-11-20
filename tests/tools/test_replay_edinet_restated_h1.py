from __future__ import annotations

import csv
import hashlib
import io
import unittest
import zipfile
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from tools.experiments import replay_edinet_restated_h1 as study

from baibai_engine.machine_analysis.observations.financials.edinet_metrics import csv as metrics

FILING = "FilingDateInstant"
TEST_TABLE = "合成の連結経営指標表・両方の比較期間に適用した注記"
TEST_NOTE = hashlib.sha256(TEST_TABLE.encode()).hexdigest()
AMOUNTS = (70_474_000, 440_531_000, 801_682_000)


def fact(tag: str, context: str, value: str, basis: str = "その他") -> dict[str, str]:
    return {
        "要素ID": tag,
        "コンテキストID": context,
        "値": value,
        "連結・個別": basis,
        "期間・時点": "期間",
        "ユニットID": "JPY",
        "単位": "円",
    }


def rows() -> list[dict[str, str]]:
    return [
        *(fact("jpdei_cor:" + tag, FILING, val) for tag, val in study.DEI.items()),
        fact(study.GROUP, FILING, TEST_TABLE),
        *(fact(study.SUMMARY, c, str(v)) for c, v in zip(study.CONTEXTS, AMOUNTS, strict=True)),
        *(
            fact(study.PL, c, str(v), "連結")
            for c, v in zip(study.CONTEXTS[:2], AMOUNTS[:2], strict=True)
        ),
    ]


def archive(members: list[tuple[str, list[dict[str, str]]]]) -> bytes:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        for name, part in members:
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=list(part[0]), delimiter="\t")
            writer.writeheader()
            writer.writerows(part)
            z.writestr(name, text.getvalue().encode("utf-16"))
    return raw.getvalue()


class TestRestatedFilingReplay(unittest.TestCase):
    def test_shared_decode_and_warm_identity(self) -> None:
        body = archive(
            [
                (study.MEMBER, rows()),
                ("XBRL_TO_CSV/audit.csv", [fact("audit:Note", FILING, "other")]),
            ]
        )
        key = replace(study.FIXED, archive_sha256=hashlib.sha256(body).hexdigest())
        read = Mock(return_value=body)
        with (
            patch.object(study, "FIXED", key),
            patch.object(study, "SIZE", len(body)),
            patch.object(study, "GROUP_DIGEST", TEST_NOTE),
            patch.object(metrics, "_decode_csv", wraps=metrics._decode_csv) as decode,
        ):
            original = study.replay(key, read)
            assert original is not None
            self.assertEqual((original.operands, original.profit_yen), (AMOUNTS, 1_171_739_000))
            self.assertIsNotNone(original.metric_fingerprint)
            warm = study.replay(key, read, prior=original)
            assert warm is not None
            self.assertEqual(warm.operands, original.operands)
            self.assertIsNone(warm.metric_fingerprint)
            for field in ("doc_id", "document_revision", "archive_sha256", "extraction_revision"):
                self.assertIsNone(
                    study.replay(replace(key, **{field: "other"}), read, prior=original)
                )
            self.assertIsNone(study.replay(key, read, prior=original, selection="unknown"))
            self.assertIsNone(
                study.replay(
                    key,
                    read,
                    prior=original,
                    selection=study.NOV13,
                    known_at=study.VERIFIED_SELECTIONS[study.NOV13] - timedelta(microseconds=1),
                )
            )
            by_nov13 = study.replay(
                key,
                read,
                prior=original,
                selection=study.NOV13,
                known_at=study.VERIFIED_SELECTIONS[study.NOV13],
            )
            # An operand cache computed later cannot prove an earlier run's production adoption;
            # the same frozen body is separately attested at both boundaries.
            self.assertIsNotNone(by_nov13)
            self.assertEqual((read.call_count, decode.call_count), (1, 2))

            bad = replace(original, operands=(70_474_000, 440_531_000, 774_625_000))
            failed = Mock(return_value=b"")
            self.assertIsNone(study.replay(key, failed, prior=bad))
            failed.assert_called_once_with()
            self.assertEqual(decode.call_count, 2)

    def test_original_position_and_accounting_scope(self) -> None:
        for tag, ctx, key, value in (
            (study.GROUP, FILING, "値", "別表の注記"),
            (study.SUMMARY, study.CONTEXTS[2], "値", "774625000"),
            (study.SUMMARY, study.CONTEXTS[2], "ユニットID", "USD"),
            (study.PL, study.CONTEXTS[0], "連結・個別", "個別"),
            (study.PL, study.CONTEXTS[0], "値", "3000000"),
            ("jpdei_cor:SecurityCodeDEI", FILING, "値", "9127A"),
            ("jpdei_cor:ComparativePeriodEndDateDEI", FILING, "値", "2025-09-29"),
            ("jpdei_cor:AmendmentFlagDEI", FILING, "値", "true"),
        ):
            with self.subTest(tag=tag, value=value):
                part = rows()
                next(r for r in part if (r["要素ID"], r["コンテキストID"]) == (tag, ctx))[key] = (
                    value
                )
                self.assertIsNone(study.member_operands(part, group_digest=TEST_NOTE))
        part = rows()
        part.append(dict(next(r for r in part if r["要素ID"] == study.SUMMARY)))
        self.assertIsNone(study.member_operands(part, group_digest=TEST_NOTE))

    def test_other_member_cannot_supply_missing_table(self) -> None:
        part = rows()
        group = next(r for r in part if r["要素ID"] == study.GROUP)
        part.remove(group)
        b = archive([(study.MEMBER, part), ("XBRL_TO_CSV/audit.csv", [group])])
        key = replace(study.FIXED, archive_sha256=hashlib.sha256(b).hexdigest())
        with (
            patch.object(study, "FIXED", key),
            patch.object(study, "GROUP_DIGEST", TEST_NOTE),
            patch.object(study, "SIZE", len(b)),
        ):
            self.assertIsNone(study.replay(key, lambda: b))
